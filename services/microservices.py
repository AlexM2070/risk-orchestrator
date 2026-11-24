"""services/microservices.py — SharedDataLayer и 4 эмулятора логистических микросервисов.

Микросервис не знает, идёт ли он через LockManager. Без оркестратора коллизии
выявляются самим SharedDataLayer и приводят к честным исключениям:
  * DeadlockError           — цикл ожидания AB-BA (StockoutRisk <-> DelayRisk);
  * DataCorruptionError     — Lost Update / грязное чтение на витринах;
  * CrossDateCollisionError — перезапись общего staging-буфера (CarrierDefaultRisk).

Схема синтетических данных (synthetic_data/*.csv или *.json, если файл есть):
  warehouse_stocks: sku, warehouse, qty, min_qty, days_to_expiry
  transit_telemetry: shipment, sku, carrier, qty, eta_delay_h, status
  weather_traffic: route, severity (0..1)
  supplier_sla: carrier, on_time_rate (0..1)
"""
from __future__ import annotations

import asyncio
import copy
import csv
import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.models import (
    CrossDateCollisionError,
    DataCorruptionError,
    DeadlockError,
    ProcessSpec,
    ProgressCallback,
    TriggerType,
)

# Допущение: имена констант в core.models не зафиксированы в ТЗ, поэтому дублируем
# строковые значения локально (значения совпадают с контрактом).
STOCKOUT, DELAY, SPOILAGE, CARRIER = "StockoutRisk", "DelayRisk", "SpoilageRisk", "CarrierDefaultRisk"
WAREHOUSE, TRANSIT, WEATHER, SLA = "WarehouseStocks", "TransitTelemetry", "WeatherAndTraffic", "SupplierSLA"
SOURCES = (WAREHOUSE, TRANSIT, WEATHER, SLA)

_FILES = {WAREHOUSE: "warehouse_stocks", TRANSIT: "transit_telemetry",
          WEATHER: "weather_traffic", SLA: "supplier_sla"}

# Компактные данные по умолчанию, если synthetic_data/ пуста.
_DEFAULTS: dict[str, list[dict[str, Any]]] = {
    WAREHOUSE: [
        {"sku": "A1", "warehouse": "W1", "qty": 40, "min_qty": 50, "days_to_expiry": 2},
        {"sku": "B2", "warehouse": "W1", "qty": 200, "min_qty": 80, "days_to_expiry": 30},
        {"sku": "C3", "warehouse": "W2", "qty": 15, "min_qty": 30, "days_to_expiry": 5},
        {"sku": "D4", "warehouse": "W2", "qty": 90, "min_qty": 60, "days_to_expiry": 1},
    ],
    TRANSIT: [
        {"shipment": "S1", "sku": "A1", "carrier": "FastTrans", "qty": 30, "eta_delay_h": 2, "status": "moving"},
        {"shipment": "S2", "sku": "C3", "carrier": "SlowLog", "qty": 40, "eta_delay_h": 18, "status": "delayed"},
        {"shipment": "S3", "sku": "B2", "carrier": "FastTrans", "qty": 100, "eta_delay_h": 0, "status": "moving"},
    ],
    WEATHER: [{"route": "R1", "severity": 0.2}, {"route": "R2", "severity": 0.7}],
    SLA: [{"carrier": "FastTrans", "on_time_rate": 0.95}, {"carrier": "SlowLog", "on_time_rate": 0.6}],
}


def _num(v: str) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except (TypeError, ValueError):
        return v


def _load(data_dir: Path, source: str) -> list[dict[str, Any]]:
    """CSV/JSON из synthetic_data/ при наличии, иначе компактный дефолт."""
    try:
        csv_path, json_path = (data_dir / f"{_FILES[source]}{e}" for e in (".csv", ".json"))
        if csv_path.exists():
            with csv_path.open(newline="", encoding="utf-8") as f:
                return [{k: _num(v) for k, v in row.items()} for row in csv.DictReader(f)]
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except (OSError, ValueError):
        pass
    return copy.deepcopy(_DEFAULTS[source])


@dataclass
class _Table:
    rows: list[dict[str, Any]]
    version: int = 0
    meta: dict[str, Any] = field(default_factory=dict)  # результаты записи сервисов (флаги, рейтинги)


class SharedDataLayer:
    """4 общие витрины + общий staging-буфер + детектор коллизий (без блокировок оркестратора)."""

    def __init__(self, data_dir: str | Path | None = None, time_scale: float = 1.0) -> None:
        self.data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent / "synthetic_data"
        self.time_scale = time_scale  # 1.0 = штатные 3–5 с; <1 ускоряет отладку
        self._initial = {s: _load(self.data_dir, s) for s in SOURCES}
        self.reset()

    def reset(self) -> None:
        self.tables = {s: _Table(copy.deepcopy(r)) for s, r in self._initial.items()}
        self.holders: dict[str, dict[int, tuple[str, str]]] = {s: {} for s in SOURCES}  # src -> {task: (mode, process)}
        self.held: dict[int, dict[str, str]] = {}
        self.waiting: dict[int, tuple[str, str]] = {}
        self.snapshots: dict[int, dict[str, int]] = {}
        self.staging: dict[str, Any] = {"owner": None, "date": None, "values": []}
        self._displaced: dict[int, tuple[int, str]] = {}

    # ---- чтение / версии ----
    def rows(self, source: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.tables[source].rows)

    def versions(self) -> dict[str, int]:
        return {s: t.version for s, t in self.tables.items()}

    # ---- доступ к витринам с детекцией Deadlock / Lost Update ----
    def _blockers(self, task_id: int, source: str, mode: str) -> set[int]:
        return {t for t, (m, _) in self.holders[source].items() if t != task_id and "W" in (m, mode)}

    def _has_cycle(self, me: int) -> bool:
        seen: set[int] = set()
        stack = [me]
        while stack:
            t = stack.pop()
            w = self.waiting.get(t)
            if not w:
                continue
            for b in self._blockers(t, *w):
                if b == me:
                    return True
                if b not in seen:
                    seen.add(b)
                    stack.append(b)
        return False

    async def access(self, task_id: int, process_id: str, source: str, mode: str) -> None:
        """Берёт витрину ('R'/'W') и держит до release(). Конфликт -> ожидание, затем ошибка."""
        cur = self.held.get(task_id, {}).get(source)
        if cur == "W" or (cur and mode == "R"):
            return
        self.waiting[task_id] = (source, mode)
        ticks = 0
        try:
            while (blockers := self._blockers(task_id, source, mode)):
                names = ", ".join(f"#{t} {self.holders[source][t][1]}" for t in sorted(blockers))
                if self._has_cycle(task_id):
                    raise DeadlockError(
                        f"AB-BA: задача #{task_id} ({process_id}) ждёт {source}, удерживаемую {names}, "
                        f"которая в свою очередь ждёт ресурс задачи #{task_id}")
                if ticks >= 15:  # «таймаут» устаревшей блокировки без цикла -> порча данных
                    kind = "Lost Update" if mode == "W" and any(
                        self.holders[source][t][0] == "W" for t in blockers) else "грязное чтение"
                    raise DataCorruptionError(
                        f"{kind}: задача #{task_id} ({process_id}) пересеклась по {source} с {names}")
                await asyncio.sleep(0.1 * self.time_scale)
                ticks += 1
        finally:
            self.waiting.pop(task_id, None)
        self.holders[source][task_id] = (mode, process_id)
        self.held.setdefault(task_id, {})[source] = mode
        self.snapshots.setdefault(task_id, {})[source] = self.tables[source].version

    def commit(self, task_id: int, writes: dict[str, dict[str, Any]]) -> None:
        """Проверка версий (защита от Lost Update) и запись результатов в витрины."""
        for src, ver in self.snapshots.get(task_id, {}).items():
            cur = self.tables[src].version
            if cur != ver:
                kind = "Lost Update" if src in writes else "грязное чтение"
                raise DataCorruptionError(f"{kind}: {src} изменена другой задачей (v{ver} -> v{cur}) во время #{task_id}")
        for src, payload in writes.items():
            t = self.tables[src]
            t.meta.update(payload)
            t.version += 1

    def release(self, task_id: int) -> None:
        for src in self.held.pop(task_id, {}):
            self.holders[src].pop(task_id, None)
        self.waiting.pop(task_id, None)
        self.snapshots.pop(task_id, None)
        self._displaced.pop(task_id, None)
        if self.staging["owner"] == task_id:
            self.staging.update(owner=None, date=None, values=[])

    # ---- общий staging-буфер скользящих средних ----
    def staging_reset(self, task_id: int, target_date: str) -> None:
        """Очищает общий буфер (как делает CarrierDefaultRisk) и вытесняет прежнего владельца."""
        prev = self.staging["owner"]
        if prev is not None and prev != task_id:
            self._displaced[prev] = (task_id, target_date)
        self.staging.update(owner=task_id, date=target_date, values=[])

    def _staging_check(self, task_id: int, target_date: str) -> None:
        if task_id in self._displaced:
            by, d = self._displaced[task_id]
            exc = CrossDateCollisionError if d != target_date else DataCorruptionError
            raise exc(f"staging-буфер перезаписан задачей #{by} (дата {d}); "
                      f"данные задачи #{task_id} за {target_date} смешаны")

    def staging_push(self, task_id: int, target_date: str, value: Any) -> None:
        self._staging_check(task_id, target_date)
        self.staging["values"].append(value)

    def staging_values(self, task_id: int, target_date: str) -> list[Any]:
        self._staging_check(task_id, target_date)
        return list(self.staging["values"])


@dataclass
class _Ctx:
    task_id: int
    target_date: str
    trigger: TriggerType
    data: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


def _mean(vals: list[float], default: float = 0.0) -> float:
    return sum(vals) / len(vals) if vals else default


class BaseRiskService(ABC):
    """Общий каркас: 5 шагов по 20% (≈3–5 с суммарно), прогресс после каждого шага."""

    spec: ProcessSpec

    def __init__(self, db: SharedDataLayer) -> None:
        self.db = db

    @abstractmethod
    async def _step(self, i: int, c: _Ctx) -> None:
        """Шаг i=1..5: обращение к витринам / расчёт; на шаге 5 — commit и c.result."""

    async def _acc(self, c: _Ctx, source: str, mode: str) -> list[dict[str, Any]]:
        await self.db.access(c.task_id, self.spec.process_id, source, mode)
        return self.db.rows(source)

    async def run(self, task_id: int, target_date: str, trigger: TriggerType,
                  on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        c = _Ctx(task_id, target_date, trigger)
        pause = random.uniform(3.0, 5.0) * self.db.time_scale / 5
        try:
            for i in range(1, 6):
                await self._step(i, c)
                await asyncio.sleep(pause)
                if on_progress:
                    await on_progress(task_id, i * 20)
            return {"process_id": self.spec.process_id, "target_date": target_date,
                    "trigger": trigger.name, **c.result, "versions": self.db.versions()}
        finally:
            self.db.release(task_id)  # освобождаем удержания и при ошибке/отмене


class StockoutRiskService(BaseRiskService):
    spec = ProcessSpec(STOCKOUT, "Риск дефицита", (SLA, TRANSIT), (WAREHOUSE,), False, 1)
    # Порядок доступа: Warehouse(W) -> SLA(R) -> Transit(R)  (обратный порядку DelayRisk => AB-BA)

    async def _step(self, i: int, c: _Ctx) -> None:
        d = c.data
        if i == 1:
            d["wh"] = await self._acc(c, WAREHOUSE, "W")
        elif i == 2:
            d["sla"] = await self._acc(c, SLA, "R")
        elif i == 3:
            d["tt"] = await self._acc(c, TRANSIT, "R")
        elif i == 4:
            reliable = {r.get("carrier") for r in d["sla"] if r.get("on_time_rate", 1.0) >= 0.8}
            inbound: dict[Any, float] = {}
            for r in d["tt"]:
                if r.get("carrier") in reliable:
                    inbound[r.get("sku")] = inbound.get(r.get("sku"), 0) + r.get("qty", 0)
            d["flags"] = sorted(str(r.get("sku")) for r in d["wh"]
                                if r.get("qty", 0) + inbound.get(r.get("sku"), 0) < r.get("min_qty", 0))
        else:
            self.db.commit(c.task_id, {WAREHOUSE: {f"stockout_flags:{c.target_date}": d["flags"]}})
            c.result = {"stockout_skus": d["flags"]}


class DelayRiskService(BaseRiskService):
    spec = ProcessSpec(DELAY, "Риск срыва сроков", (WEATHER, SLA, WAREHOUSE), (TRANSIT,), False, 1)
    # Порядок доступа: Transit(W) -> Weather/SLA(R) -> Warehouse(R)

    async def _step(self, i: int, c: _Ctx) -> None:
        d = c.data
        if i == 1:
            d["tt"] = await self._acc(c, TRANSIT, "W")
        elif i == 2:
            d["wx"] = await self._acc(c, WEATHER, "R")
            d["sla"] = await self._acc(c, SLA, "R")
        elif i == 3:
            d["wh"] = await self._acc(c, WAREHOUSE, "R")
        elif i == 4:
            sev = _mean([r.get("severity", 0.0) for r in d["wx"]])
            rate = {r.get("carrier"): r.get("on_time_rate", 1.0) for r in d["sla"]}
            low = {r.get("sku") for r in d["wh"] if r.get("qty", 0) < r.get("min_qty", 0)}
            scores = {str(r.get("shipment")): round(min(1.0, r.get("eta_delay_h", 0) / 24 + 0.5 * sev
                                                       + 0.5 * (1 - rate.get(r.get("carrier"), 1.0))), 2)
                      for r in d["tt"]}
            d["scores"] = scores
            d["critical"] = sorted(str(r.get("shipment")) for r in d["tt"]
                                   if r.get("sku") in low and scores[str(r.get("shipment"))] >= 0.5)
        else:
            self.db.commit(c.task_id, {TRANSIT: {f"delay_scores:{c.target_date}": d["scores"]}})
            c.result = {"delay_scores": d["scores"], "critical_shipments": d["critical"]}


class SpoilageRiskService(BaseRiskService):
    spec = ProcessSpec(SPOILAGE, "Риск порчи/просрочки", (WEATHER, WAREHOUSE), (WAREHOUSE,), True, 1)

    async def _step(self, i: int, c: _Ctx) -> None:
        d = c.data
        if i == 1:
            d["wx"] = await self._acc(c, WEATHER, "R")
        elif i == 2:
            d["wh"] = await self._acc(c, WAREHOUSE, "W")  # чтение+запись флагов списания
        elif i == 4:
            sev = _mean([r.get("severity", 0.0) for r in d["wx"]])
            limit = 3 + (2 if sev > 0.5 else 0)  # плохая погода/трассы ускоряют порчу
            d["writeoff"] = sorted(str(r.get("sku")) for r in d["wh"] if r.get("days_to_expiry", 99) <= limit)
        elif i == 5:
            self.db.commit(c.task_id, {WAREHOUSE: {f"writeoff_flags:{c.target_date}": d["writeoff"]}})
            c.result = {"writeoff_skus": d["writeoff"]}


class CarrierDefaultRiskService(BaseRiskService):
    spec = ProcessSpec(CARRIER, "Риск невыхода перевозчика", (TRANSIT, WEATHER), (SLA,), False, 1)

    async def _step(self, i: int, c: _Ctx) -> None:
        db, d = self.db, c.data
        if i == 1:
            db.staging_reset(c.task_id, c.target_date)
        elif i == 2:
            tt = await self._acc(c, TRANSIT, "R")
            wx = await self._acc(c, WEATHER, "R")
            d["sev"] = _mean([r.get("severity", 0.0) for r in wx])
            by_carrier: dict[Any, list[bool]] = {}
            for r in tt:
                by_carrier.setdefault(r.get("carrier"), []).append(r.get("eta_delay_h", 0) > 6)
            for carrier, flags in by_carrier.items():
                db.staging_push(c.task_id, c.target_date, ("share", carrier, _mean([float(f) for f in flags])))
        elif i == 3:
            d["sla"] = await self._acc(c, SLA, "W")
            db.staging_push(c.task_id, c.target_date, ("sev", d["sev"]))
        elif i == 4:
            vals = db.staging_values(c.task_id, c.target_date)  # проверка: буфер всё ещё наш
            sev = next((v[1] for v in vals if v[0] == "sev"), 0.0)
            old = {r.get("carrier"): r.get("on_time_rate", 0.9) for r in d["sla"]}
            d["ratings"] = {v[1]: round(0.7 * old.get(v[1], 0.9) + 0.3 * max(0.0, 1 - v[2] * (1 + sev)), 3)
                            for v in vals if v[0] == "share"}
        else:
            db.staging_values(c.task_id, c.target_date)
            db.commit(c.task_id, {SLA: {f"rolling_rating:{c.target_date}": d["ratings"]}})
            c.result = {"rolling_rating": d["ratings"]}


PROCESS_SPECS: dict[str, ProcessSpec] = {
    cls.spec.process_id: cls.spec
    for cls in (StockoutRiskService, DelayRiskService, SpoilageRiskService, CarrierDefaultRiskService)
}


def create_services(db: SharedDataLayer) -> dict[str, BaseRiskService]:
    return {
        STOCKOUT: StockoutRiskService(db),
        DELAY: DelayRiskService(db),
        SPOILAGE: SpoilageRiskService(db),
        CARRIER: CarrierDefaultRiskService(db),
    }

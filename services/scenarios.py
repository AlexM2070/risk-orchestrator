"""services/scenarios.py — демо-сценарии «в один клик» и генератор случайных пачек."""
from __future__ import annotations

import random

from core.models import ScenarioSpec, TaskRequest, TriggerType
from services.microservices import CARRIER, DELAY, SPOILAGE, STOCKOUT

D0, D1, D2 = "2026-11-20", "2026-11-21", "2026-11-22"
_PROCESSES = (STOCKOUT, DELAY, SPOILAGE, CARRIER)
AUTO, MANUAL = TriggerType.AUTO, TriggerType.MANUAL


def _req(pid: str, date: str, trigger: TriggerType, delay: float = 0.0, priority: int | None = None) -> TaskRequest:
    # Допущение: priority 1 — регламент, 2 — ручной (как в плане); None — решает оркестратор.
    return TaskRequest(process_id=pid, target_date=date, trigger=trigger, priority=priority, delay_sec=delay)


def generate_random_batch(count: int, seed: int | None = None) -> list[TaskRequest]:
    """Случайная пачка: первые 4 задачи покрывают все процессы, даты ограничены 3 значениями
    (гарантирует пересечения по источникам и датам), delay_sec ∈ [0, 2]."""
    rng = random.Random(seed)
    first = list(_PROCESSES)
    rng.shuffle(first)
    batch = [
        _req(first[i] if i < len(first) else rng.choice(_PROCESSES),
             rng.choice((D0, D0, D1, D2)),
             rng.choice((AUTO, MANUAL)),
             round(rng.uniform(0.0, 2.0), 1))
        for i in range(max(0, count))
    ]
    return sorted(batch, key=lambda t: t.delay_sec)


def get_demo_scenarios() -> list[ScenarioSpec]:
    return [
        ScenarioSpec(
            scenario_id="stress_batch",
            title="Запустить всё (Стресс-тест)",
            description="10 случайных задач (seed=2026): пересекающиеся и независимые, разные даты.",
            tasks=generate_random_batch(10, seed=2026),
        ),
        ScenarioSpec(
            scenario_id="manual_vs_auto",
            title="Сценарий 1: Ручной vs Регламент",
            description="Регламентный StockoutRisk и одновременный ручной запуск того же расчёта, "
                        "плюс ручной SpoilageRisk по тем же остаткам (Lost Update на WarehouseStocks).",
            tasks=[_req(STOCKOUT, D0, AUTO, 0.0, 1),
                   _req(STOCKOUT, D0, MANUAL, 0.5, 2),
                   _req(SPOILAGE, D0, MANUAL, 1.0, 2)],
        ),
        ScenarioSpec(
            scenario_id="source_conflict",
            title="Сценарий 2: Конфликт источников",
            description="StockoutRisk и DelayRisk за одну дату: общие WarehouseStocks/TransitTelemetry "
                        "в обратном порядке (AB-BA Deadlock).",
            tasks=[_req(STOCKOUT, D0, AUTO, 0.0, 1),
                   _req(DELAY, D0, AUTO, 0.0, 1)],
        ),
        ScenarioSpec(
            scenario_id="cross_date_deadlock",
            title="Сценарий 3: Перекрёстные даты (Deadlock-тест)",
            description="CarrierDefaultRisk за две даты (общий staging-буфер) + разные процессы за одну дату "
                        "+ тот же StockoutRisk за другую дату.",
            tasks=[_req(CARRIER, D0, AUTO, 0.0, 1),
                   _req(CARRIER, D1, MANUAL, 0.3, 2),
                   _req(STOCKOUT, D0, AUTO, 0.6, 1),
                   _req(DELAY, D0, AUTO, 0.6, 1),
                   _req(STOCKOUT, D1, MANUAL, 1.0, 2)],
        ),
    ]

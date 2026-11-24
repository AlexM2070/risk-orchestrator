"""services/mocks.py — заглушки для автономной работы UI и ядра."""
from __future__ import annotations

import asyncio
import copy
import inspect
from datetime import datetime, timedelta
from typing import Any

from core.models import (
    CrossDateCollisionError,
    DataCorruptionError,
    DeadlockError,
    OrchestratorEvent,
    ProcessSpec,
    ProgressCallback,
    StateChangeCallback,
    TaskRequest,
    TaskRun,
    TaskStatus,
    TriggerType,
)
from services.microservices import DELAY, PROCESS_SPECS, STOCKOUT, TRANSIT, WAREHOUSE


class FakeRiskService:
    """Эмуляция расчёта ≈2 с (5 шагов по 0.4 с) с прогрессом 20..100."""

    def __init__(self, spec: ProcessSpec) -> None:
        self.spec = spec

    async def run(self, task_id: int, target_date: str, trigger: TriggerType,
                  on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        for pct in (20, 40, 60, 80, 100):
            await asyncio.sleep(0.4)
            if on_progress:
                await on_progress(task_id, pct)
        return {"process_id": self.spec.process_id, "target_date": target_date, "trigger": trigger.name, "fake": True}


class MockOrchestrator:
    """Те же публичные методы, что у Orchestrator; упрощённый планировщик «всё или ничего»."""

    def __init__(self, services: dict[str, Any] | None = None) -> None:
        self._specs: dict[str, ProcessSpec] = dict(PROCESS_SPECS)
        self._services = services or {pid: FakeRiskService(s) for pid, s in self._specs.items()}
        self._enabled = True
        self._paused = False
        self._started = False
        self._seq = 0
        self._tasks: dict[int, TaskRun] = {}
        self._events: list[OrchestratorEvent] = []
        self._subs: list[StateChangeCallback] = []
        self._runners: dict[int, asyncio.Task] = {}
        self._doom: dict[int, tuple[int, Exception]] = {}

    # ---------- управление ----------
    async def start(self) -> None:
        self._started = True
        self._log("INFO", None, "Оркестратор запущен (mock)")
        self._pump()
        self._notify()

    async def stop(self) -> None:
        self._started = False
        await self._cancel_runners()
        self._log("INFO", None, "Оркестратор остановлен (mock)")
        self._notify()

    def set_orchestration_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._log("INFO", None, "Режим: " + ("С оркестратором" if enabled else "Без оркестратора (как было)"))
        self._pump()
        self._notify()

    def is_orchestration_enabled(self) -> bool:
        return self._enabled

    async def submit(self, request: TaskRequest) -> TaskRun:
        spec = self._specs.get(request.process_id)
        if spec is None:
            raise ValueError(f"Неизвестный процесс: {request.process_id}")
        self._seq += 1
        task = TaskRun(
            task_id=self._seq, process_id=request.process_id, target_date=request.target_date,
            trigger=request.trigger,
            priority=request.priority if request.priority is not None else spec.default_priority,
            status=TaskStatus.QUEUED, progress=0,
            queued_at=datetime.now() + timedelta(seconds=request.delay_sec),  # delay_sec: отложенная постановка
        )
        self._tasks[task.task_id] = task
        self._log("INFO", task.task_id, f"Заявка {task.process_id} за {task.target_date} ({task.trigger.name})")
        if request.delay_sec > 0:
            asyncio.get_running_loop().call_later(request.delay_sec, self._pump)
        self._pump()
        self._notify()
        return copy.copy(task)

    async def submit_batch(self, requests: list[TaskRequest]) -> list[TaskRun]:
        return [await self.submit(r) for r in requests]

    def pause(self) -> None:
        self._paused = True
        self._log("INFO", None, "Пауза: новые задачи не стартуют")
        self._notify()

    def resume(self) -> None:
        self._paused = False
        self._log("INFO", None, "Продолжение работы")
        self._pump()
        self._notify()

    async def reset(self) -> None:
        await self._cancel_runners()
        self._tasks.clear()
        self._events.clear()
        self._doom.clear()
        self._seq = 0
        self._paused = False
        self._log("INFO", None, "Сброс состояния")
        self._notify()

    def subscribe(self, callback: StateChangeCallback) -> None:
        self._subs.append(callback)

    # ---------- read-only снимки ----------
    def get_process_specs(self) -> list[ProcessSpec]:
        return list(self._specs.values())

    def get_tasks(self, status: TaskStatus | None = None) -> list[TaskRun]:
        return [copy.copy(t) for t in self._tasks.values() if status is None or t.status == status]

    def get_active_locks(self) -> dict[str, list[int]]:
        """Ключи вида 'Источник@дата' и 'process:ID' (если нет параллельных дат) -> id задач."""
        locks: dict[str, list[int]] = {}
        for t in self._running():
            spec = self._specs[t.process_id]
            for src in (*spec.read_sources, *spec.write_sources):
                ids = locks.setdefault(f"{src}@{t.target_date}", [])
                if t.task_id not in ids:
                    ids.append(t.task_id)
            if not spec.allow_parallel_dates:
                locks.setdefault(f"process:{t.process_id}", []).append(t.task_id)
        return locks

    def get_events(self, limit: int = 100) -> list[OrchestratorEvent]:
        return list(self._events[-limit:])

    def get_gantt_data(self) -> list[dict[str, Any]]:
        """Допущение по формату: start/finish=None, если ещё не начато/не завершено (UI подставляет now)."""
        now = datetime.now()
        out = []
        for t in self._tasks.values():
            end = t.finished_at or (now if t.started_at else None)
            out.append({
                "task_id": t.task_id, "task": f"#{t.task_id} {t.process_id}", "process_id": t.process_id,
                "target_date": t.target_date, "trigger": t.trigger.name, "status": t.status.name,
                "queued_at": t.queued_at, "start": t.started_at, "finish": end,
                "wait_sec": ((t.started_at or now) - t.queued_at).total_seconds() if t.queued_at <= now else 0.0,
                "run_sec": (end - t.started_at).total_seconds() if t.started_at and end else 0.0,
            })
        return out

    def get_metrics(self) -> dict[str, Any]:
        ts = list(self._tasks.values())
        done = [t for t in ts if t.status == TaskStatus.COMPLETED and t.started_at and t.finished_at]
        err = lambda name: sum(1 for t in ts if t.status == TaskStatus.FAILED and (t.error_message or "").startswith(name))
        starts = [t.queued_at for t in ts]
        ends = [t.finished_at for t in ts if t.finished_at]
        return {
            "orchestration_enabled": self._enabled, "paused": self._paused, "total": len(ts),
            "queued": sum(t.status == TaskStatus.QUEUED for t in ts),
            "running": sum(t.status == TaskStatus.RUNNING for t in ts),
            "completed": len(done),
            "failed": sum(t.status == TaskStatus.FAILED for t in ts),
            "deadlocks": err("DeadlockError"), "data_corruptions": err("DataCorruptionError"),
            "cross_date_collisions": err("CrossDateCollisionError"),
            "avg_wait_sec": round(sum((t.started_at - t.queued_at).total_seconds() for t in done) / len(done), 2) if done else 0.0,
            "avg_run_sec": round(sum((t.finished_at - t.started_at).total_seconds() for t in done) / len(done), 2) if done else 0.0,
            "makespan_sec": round((max(ends) - min(starts)).total_seconds(), 2) if ends and starts else 0.0,
        }

    # ---------- внутренности ----------
    def _running(self) -> list[TaskRun]:
        return [t for t in self._tasks.values() if t.status == TaskStatus.RUNNING]

    def _conflict(self, a: TaskRun, b: TaskRun) -> str | None:
        """'process:ID' или имя источника, если задачи конфликтуют (READ/WRITE + даты + allow_parallel_dates)."""
        sa, sb = self._specs[a.process_id], self._specs[b.process_id]
        if a.process_id == b.process_id and not sa.allow_parallel_dates:
            return f"process:{a.process_id}"
        if a.target_date != b.target_date:
            return None
        for src in (*sa.write_sources, *sb.write_sources):
            if (src in sa.write_sources and src in (*sb.read_sources, *sb.write_sources)) or \
               (src in sb.write_sources and src in (*sa.read_sources, *sa.write_sources)):
                return src
        return None

    def _first_conflict(self, t: TaskRun, running: list[TaskRun]) -> tuple[TaskRun, str] | None:
        for o in running:
            if (r := self._conflict(t, o)):
                return o, r
        return None

    def _pump(self) -> None:
        if not self._started or self._paused:
            return
        now = datetime.now()
        running = self._running()
        queued = sorted((t for t in self._tasks.values() if t.status == TaskStatus.QUEUED and t.queued_at <= now),
                        key=lambda t: (t.priority, t.queued_at, t.task_id))  # допущение: меньше число = выше приоритет
        changed = False
        for t in queued:
            hit = self._first_conflict(t, running)
            if hit and self._enabled:
                o, r = hit
                why = (f"Ожидает завершения {t.process_id} за другую дату (задача #{o.task_id}, {o.target_date})"
                       if r.startswith("process:")
                       else f"Ожидает освобождения витрины {r} ({t.target_date}), занятой задачей #{o.task_id}")
                if t.blocked_by != why:
                    t.blocked_by, changed = why, True
                continue
            if hit:  # режим «без оркестратора»: стартуем вслепую, задача упадёт на 60%
                self._doom[t.task_id] = (60, self._error_for(hit[1], t, hit[0]))
            t.status, t.started_at, t.blocked_by = TaskStatus.RUNNING, now, None
            running.append(t)
            self._runners[t.task_id] = asyncio.create_task(self._execute(t))
            self._log("INFO", t.task_id, f"Старт {t.process_id} за {t.target_date}")
            changed = True
        if changed:
            self._notify()

    def _error_for(self, reason: str, a: TaskRun, b: TaskRun) -> Exception:
        if reason.startswith("process:"):
            return CrossDateCollisionError(f"staging-буфер {a.process_id} перемешан датами {b.target_date}/{a.target_date}")
        if {a.process_id, b.process_id} == {STOCKOUT, DELAY} and reason in (WAREHOUSE, TRANSIT):
            return DeadlockError(f"AB-BA между #{a.task_id} и #{b.task_id}")
        return DataCorruptionError(f"Lost Update на {reason}: #{a.task_id} и #{b.task_id}")

    async def _execute(self, task: TaskRun) -> None:
        doom = self._doom.pop(task.task_id, None)

        async def on_progress(tid: int, pct: int) -> None:
            task.progress = pct
            if doom and pct >= doom[0]:
                raise doom[1]
            self._notify()

        try:
            task.result = await self._services[task.process_id].run(
                task.task_id, task.target_date, task.trigger, on_progress)
            task.status = TaskStatus.COMPLETED
            self._log("INFO", task.task_id, "Завершено")
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            raise
        except Exception as e:  # noqa: BLE001
            task.status, task.error_message = TaskStatus.FAILED, f"{type(e).__name__}: {e}"
            self._log("ERROR", task.task_id, task.error_message)
        finally:
            task.finished_at = datetime.now()
            self._runners.pop(task.task_id, None)
            self._pump()
            self._notify()

    async def _cancel_runners(self) -> None:
        runners = list(self._runners.values())
        for r in runners:
            r.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        self._runners.clear()

    def _log(self, level: str, task_id: int | None, message: str) -> None:
        self._events.append(OrchestratorEvent(timestamp=datetime.now(), level=level, task_id=task_id, message=message))

    def _notify(self) -> None:
        for cb in self._subs:
            res = cb()
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)

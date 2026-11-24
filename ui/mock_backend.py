"""Standalone mock backend for developing and demoing the UI before core/services merge.

Strictly conforms to Section 5 contracts (OrchestratorProtocol, SchedulerProtocol,
ScenarioSpec, get_demo_scenarios, generate_random_batch).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import date, datetime, timedelta
import inspect
import random
from typing import Any

from ui.contracts import (
    ALL_PROCESS_IDS,
    CrossDateCollisionError,
    DataCorruptionError,
    DeadlockError,
    OrchestratorEvent,
    PROCESS_CARRIER_DEFAULT,
    PROCESS_DELAY,
    PROCESS_SPOILAGE,
    PROCESS_STOCKOUT,
    ProcessSpec,
    SOURCE_SUPPLIER_SLA,
    SOURCE_TRANSIT_TELEMETRY,
    SOURCE_WAREHOUSE_STOCKS,
    SOURCE_WEATHER_TRAFFIC,
    ScenarioSpec,
    StateChangeCallback,
    TaskRequest,
    TaskRun,
    TaskStatus,
    TriggerType,
    classify_error,
    status_key,
)


def default_process_specs() -> list[ProcessSpec]:
    """Return canonical Section 3 logistics risk process specifications."""
    return [
        ProcessSpec(
            process_id=PROCESS_STOCKOUT,
            display_name="StockoutRisk — Риск дефицита",
            read_sources=(SOURCE_SUPPLIER_SLA, SOURCE_TRANSIT_TELEMETRY),
            write_sources=(SOURCE_WAREHOUSE_STOCKS,),
            allow_parallel_dates=False,
            default_priority=1,
        ),
        ProcessSpec(
            process_id=PROCESS_DELAY,
            display_name="DelayRisk — Риск срыва сроков",
            read_sources=(SOURCE_WEATHER_TRAFFIC, SOURCE_SUPPLIER_SLA, SOURCE_WAREHOUSE_STOCKS),
            write_sources=(SOURCE_TRANSIT_TELEMETRY,),
            allow_parallel_dates=False,
            default_priority=1,
        ),
        ProcessSpec(
            process_id=PROCESS_SPOILAGE,
            display_name="SpoilageRisk — Риск порчи/просрочки",
            read_sources=(SOURCE_WEATHER_TRAFFIC, SOURCE_WAREHOUSE_STOCKS),
            write_sources=(SOURCE_WAREHOUSE_STOCKS,),
            allow_parallel_dates=True,
            default_priority=2,
        ),
        ProcessSpec(
            process_id=PROCESS_CARRIER_DEFAULT,
            display_name="CarrierDefaultRisk — Невыход перевозчика",
            read_sources=(SOURCE_TRANSIT_TELEMETRY, SOURCE_WEATHER_TRAFFIC),
            write_sources=(SOURCE_SUPPLIER_SLA,),
            allow_parallel_dates=False,
            default_priority=2,
        ),
    ]


def get_mock_demo_scenarios() -> list[ScenarioSpec]:
    """Return the 4 one-click demo scenarios specified in Section 2."""
    today = date.today()
    today_str = today.isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()

    return [
        ScenarioSpec(
            scenario_id="stress_all",
            title="Запустить всё (Стресс-тест)",
            description=(
                "Шторм из 9 параллельных и пересекающихся задач (регламент + ручные, "
                "разные даты, AB-BA, Lost Update и независимые пары)."
            ),
            tasks=[
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.AUTO, priority=1, delay_sec=0.0),
                TaskRequest(PROCESS_DELAY, today_str, TriggerType.AUTO, priority=1, delay_sec=0.15),
                TaskRequest(PROCESS_SPOILAGE, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.25),
                TaskRequest(PROCESS_CARRIER_DEFAULT, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.3),
                TaskRequest(PROCESS_CARRIER_DEFAULT, yesterday_str, TriggerType.AUTO, priority=1, delay_sec=0.45),
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.6),
                TaskRequest(PROCESS_SPOILAGE, yesterday_str, TriggerType.MANUAL, priority=2, delay_sec=0.75),
                TaskRequest(PROCESS_DELAY, tomorrow_str, TriggerType.MANUAL, priority=3, delay_sec=0.9),
                TaskRequest(PROCESS_STOCKOUT, yesterday_str, TriggerType.AUTO, priority=1, delay_sec=1.05),
            ],
        ),
        ScenarioSpec(
            scenario_id="manual_vs_reglament",
            title="Сценарий 1: Ручной vs Регламент",
            description=(
                "Ночной регламент StockoutRisk сталкивается с ручными запусками "
                "StockoutRisk и SpoilageRisk на витрине WarehouseStocks."
            ),
            tasks=[
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.AUTO, priority=1, delay_sec=0.0),
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.25),
                TaskRequest(PROCESS_SPOILAGE, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.45),
            ],
        ),
        ScenarioSpec(
            scenario_id="source_conflict",
            title="Сценарий 2: Конфликт источников",
            description=(
                "StockoutRisk и DelayRisk конфликтуют по WarehouseStocks и TransitTelemetry "
                "(AB-BA), а независимые расчёты идут параллельно."
            ),
            tasks=[
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.AUTO, priority=1, delay_sec=0.0),
                TaskRequest(PROCESS_DELAY, today_str, TriggerType.MANUAL, priority=1, delay_sec=0.15),
                TaskRequest(PROCESS_CARRIER_DEFAULT, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.3),
                TaskRequest(PROCESS_SPOILAGE, yesterday_str, TriggerType.MANUAL, priority=2, delay_sec=0.4),
            ],
        ),
        ScenarioSpec(
            scenario_id="cross_date_deadlock",
            title="Сценарий 3: Перекрёстные даты (Deadlock-тест)",
            description=(
                "CarrierDefaultRisk за вчера и сегодня (коллизия staging-буфера) + "
                "одновременные StockoutRisk и DelayRisk за одну дату."
            ),
            tasks=[
                TaskRequest(PROCESS_CARRIER_DEFAULT, yesterday_str, TriggerType.AUTO, priority=1, delay_sec=0.0),
                TaskRequest(PROCESS_CARRIER_DEFAULT, today_str, TriggerType.MANUAL, priority=2, delay_sec=0.2),
                TaskRequest(PROCESS_STOCKOUT, today_str, TriggerType.AUTO, priority=1, delay_sec=0.35),
                TaskRequest(PROCESS_DELAY, today_str, TriggerType.MANUAL, priority=1, delay_sec=0.45),
            ],
        ),
    ]


def generate_mock_random_batch(count: int = 8, seed: int | None = None) -> list[TaskRequest]:
    """Generate a synthetic random batch of tasks with date & source overlaps."""
    rng = random.Random(seed)
    today = date.today()
    date_pool = [(today - timedelta(days=offset)).isoformat() for offset in range(3)]
    requests: list[TaskRequest] = []
    for idx in range(max(1, count)):
        pid = rng.choice(ALL_PROCESS_IDS)
        t_date = rng.choice(date_pool)
        trigger = rng.choice([TriggerType.AUTO, TriggerType.MANUAL])
        priority = 1 if trigger == TriggerType.AUTO else rng.choice([1, 2, 3])
        delay = round(idx * rng.uniform(0.1, 0.25), 2)
        requests.append(
            TaskRequest(
                process_id=pid,
                target_date=t_date,
                trigger=trigger,
                priority=priority,
                delay_sec=delay,
            )
        )
    return requests


class _MockLockManager:
    """In-memory deadlock-free all-or-nothing lock manager for standalone UI demos."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        # key -> list of holder task_ids
        self._locks: dict[str, list[int]] = {}
        # task_id -> set of acquired keys
        self._task_keys: dict[int, set[str]] = {}
        # task_id -> (process_id, target_date) for readable blocked_by messages
        self._task_meta: dict[int, tuple[str, str]] = {}

    async def try_acquire(self, task: TaskRun, spec: ProcessSpec) -> tuple[bool, str | None]:
        async with self._guard:
            # 1. Check process-level cross-date exclusion
            proc_key = f"process:{spec.process_id}"
            if not spec.allow_parallel_dates and proc_key in self._locks:
                holder = self._locks[proc_key][0]
                holder_proc, holder_date = self._task_meta.get(holder, (spec.process_id, "?"))
                return (
                    False,
                    f"Ожидает завершения {holder_proc} (#{holder}, дата {holder_date}): "
                    f"запрещён параллельный запуск процесса (staging-буфер)",
                )

            # 2. Check WRITE sources (exclusive: conflict with any READ or WRITE on same source+date)
            for src in spec.write_sources:
                write_key = f"WRITE:{src}:{task.target_date}"
                read_key = f"READ:{src}:{task.target_date}"
                if write_key in self._locks:
                    holder = self._locks[write_key][0]
                    holder_proc = self._task_meta.get(holder, ("?", "?"))[0]
                    return (
                        False,
                        f"Ожидает освобождения витрины {src} ({task.target_date}), "
                        f"занятой на запись задачей #{holder} ({holder_proc})",
                    )
                if read_key in self._locks:
                    holder = self._locks[read_key][0]
                    holder_proc = self._task_meta.get(holder, ("?", "?"))[0]
                    return (
                        False,
                        f"Ожидает завершения чтения витрины {src} ({task.target_date}) "
                        f"задачей #{holder} ({holder_proc})",
                    )

            # 3. Check READ sources (shared: conflict ONLY with WRITE on same source+date)
            write_set = set(spec.write_sources)
            for src in spec.read_sources:
                if src in write_set:
                    continue
                write_key = f"WRITE:{src}:{task.target_date}"
                if write_key in self._locks:
                    holder = self._locks[write_key][0]
                    holder_proc = self._task_meta.get(holder, ("?", "?"))[0]
                    return (
                        False,
                        f"Ожидает освобождения витрины {src} ({task.target_date}), "
                        f"перезаписываемой задачей #{holder} ({holder_proc})",
                    )

            # 4. All resources available — acquire atomically!
            acquired: set[str] = set()
            if not spec.allow_parallel_dates:
                self._locks.setdefault(proc_key, []).append(task.task_id)
                acquired.add(proc_key)
            for src in spec.write_sources:
                write_key = f"WRITE:{src}:{task.target_date}"
                self._locks.setdefault(write_key, []).append(task.task_id)
                acquired.add(write_key)
            for src in spec.read_sources:
                if src in write_set:
                    continue
                read_key = f"READ:{src}:{task.target_date}"
                self._locks.setdefault(read_key, []).append(task.task_id)
                acquired.add(read_key)

            self._task_keys[task.task_id] = acquired
            self._task_meta[task.task_id] = (task.process_id, task.target_date)
            return True, None

    async def release(self, task_id: int) -> None:
        async with self._guard:
            keys = self._task_keys.pop(task_id, set())
            self._task_meta.pop(task_id, None)
            for key in keys:
                holders = self._locks.get(key)
                if not holders:
                    continue
                if task_id in holders:
                    holders.remove(task_id)
                if not holders:
                    self._locks.pop(key, None)

    def get_snapshot(self) -> dict[str, list[int]]:
        return {k: list(v) for k, v in self._locks.items()}

    def reset(self) -> None:
        self._locks.clear()
        self._task_keys.clear()
        self._task_meta.clear()


class MockOrchestrator:
    """Realistic in-memory orchestrator matching Section 5.4 Orchestrator contract."""

    _BASE_DURATIONS: dict[str, float] = {
        PROCESS_STOCKOUT: 4.0,
        PROCESS_DELAY: 4.5,
        PROCESS_SPOILAGE: 3.5,
        PROCESS_CARRIER_DEFAULT: 4.2,
    }

    def __init__(self) -> None:
        self._specs_list = default_process_specs()
        self._specs: dict[str, ProcessSpec] = {s.process_id: s for s in self._specs_list}
        self._lock_manager = _MockLockManager()
        self._orchestration_enabled = True
        self._paused = False
        self._running = False
        self._next_task_id = 1
        self._tasks: dict[int, TaskRun] = {}
        self._events: list[OrchestratorEvent] = []
        self._subscribers: list[StateChangeCallback] = []
        self._wake_event = asyncio.Event()
        self._dispatch_loop_task: asyncio.Task[None] | None = None
        self._worker_tasks: set[asyncio.Task[Any]] = set()
        self._max_parallelism = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._wake_event = asyncio.Event()
        self._lock_manager._guard = asyncio.Lock()
        self._dispatch_loop_task = asyncio.create_task(self._scheduler_loop(), name="mock-orch-loop")
        self._add_event("INFO", None, "Оркестратор запущен (Mock Demo Engine готов к работе).")
        await self._notify()

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        current_loop = asyncio.get_running_loop()
        if self._dispatch_loop_task is not None:
            self._dispatch_loop_task.cancel()
            if self._dispatch_loop_task.get_loop() is current_loop:
                try:
                    await self._dispatch_loop_task
                except asyncio.CancelledError:
                    pass
            self._dispatch_loop_task = None
        same_loop_workers = []
        for wt in list(self._worker_tasks):
            wt.cancel()
            if wt.get_loop() is current_loop:
                same_loop_workers.append(wt)
        if same_loop_workers:
            await asyncio.gather(*same_loop_workers, return_exceptions=True)
        self._worker_tasks.clear()

    def set_orchestration_enabled(self, enabled: bool) -> None:
        self._orchestration_enabled = bool(enabled)
        mode_text = (
            "ВКЛЮЧЁН (приоритетная очередь + LockManager защищают витрины)"
            if self._orchestration_enabled
            else "ОТКЛЮЧЁН (режим хаоса «как было»: прямые параллельные запуски)"
        )
        self._add_event("WARNING" if not enabled else "INFO", None, f"Режим оркестрации: {mode_text}")
        self._wake_event.set()
        self._schedule_notify()

    def is_orchestration_enabled(self) -> bool:
        return self._orchestration_enabled

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._add_event("WARNING", None, "Планировщик поставлен на паузу. Новые задачи ждут в очереди.")
            self._schedule_notify()

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._add_event("INFO", None, "Планировщик возобновил распределение очереди.")
            self._wake_event.set()
            self._schedule_notify()

    async def reset(self) -> None:
        for wt in list(self._worker_tasks):
            wt.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._lock_manager.reset()
        self._tasks.clear()
        self._events.clear()
        self._next_task_id = 1
        self._paused = False
        self._max_parallelism = 0
        self._add_event("INFO", None, "Состояние оркестратора, очередь, блокировки и журнал сброшены.")
        await self._notify()

    def subscribe(self, callback: StateChangeCallback) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    async def submit(self, request: TaskRequest) -> TaskRun:
        if not self._running:
            await self.start()

        spec = self._specs.get(request.process_id)
        if spec is None:
            raise ValueError(f"Unknown process_id: {request.process_id}")

        priority = request.priority if request.priority is not None else spec.default_priority
        task_id = self._next_task_id
        self._next_task_id += 1

        task = TaskRun(
            task_id=task_id,
            process_id=request.process_id,
            target_date=request.target_date,
            trigger=request.trigger,
            priority=priority,
            status=TaskStatus.QUEUED,
            progress=0,
            queued_at=datetime.now(),
        )
        self._tasks[task_id] = task
        self._add_event(
            "INFO",
            task_id,
            f"Задача #{task_id} ({task.process_id}, дата {task.target_date}, "
            f"{task.trigger.value}, P{task.priority}) принята в систему.",
        )

        if not self._orchestration_enabled:
            self._spawn_worker(task, orchestrated=False)
        else:
            self._wake_event.set()

        await self._notify()
        return task

    async def submit_batch(self, requests: list[TaskRequest]) -> list[TaskRun]:
        if not self._running:
            await self.start()

        immediate: list[TaskRun] = []
        for req in requests:
            if req.delay_sec and req.delay_sec > 0:
                wt = asyncio.create_task(self._delayed_submit(req))
                self._worker_tasks.add(wt)
                wt.add_done_callback(self._worker_tasks.discard)
            else:
                immediate.append(await self.submit(req))
        return immediate

    async def _delayed_submit(self, req: TaskRequest) -> None:
        try:
            await asyncio.sleep(req.delay_sec)
            await self.submit(
                TaskRequest(
                    process_id=req.process_id,
                    target_date=req.target_date,
                    trigger=req.trigger,
                    priority=req.priority,
                    delay_sec=0.0,
                )
            )
        except asyncio.CancelledError:
            return

    def get_process_specs(self) -> list[ProcessSpec]:
        return list(self._specs_list)

    def get_tasks(self, status: TaskStatus | None = None) -> list[TaskRun]:
        tasks = list(self._tasks.values())
        if status is not None:
            target = status_key(status)
            tasks = [t for t in tasks if status_key(t.status) == target]
        return tasks

    def get_active_locks(self) -> dict[str, list[int]]:
        return self._lock_manager.get_snapshot()

    def get_events(self, limit: int = 100) -> list[OrchestratorEvent]:
        if limit <= 0:
            return []
        return list(self._events[-limit:])

    def get_gantt_data(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for t in self._tasks.values():
            rows.append(
                {
                    "task_id": t.task_id,
                    "process_id": t.process_id,
                    "target_date": t.target_date,
                    "trigger": t.trigger.value,
                    "priority": t.priority,
                    "status": t.status.value,
                    "progress": t.progress,
                    "queued_at": t.queued_at,
                    "started_at": t.started_at,
                    "finished_at": t.finished_at,
                    "blocked_by": t.blocked_by,
                    "error_message": t.error_message,
                }
            )
        return rows

    def get_metrics(self) -> dict[str, Any]:
        tasks = list(self._tasks.values())
        total = len(tasks)
        running = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
        cancelled = sum(1 for t in tasks if t.status == TaskStatus.CANCELLED)

        wait_times = [
            max(0.0, (t.started_at - t.queued_at).total_seconds())
            for t in tasks
            if t.started_at is not None
        ]
        run_times = [
            max(0.0, (t.finished_at - t.started_at).total_seconds())
            for t in tasks
            if t.started_at is not None and t.finished_at is not None
        ]

        deadlocks = sum(1 for t in tasks if t.status == TaskStatus.FAILED and classify_error(t.error_message) == "deadlock")
        corruptions = sum(1 for t in tasks if t.status == TaskStatus.FAILED and classify_error(t.error_message) == "corruption")
        collisions = sum(1 for t in tasks if t.status == TaskStatus.FAILED and classify_error(t.error_message) == "cross_date")

        finished_total = completed + failed
        success_rate = (completed / finished_total * 100.0) if finished_total > 0 else 100.0

        return {
            "total": total,
            "running": running,
            "queued": queued,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "deadlocks": deadlocks,
            "corruptions": corruptions,
            "cross_date_collisions": collisions,
            "avg_wait_sec": round(sum(wait_times) / len(wait_times), 2) if wait_times else 0.0,
            "avg_run_sec": round(sum(run_times) / len(run_times), 2) if run_times else 0.0,
            "success_rate": round(success_rate, 1),
            "active_locks": len(self._lock_manager.get_snapshot()),
            "max_parallelism": self._max_parallelism,
            "orchestration_enabled": self._orchestration_enabled,
        }

    async def _scheduler_loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.4)
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()

            if self._paused:
                continue

            await self._dispatch_queued_tasks()

    async def _dispatch_queued_tasks(self) -> None:
        queued = [t for t in self._tasks.values() if t.status == TaskStatus.QUEUED]
        if not queued:
            return

        queued.sort(key=lambda t: (t.priority, t.queued_at, t.task_id))
        changed = False

        for task in queued:
            if not self._orchestration_enabled:
                self._spawn_worker(task, orchestrated=False)
                changed = True
                continue

            spec = self._specs[task.process_id]
            acquired, reason = await self._lock_manager.try_acquire(task, spec)
            if acquired:
                task.blocked_by = None
                self._spawn_worker(task, orchestrated=True)
                changed = True
            else:
                if task.blocked_by != reason:
                    task.blocked_by = reason
                    self._add_event("WARNING", task.task_id, f"Задача #{task.task_id} ({task.process_id}) в ожидании: {reason}")
                    changed = True

        if changed:
            await self._notify()

    def _spawn_worker(self, task: TaskRun, *, orchestrated: bool) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        task.blocked_by = None
        running_now = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
        if running_now > self._max_parallelism:
            self._max_parallelism = running_now

        mode_tag = "с блокировками LockManager" if orchestrated else "НАПРЯМУЮ (без оркестратора)"
        self._add_event(
            "INFO",
            task.task_id,
            f"Старт #{task.task_id} ({task.process_id}, {task.target_date}) {mode_tag}.",
        )
        wt = asyncio.create_task(self._run_worker(task, orchestrated=orchestrated))
        self._worker_tasks.add(wt)
        wt.add_done_callback(self._worker_tasks.discard)

    async def _run_worker(self, task: TaskRun, *, orchestrated: bool) -> None:
        duration = self._BASE_DURATIONS.get(task.process_id, 4.0)
        steps = 8
        step_sleep = duration / steps

        try:
            for step in range(1, steps + 1):
                await asyncio.sleep(step_sleep)
                if not orchestrated and step in (2, 3, 4):
                    collision_exc = self._detect_chaos_collision(task)
                    if collision_exc is not None:
                        raise collision_exc
                task.progress = min(100, int(step / steps * 100))
                await self._notify()

            task.progress = 100
            task.status = TaskStatus.COMPLETED
            task.finished_at = datetime.now()
            task.result = {
                "risk_score": round(0.18 + (task.task_id * 17 % 73) / 100.0, 2),
                "records_processed": 1200 + (task.task_id * 137) % 900,
                "status": "ok",
            }
            self._add_event(
                "SUCCESS",
                task.task_id,
                f"Задача #{task.task_id} ({task.process_id}, {task.target_date}) успешно завершена "
                f"(risk_score={task.result['risk_score']}).",
            )
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            task.finished_at = datetime.now()
            raise
        except (DeadlockError, DataCorruptionError, CrossDateCollisionError) as exc:
            task.status = TaskStatus.FAILED
            task.finished_at = datetime.now()
            task.error_message = f"{type(exc).__name__}: {exc}"
            self._add_event("ERROR", task.task_id, f"СБОЙ #{task.task_id} ({task.process_id}): {task.error_message}")
        finally:
            if orchestrated:
                await self._lock_manager.release(task.task_id)
                self._wake_event.set()
            await self._notify()

    def _detect_chaos_collision(self, task: TaskRun) -> Exception | None:
        """Detect AB-BA deadlock, Cross-Date staging collision, or Lost Update in chaos mode."""
        spec = self._specs[task.process_id]
        others = [
            t
            for t in self._tasks.values()
            if t.task_id != task.task_id and t.status == TaskStatus.RUNNING
        ]
        if not others:
            return None

        # 1. AB-BA Deadlock: StockoutRisk + DelayRisk on same date
        for other in others:
            pair = {task.process_id, other.process_id}
            if pair == {PROCESS_STOCKOUT, PROCESS_DELAY} and task.target_date == other.target_date:
                return DeadlockError(
                    f"AB-BA Deadlock между StockoutRisk и DelayRisk за {task.target_date}: "
                    f"циклическая блокировка WarehouseStocks ↔ TransitTelemetry (конфликт с #{other.task_id})"
                )

        # 2. Cross-Date Collision in staging buffer
        if not spec.allow_parallel_dates:
            for other in others:
                if other.process_id == task.process_id and other.target_date != task.target_date:
                    return CrossDateCollisionError(
                        f"Коллизия staging-буфера {task.process_id}: параллельный расчёт за "
                        f"{task.target_date} и {other.target_date} (задача #{other.task_id}) перемешал данные"
                    )

        # 3. Lost Update / Dirty Read on shared data mart for same date
        my_writes = set(spec.write_sources)
        my_reads = set(spec.read_sources)
        for other in others:
            if other.target_date != task.target_date:
                continue
            other_spec = self._specs[other.process_id]
            other_writes = set(other_spec.write_sources)
            other_reads = set(other_spec.read_sources)
            ww = my_writes & other_writes
            if ww:
                src = sorted(ww)[0]
                return DataCorruptionError(
                    f"Lost Update витрины {src} ({task.target_date}): одновременная запись "
                    f"с задачей #{other.task_id} ({other.process_id}) перезатёрла результаты"
                )
            rw = (my_reads & other_writes) | (my_writes & other_reads)
            if rw:
                src = sorted(rw)[0]
                return DataCorruptionError(
                    f"Грязное чтение / порча данных витрины {src} ({task.target_date}): "
                    f"конфликт READ/WRITE с задачей #{other.task_id} ({other.process_id})"
                )
        return None

    def _add_event(self, level: str, task_id: int | None, message: str) -> None:
        self._events.append(
            OrchestratorEvent(
                timestamp=datetime.now(),
                level=level,
                task_id=task_id,
                message=message,
            )
        )
        if len(self._events) > 500:
            self._events = self._events[-500:]

    def _schedule_notify(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify())
        except RuntimeError:
            pass

    async def _notify(self) -> None:
        for cb in list(self._subscribers):
            try:
                res = cb()
                if inspect.isawaitable(res):
                    await res  # type: ignore[misc]
            except Exception:
                # Never allow a broken UI callback to crash the orchestrator loop
                continue


class MockReglamentScheduler:
    """Mock background scheduler implementing Section 5.4 ReglamentScheduler contract."""

    def __init__(self, orchestrator: MockOrchestrator, interval: float = 15.0) -> None:
        self._orchestrator = orchestrator
        self._interval = max(2.0, float(interval))
        self._enabled = False
        self._cycle_index = 0
        self._task: asyncio.Task[None] | None = None

    def is_enabled(self) -> bool:
        return self._enabled

    def get_interval(self) -> float:
        return self._interval

    def set_interval(self, seconds: float) -> None:
        self._interval = max(2.0, float(seconds))

    def set_enabled(self, enabled: bool) -> None:
        new_state = bool(enabled)
        if new_state == self._enabled:
            return
        self._enabled = new_state
        if self._enabled:
            self._start_loop()
        else:
            self._stop_loop()

    def _start_loop(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._loop(), name="mock-reglament-scheduler")
        except RuntimeError:
            self._task = None

    def _stop_loop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def stop(self) -> None:
        self._enabled = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        today_str = date.today().isoformat()
        cycle = [
            (PROCESS_STOCKOUT, today_str),
            (PROCESS_DELAY, today_str),
            (PROCESS_CARRIER_DEFAULT, today_str),
            (PROCESS_SPOILAGE, today_str),
        ]
        try:
            while self._enabled:
                pid, t_date = cycle[self._cycle_index % len(cycle)]
                self._cycle_index += 1
                await self._orchestrator.submit(
                    TaskRequest(
                        process_id=pid,
                        target_date=t_date,
                        trigger=TriggerType.AUTO,
                        priority=1,
                    )
                )
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return


__all__ = [
    "MockOrchestrator",
    "MockReglamentScheduler",
    "default_process_specs",
    "generate_mock_random_batch",
    "get_mock_demo_scenarios",
]

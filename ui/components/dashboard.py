"""Live KPI metrics and 3-column task dashboard (RUNNING, QUEUED, COMPLETED/FAILED)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nicegui import ui

from ui.contracts import (
    OrchestratorProtocol,
    ProcessSpec,
    TaskRun,
    classify_error,
    status_key,
    trigger_key,
)


@dataclass
class RunSnapshot:
    """Saved summary of a run in either orchestrated or chaos mode for Before/After comparison."""

    orchestration_enabled: bool
    total: int
    completed: int
    failed: int
    deadlocks: int
    corruptions: int
    cross_date: int
    success_rate: float
    avg_wait_sec: float
    avg_run_sec: float
    captured_at: str


def _fmt_sec(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{max(0.0, seconds):.1f}с"


def _compute_metrics(
    tasks: list[TaskRun],
    backend_metrics: dict[str, Any],
    active_locks_count: int,
) -> dict[str, Any]:
    """Combine backend get_metrics() with deterministic fallbacks computed from TaskRun list."""
    running = [t for t in tasks if status_key(t.status) == "RUNNING"]
    queued = [t for t in tasks if status_key(t.status) == "QUEUED"]
    completed = [t for t in tasks if status_key(t.status) == "COMPLETED"]
    failed = [t for t in tasks if status_key(t.status) == "FAILED"]
    cancelled = [t for t in tasks if status_key(t.status) == "CANCELLED"]

    wait_secs = [
        max(0.0, (t.started_at - t.queued_at).total_seconds())
        for t in tasks
        if t.started_at is not None and t.queued_at is not None
    ]
    run_secs = [
        max(0.0, (t.finished_at - t.started_at).total_seconds())
        for t in tasks
        if t.started_at is not None and t.finished_at is not None
    ]

    deadlocks = sum(1 for t in failed if classify_error(t.error_message) == "deadlock")
    corruptions = sum(1 for t in failed if classify_error(t.error_message) == "corruption")
    cross_date = sum(1 for t in failed if classify_error(t.error_message) == "cross_date")

    finished_n = len(completed) + len(failed)
    calc_success_rate = (len(completed) / finished_n * 100.0) if finished_n > 0 else 100.0

    def _pick(keys: tuple[str, ...], fallback: Any) -> Any:
        for k in keys:
            if k in backend_metrics and backend_metrics[k] is not None:
                return backend_metrics[k]
        return fallback

    return {
        "total": len(tasks),
        "running": len(running),
        "queued": len(queued),
        "completed": len(completed),
        "failed": len(failed),
        "cancelled": len(cancelled),
        "deadlocks": int(_pick(("deadlocks", "deadlock_count"), deadlocks)),
        "corruptions": int(_pick(("corruptions", "data_corruptions", "lost_updates"), corruptions)),
        "cross_date": int(_pick(("cross_date_collisions", "collisions"), cross_date)),
        "success_rate": float(_pick(("success_rate",), round(calc_success_rate, 1))),
        "avg_wait_sec": float(
            _pick(
                ("avg_wait_sec", "avg_wait_time", "mean_wait_sec"),
                round(sum(wait_secs) / len(wait_secs), 2) if wait_secs else 0.0,
            )
        ),
        "avg_run_sec": float(
            _pick(
                ("avg_run_sec", "avg_run_time", "mean_run_sec"),
                round(sum(run_secs) / len(run_secs), 2) if run_secs else 0.0,
            )
        ),
        "active_locks": active_locks_count,
        "max_parallelism": int(_pick(("max_parallelism",), len(running))),
    }


class Dashboard:
    """Renders KPI summary row, optional Before/After comparison bar, and 3 task columns."""

    def __init__(self, orchestrator: OrchestratorProtocol) -> None:
        self._orchestrator = orchestrator
        self._specs: dict[str, ProcessSpec] = {
            s.process_id: s for s in orchestrator.get_process_specs()
        }
        self._done_filter: str = "ALL"  # ALL | COMPLETED | FAILED
        self._snapshots: dict[str, RunSnapshot] = {}

        # KPI labels updated in-place
        self._kpi_total: Any = None
        self._kpi_running: Any = None
        self._kpi_running_sub: Any = None
        self._kpi_queued: Any = None
        self._kpi_completed: Any = None
        self._kpi_completed_sub: Any = None
        self._kpi_failed: Any = None
        self._kpi_failed_sub: Any = None
        self._kpi_timing: Any = None
        self._kpi_timing_sub: Any = None

        # Column headers & containers
        self._running_badge: Any = None
        self._queued_badge: Any = None
        self._done_badge: Any = None
        self._done_filter_btns: dict[str, Any] = {}
        self._running_list: Any = None
        self._queued_list: Any = None
        self._done_list: Any = None
        self._comparison_box: Any = None

        self._last_running_fp: Any = None
        self._last_queued_fp: Any = None
        self._last_done_fp: Any = None

    def capture_snapshot(self) -> None:
        """Capture current run statistics into the Before/After comparison bar."""
        tasks = self._orchestrator.get_tasks()
        if not tasks:
            return
        finished = [t for t in tasks if status_key(t.status) in ("COMPLETED", "FAILED")]
        if not finished:
            return
        metrics = _compute_metrics(
            tasks,
            self._orchestrator.get_metrics() or {},
            len(self._orchestrator.get_active_locks() or {}),
        )
        enabled = self._orchestrator.is_orchestration_enabled()
        key = "with" if enabled else "without"
        self._snapshots[key] = RunSnapshot(
            orchestration_enabled=enabled,
            total=metrics["total"],
            completed=metrics["completed"],
            failed=metrics["failed"],
            deadlocks=metrics["deadlocks"],
            corruptions=metrics["corruptions"],
            cross_date=metrics["cross_date"],
            success_rate=metrics["success_rate"],
            avg_wait_sec=metrics["avg_wait_sec"],
            avg_run_sec=metrics["avg_run_sec"],
            captured_at=datetime.now().strftime("%H:%M:%S"),
        )
        self._render_comparison()

    def clear_snapshots(self) -> None:
        self._snapshots.clear()
        self._render_comparison()

    def build(self) -> None:
        with ui.column().classes("w-full gap-4"):
            # KPI row
            with ui.element("div").classes(
                "w-full grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3"
            ):
                self._kpi_total = self._make_kpi(
                    "Всего задач", "0", "За текущую сессию", "layers", "text-slate-700 dark:text-slate-200"
                )[0]
                self._kpi_running, self._kpi_running_sub = self._make_kpi(
                    "Выполняются", "0", "Макс. параллельно: 0", "play_circle", "text-blue-600 dark:text-blue-400"
                )
                self._kpi_queued = self._make_kpi(
                    "В очереди", "0", "Ожидают блокировок", "hourglass_top", "text-amber-600 dark:text-amber-400"
                )[0]
                self._kpi_completed, self._kpi_completed_sub = self._make_kpi(
                    "Завершено", "0", "Успешность: 100%", "check_circle", "text-emerald-600 dark:text-emerald-400"
                )
                self._kpi_failed, self._kpi_failed_sub = self._make_kpi(
                    "Сбои / Ошибки", "0", "DL: 0 · Corr: 0 · Date: 0", "error", "text-red-600 dark:text-red-400"
                )
                self._kpi_timing, self._kpi_timing_sub = self._make_kpi(
                    "Ср. ожидание", "0.0с", "Ср. расчёт: 0.0с", "timer", "text-indigo-600 dark:text-indigo-400"
                )

            # Session Before/After comparison bar (auto-populates after reset/mode switch)
            self._comparison_box = ui.column().classes("w-full")
            self._render_comparison()

            # 3-Column Live Task Board
            with ui.element("div").classes("w-full grid grid-cols-1 lg:grid-cols-3 gap-4 items-start"):
                # Column 1: RUNNING
                with ui.column().classes("ro-card p-3.5 w-full gap-3"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-2"):
                            ui.element("span").classes("ro-pulse-dot bg-blue-600")
                            ui.label("Выполняются сейчас").classes("text-sm font-semibold")
                            ui.label("RUNNING").classes("text-[10px] font-bold px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300")
                        self._running_badge = ui.badge("0", color="primary").props("rounded")
                    self._running_list = ui.column().classes("w-full gap-2.5 ro-column-scroll")

                # Column 2: QUEUED
                with ui.column().classes("ro-card p-3.5 w-full gap-3"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-2"):
                            ui.element("span").classes("w-2.5 h-2.5 rounded-full bg-amber-500 inline-block")
                            ui.label("Очередь ожидания").classes("text-sm font-semibold")
                            ui.label("QUEUED").classes("text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300")
                        self._queued_badge = ui.badge("0", color="warning").props("rounded")
                    self._queued_list = ui.column().classes("w-full gap-2.5 ro-column-scroll")

                # Column 3: COMPLETED / FAILED
                with ui.column().classes("ro-card p-3.5 w-full gap-3"):
                    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                        with ui.row().classes("items-center gap-2"):
                            ui.element("span").classes("w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block")
                            ui.label("Завершённые и сбои").classes("text-sm font-semibold")
                        with ui.row().classes("items-center gap-1"):
                            for flt_key, flt_lbl in (("ALL", "Все"), ("COMPLETED", "OK"), ("FAILED", "Сбои")):
                                btn = ui.button(
                                    flt_lbl,
                                    on_click=lambda k=flt_key: self._set_done_filter(k),
                                ).props("flat dense size=xs no-caps")
                                self._done_filter_btns[flt_key] = btn
                            self._update_done_filter_styles()
                            self._done_badge = ui.badge("0", color="positive").props("rounded")
                    self._done_list = ui.column().classes("w-full gap-2.5 ro-column-scroll")

        self.refresh(force=True)

    def _make_kpi(
        self, title: str, value: str, subtitle: str, icon: str, accent_cls: str
    ) -> tuple[Any, Any]:
        with ui.column().classes("ro-kpi-card p-3 gap-0.5"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(title).classes("text-xs font-medium text-slate-500")
                ui.icon(icon, size="xs").classes(accent_cls)
            val_lbl = ui.label(value).classes(f"text-2xl font-bold leading-tight {accent_cls}")
            sub_lbl = ui.label(subtitle).classes("text-[11px] text-slate-500 truncate w-full")
        return val_lbl, sub_lbl

    def _update_done_filter_styles(self) -> None:
        for key, btn in self._done_filter_btns.items():
            if key == self._done_filter:
                btn.classes(
                    replace="px-2 text-xs font-semibold rounded bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                )
            else:
                btn.classes(replace="px-1.5 text-xs text-slate-500 dark:text-slate-400")

    def _set_done_filter(self, flt: str) -> None:
        self._done_filter = flt
        self._update_done_filter_styles()
        self._last_done_fp = None
        self.refresh(force=True)

    def _render_comparison(self) -> None:
        if self._comparison_box is None:
            return
        self._comparison_box.clear()
        if not self._snapshots:
            return
        with self._comparison_box:
            with ui.column().classes("ro-card p-3.5 w-full gap-2"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("compare", size="xs").classes("text-indigo-600")
                        ui.label("Сравнение прогонов в текущей сессии (До / После)").classes(
                            "text-xs font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300"
                        )
                    ui.button(
                        "Скрыть",
                        on_click=self.clear_snapshots,
                    ).props("flat dense size=xs no-caps color=grey")

                with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-2 gap-3"):
                    without = self._snapshots.get("without")
                    with_orch = self._snapshots.get("with")

                    with ui.row().classes("ro-mode-banner-chaos px-3 py-2 items-center justify-between flex-wrap gap-2"):
                        ui.label("БЕЗ оркестратора (как было)").classes("text-xs font-bold text-red-700 dark:text-red-300")
                        if without is None:
                            ui.label("Прогон ещё не зафиксирован").classes("text-xs text-slate-500")
                        else:
                            ui.label(
                                f"Успех: {without.success_rate:.0f}% · Сбои: {without.failed}/{without.total} "
                                f"(DL: {without.deadlocks}, LostUpd: {without.corruptions}, Date: {without.cross_date}) · {without.captured_at}"
                            ).classes("text-xs font-medium text-red-800 dark:text-red-200")

                    with ui.row().classes("ro-mode-banner-safe px-3 py-2 items-center justify-between flex-wrap gap-2"):
                        ui.label("С оркестратором (LockManager)").classes("text-xs font-bold text-emerald-700 dark:text-emerald-300")
                        if with_orch is None:
                            ui.label("Прогон ещё не зафиксирован").classes("text-xs text-slate-500")
                        else:
                            ui.label(
                                f"Успех: {with_orch.success_rate:.0f}% · Сбои: {with_orch.failed}/{with_orch.total} · "
                                f"Ср. ожидание: {with_orch.avg_wait_sec:.1f}с · {with_orch.captured_at}"
                            ).classes("text-xs font-medium text-emerald-800 dark:text-emerald-200")

    def refresh(self, *, force: bool = False) -> None:
        """Refresh KPIs and all 3 task columns from orchestrator read-only state."""
        tasks = self._orchestrator.get_tasks()
        active_locks = self._orchestrator.get_active_locks() or {}
        backend_metrics = self._orchestrator.get_metrics() or {}
        metrics = _compute_metrics(tasks, backend_metrics, len(active_locks))
        now = datetime.now()

        # Refresh specs cache if backend dynamically updated them
        if not self._specs:
            self._specs = {s.process_id: s for s in self._orchestrator.get_process_specs()}

        # 1. Update KPI labels in place
        if self._kpi_total is not None:
            self._kpi_total.text = str(metrics["total"])
            self._kpi_running.text = str(metrics["running"])
            self._kpi_running_sub.text = (
                f"Макс. параллельно: {metrics['max_parallelism']} · Локов: {metrics['active_locks']}"
            )
            self._kpi_queued.text = str(metrics["queued"])
            self._kpi_completed.text = str(metrics["completed"])
            self._kpi_completed_sub.text = f"Успешность: {metrics['success_rate']:.0f}%"
            self._kpi_failed.text = str(metrics["failed"])
            self._kpi_failed_sub.text = (
                f"DL: {metrics['deadlocks']} · LostUpd: {metrics['corruptions']} · Date: {metrics['cross_date']}"
            )
            self._kpi_timing.text = _fmt_sec(metrics["avg_wait_sec"])
            self._kpi_timing_sub.text = f"Ср. расчёт: {_fmt_sec(metrics['avg_run_sec'])}"

        running_tasks = [t for t in tasks if status_key(t.status) == "RUNNING"]
        running_tasks.sort(key=lambda t: (t.started_at or t.queued_at, t.task_id))

        queued_tasks = [t for t in tasks if status_key(t.status) == "QUEUED"]
        queued_tasks.sort(key=lambda t: (t.priority, t.queued_at, t.task_id))

        done_tasks = [
            t for t in tasks if status_key(t.status) in ("COMPLETED", "FAILED", "CANCELLED")
        ]
        done_tasks.sort(
            key=lambda t: (t.finished_at or t.started_at or t.queued_at, t.task_id),
            reverse=True,
        )

        if self._running_badge is not None:
            self._running_badge.text = str(len(running_tasks))
        if self._queued_badge is not None:
            self._queued_badge.text = str(len(queued_tasks))
        if self._done_badge is not None:
            self._done_badge.text = str(len(done_tasks))

        # 2. Render RUNNING column if changed
        running_fp = [
            (t.task_id, t.progress, int((now - (t.started_at or now)).total_seconds()))
            for t in running_tasks
        ]
        if force or running_fp != self._last_running_fp:
            self._last_running_fp = running_fp
            self._render_running_column(running_tasks, now)

        # 3. Render QUEUED column if changed
        queued_fp = [
            (t.task_id, t.priority, t.blocked_by, int((now - t.queued_at).total_seconds()))
            for t in queued_tasks
        ]
        if force or queued_fp != self._last_queued_fp:
            self._last_queued_fp = queued_fp
            self._render_queued_column(queued_tasks, now)

        # 4. Render COMPLETED / FAILED column if changed
        filtered_done = done_tasks
        if self._done_filter == "COMPLETED":
            filtered_done = [t for t in done_tasks if status_key(t.status) == "COMPLETED"]
        elif self._done_filter == "FAILED":
            filtered_done = [t for t in done_tasks if status_key(t.status) == "FAILED"]

        done_fp = (
            self._done_filter,
            [(t.task_id, status_key(t.status), t.error_message) for t in filtered_done],
        )
        if force or done_fp != self._last_done_fp:
            self._last_done_fp = done_fp
            self._render_done_column(filtered_done)

    def _render_running_column(self, tasks: list[TaskRun], now: datetime) -> None:
        if self._running_list is None:
            return
        self._running_list.clear()
        with self._running_list:
            if not tasks:
                with ui.column().classes("w-full items-center py-8 text-slate-400 gap-1"):
                    ui.icon("hourglass_empty", size="md")
                    ui.label("Нет активных расчётов").classes("text-xs")
                return

            for task in tasks:
                spec = self._specs.get(task.process_id)
                elapsed = (now - task.started_at).total_seconds() if task.started_at else 0.0
                trig = trigger_key(task.trigger)
                pct = max(0, min(100, int(task.progress)))

                with ui.column().classes("ro-task-card ro-task-running p-3 w-full gap-2"):
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                        with ui.row().classes("items-center gap-1.5 min-w-0"):
                            ui.label(f"#{task.task_id}").classes("text-xs font-bold ro-mono text-blue-600")
                            ui.label(task.process_id).classes("text-sm font-semibold truncate")
                        with ui.row().classes("items-center gap-1 shrink-0"):
                            ui.badge(
                                trig,
                                color="purple" if trig == "AUTO" else "indigo",
                            ).props("outline")
                            ui.badge(f"P{task.priority}", color="primary").props("outline")

                    with ui.row().classes("w-full items-center justify-between text-xs text-slate-500"):
                        ui.label(f"Дата: {task.target_date}").classes("ro-mono")
                        ui.label(f"В работе: {_fmt_sec(elapsed)} · {pct}%").classes("font-medium text-blue-600")

                    ui.linear_progress(value=pct / 100.0, show_value=False, color="primary").props(
                        "rounded stripe size=8px"
                    )

                    if spec is not None:
                        with ui.row().classes("w-full items-center gap-1 flex-wrap"):
                            for src in spec.write_sources:
                                ui.label(f"W:{src}").classes(
                                    "text-[10px] px-1.5 py-0.5 rounded ro-source-pill-write ro-mono"
                                )
                            for src in spec.read_sources:
                                if src not in spec.write_sources:
                                    ui.label(f"R:{src}").classes(
                                        "text-[10px] px-1.5 py-0.5 rounded ro-source-pill-read ro-mono"
                                    )

    def _render_queued_column(self, tasks: list[TaskRun], now: datetime) -> None:
        if self._queued_list is None:
            return
        self._queued_list.clear()
        with self._queued_list:
            if not tasks:
                with ui.column().classes("w-full items-center py-8 text-slate-400 gap-1"):
                    ui.icon("done_all", size="md")
                    ui.label("Очередь ожидания пуста").classes("text-xs")
                return

            for pos, task in enumerate(tasks, start=1):
                wait_sec = (now - task.queued_at).total_seconds()
                trig = trigger_key(task.trigger)
                reason = task.blocked_by or "В очереди планировщика — проверка доступности ресурсов..."

                with ui.column().classes("ro-task-card ro-task-queued p-3 w-full gap-2"):
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                        with ui.row().classes("items-center gap-1.5 min-w-0"):
                            ui.badge(f"#{pos}", color="amber-8").props("rounded")
                            ui.label(f"#{task.task_id}").classes("text-xs font-bold ro-mono text-amber-700 dark:text-amber-400")
                            ui.label(task.process_id).classes("text-sm font-semibold truncate")
                        with ui.row().classes("items-center gap-1 shrink-0"):
                            ui.badge(
                                trig,
                                color="purple" if trig == "AUTO" else "indigo",
                            ).props("outline")
                            ui.badge(f"P{task.priority}", color="warning").props("outline")

                    with ui.row().classes("w-full items-center justify-between text-xs text-slate-500"):
                        ui.label(f"Дата: {task.target_date}").classes("ro-mono")
                        ui.label(f"Ожидание: {_fmt_sec(wait_sec)}").classes("font-medium text-amber-700 dark:text-amber-400")

                    with ui.row().classes("ro-blocked-box px-2.5 py-1.5 w-full items-start gap-1.5 no-wrap"):
                        ui.icon("lock_clock", size="xs").classes("mt-0.5 shrink-0")
                        ui.label(reason).classes("text-xs leading-snug")

    def _render_done_column(self, tasks: list[TaskRun]) -> None:
        if self._done_list is None:
            return
        self._done_list.clear()
        with self._done_list:
            if not tasks:
                with ui.column().classes("w-full items-center py-8 text-slate-400 gap-1"):
                    ui.icon("inventory_2", size="md")
                    ui.label("Пока нет завершённых задач").classes("text-xs")
                return

            for task in tasks[:40]:
                st = status_key(task.status)
                trig = trigger_key(task.trigger)
                wait_s = (
                    (task.started_at - task.queued_at).total_seconds()
                    if task.started_at is not None
                    else None
                )
                run_s = (
                    (task.finished_at - task.started_at).total_seconds()
                    if task.started_at is not None and task.finished_at is not None
                    else None
                )

                card_cls = {
                    "COMPLETED": "ro-task-completed",
                    "FAILED": "ro-task-failed",
                    "CANCELLED": "ro-task-cancelled",
                }.get(st, "ro-task-completed")

                badge_color = {
                    "COMPLETED": "positive",
                    "FAILED": "negative",
                    "CANCELLED": "grey",
                }.get(st, "positive")

                with ui.column().classes(f"ro-task-card {card_cls} p-3 w-full gap-1.5"):
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                        with ui.row().classes("items-center gap-1.5 min-w-0"):
                            ui.label(f"#{task.task_id}").classes("text-xs font-bold ro-mono text-slate-600 dark:text-slate-300")
                            ui.label(task.process_id).classes("text-sm font-semibold truncate")
                        with ui.row().classes("items-center gap-1 shrink-0"):
                            ui.badge(trig, color="purple" if trig == "AUTO" else "indigo").props("outline")
                            ui.badge(st, color=badge_color)

                    with ui.row().classes("w-full items-center justify-between text-xs text-slate-500 flex-wrap gap-1"):
                        ui.label(f"Дата: {task.target_date}").classes("ro-mono")
                        ui.label(f"Ожид.: {_fmt_sec(wait_s)} · Расчёт: {_fmt_sec(run_s)}")

                    if st == "FAILED" and task.error_message:
                        with ui.row().classes("ro-error-box px-2.5 py-1.5 w-full items-start gap-1.5 no-wrap"):
                            ui.icon("report_problem", size="xs").classes("mt-0.5 shrink-0")
                            ui.label(task.error_message).classes("text-xs leading-snug font-medium")
                    elif st == "COMPLETED" and isinstance(task.result, dict) and task.result:
                        preview_parts = [
                            f"{k}={v}"
                            for k, v in list(task.result.items())[:3]
                            if k != "status"
                        ]
                        if preview_parts:
                            ui.label("Результат: " + ", ".join(preview_parts)).classes(
                                "text-[11px] text-emerald-700 dark:text-emerald-400 ro-mono"
                            )

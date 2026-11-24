"""Live event log and active resource locks monitor widget."""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ui.contracts import (
    ALL_SOURCE_IDS,
    OrchestratorEvent,
    OrchestratorProtocol,
    SOURCE_DISPLAY_NAMES,
    status_key,
)


def _classify_lock_key(key: str) -> tuple[str, str]:
    """Return (kind, badge_color) for any lock key format returned by LockManager."""
    upper = key.upper()
    if upper.startswith("WRITE") or ":WRITE" in upper or "'WRITE'" in upper:
        return "WRITE", "negative"
    if upper.startswith("READ") or ":READ" in upper or "'READ'" in upper:
        return "READ", "primary"
    if "PROCESS" in upper or "STAGING" in upper:
        return "PROCESS", "warning"
    return "LOCK", "indigo"


def _detect_source_lock_states(
    active_locks: dict[str, list[int]],
) -> dict[str, tuple[str, list[int]]]:
    """Map each canonical data mart source_id to ('FREE' | 'READ' | 'WRITE', holder_ids)."""
    result: dict[str, tuple[str, list[int]]] = {}
    for src in ALL_SOURCE_IDS:
        write_holders: list[int] = []
        read_holders: list[int] = []
        generic_holders: list[int] = []

        for key, holders in active_locks.items():
            if src not in key:
                continue
            kind, _ = _classify_lock_key(key)
            if kind == "WRITE":
                write_holders.extend(holders)
            elif kind == "READ":
                read_holders.extend(holders)
            else:
                generic_holders.extend(holders)

        if write_holders:
            result[src] = ("WRITE", sorted(set(write_holders)))
        elif generic_holders:
            result[src] = ("LOCKED", sorted(set(generic_holders)))
        elif read_holders:
            result[src] = ("READ", sorted(set(read_holders)))
        else:
            result[src] = ("FREE", [])
    return result


class EventLogAndLocksPanel:
    """Renders active data-mart locks alongside the filterable live event journal."""

    def __init__(self, orchestrator: OrchestratorProtocol) -> None:
        self._orchestrator = orchestrator
        self._level_filter: str = "ALL"
        self._search_query: str = ""

        self._locks_count_badge: Any = None
        self._marts_container: Any = None
        self._locks_list_container: Any = None
        self._events_count_badge: Any = None
        self._level_filter_btns: dict[str, Any] = {}
        self._events_container: Any = None

        self._last_locks_fp: Any = None
        self._last_events_fp: Any = None

    def build(self) -> None:
        with ui.element("div").classes("w-full grid grid-cols-1 lg:grid-cols-12 gap-4 items-start"):
            # Left column (5/12): Active Locks & Data Mart Status
            with ui.column().classes("ro-card p-4 w-full gap-3 lg:col-span-5"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("lock", size="sm").classes("text-amber-600")
                        ui.label("Активные блокировки (LockManager)").classes("text-base font-semibold")
                    self._locks_count_badge = ui.badge("0", color="warning").props("rounded")

                ui.label("Состояние витрин данных").classes(
                    "text-xs font-semibold uppercase tracking-wider text-slate-500"
                )
                self._marts_container = ui.element("div").classes(
                    "w-full grid grid-cols-1 sm:grid-cols-2 gap-2"
                )

                ui.label("Удерживаемые ключи блокировок").classes(
                    "text-xs font-semibold uppercase tracking-wider text-slate-500 mt-1"
                )
                self._locks_list_container = ui.column().classes(
                    "w-full gap-1.5 max-h-48 overflow-y-auto"
                )

            # Right column (7/12): Live Event Journal
            with ui.column().classes("ro-card p-4 w-full gap-3 lg:col-span-7"):
                with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("receipt_long", size="sm").classes("text-blue-600")
                        ui.label("Журнал событий оркестратора").classes("text-base font-semibold")
                        self._events_count_badge = ui.badge("0", color="primary").props("outline rounded")

                    with ui.row().classes("items-center gap-1 flex-wrap"):
                        for lvl, lbl in (
                            ("ALL", "Все"),
                            ("INFO", "INFO"),
                            ("WARNING", "WARN"),
                            ("ERROR", "ERR"),
                            ("SUCCESS", "OK"),
                        ):
                            btn = ui.button(
                                lbl,
                                on_click=lambda lv=lvl: self._set_level_filter(lv),
                            ).props("flat dense size=xs no-caps")
                            self._level_filter_btns[lvl] = btn
                        self._update_level_filter_styles()

                ui.input(
                    placeholder="Фильтр по #task_id, процессу или тексту события...",
                    on_change=self._on_search_change,
                ).props("outlined dense clearable").classes("w-full")

                self._events_container = ui.column().classes("w-full gap-1 ro-log-scroll")

        self.refresh(force=True)

    def _update_level_filter_styles(self) -> None:
        for lvl, btn in self._level_filter_btns.items():
            if lvl == self._level_filter:
                btn.classes(
                    replace="px-2 text-xs font-semibold rounded bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                )
            else:
                btn.classes(replace="px-2 text-xs text-slate-500 dark:text-slate-400")

    def _set_level_filter(self, level: str) -> None:
        self._level_filter = level
        self._update_level_filter_styles()
        self._last_events_fp = None
        self.refresh(force=True)

    def _on_search_change(self, event: Any) -> None:
        self._search_query = str(event.value or "").strip().lower()
        self._last_events_fp = None
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        active_locks = self._orchestrator.get_active_locks() or {}
        orch_enabled = self._orchestrator.is_orchestration_enabled()
        running_count = len(
            [t for t in self._orchestrator.get_tasks() if status_key(t.status) == "RUNNING"]
        )

        locks_fp = (
            orch_enabled,
            running_count > 0,
            tuple(sorted((k, tuple(v)) for k, v in active_locks.items())),
        )
        if force or locks_fp != self._last_locks_fp:
            self._last_locks_fp = locks_fp
            self._render_locks(active_locks, orch_enabled, running_count)

        events = self._orchestrator.get_events(limit=100) or []
        filtered = self._filter_events(events)
        events_fp = (
            self._level_filter,
            self._search_query,
            len(events),
            tuple(
                (e.timestamp.isoformat(), e.level, e.task_id, e.message)
                for e in filtered[-60:]
            ),
        )
        if force or events_fp != self._last_events_fp:
            self._last_events_fp = events_fp
            self._render_events(filtered, len(events))

    def _filter_events(self, events: list[OrchestratorEvent]) -> list[OrchestratorEvent]:
        out: list[OrchestratorEvent] = []
        for ev in events:
            lvl = (ev.level or "INFO").upper()
            if self._level_filter != "ALL":
                if self._level_filter == "ERROR" and lvl not in ("ERROR", "CRITICAL"):
                    continue
                if self._level_filter == "WARNING" and lvl not in ("WARN", "WARNING"):
                    continue
                if self._level_filter in ("INFO", "SUCCESS") and lvl != self._level_filter:
                    continue
            if self._search_query:
                haystack = f"#{ev.task_id or ''} {lvl} {ev.message}".lower()
                if self._search_query not in haystack:
                    continue
            out.append(ev)
        return out

    def _render_locks(
        self,
        active_locks: dict[str, list[int]],
        orch_enabled: bool,
        running_count: int,
    ) -> None:
        if self._locks_count_badge is not None:
            self._locks_count_badge.text = str(len(active_locks))

        mart_states = _detect_source_lock_states(active_locks)

        if self._marts_container is not None:
            self._marts_container.clear()
            with self._marts_container:
                for src in ALL_SOURCE_IDS:
                    state, holders = mart_states.get(src, ("FREE", []))
                    if state == "WRITE":
                        border_cls = "border-red-400 bg-red-50/70 dark:bg-red-950/30"
                        badge_color = "negative"
                        state_lbl = "WRITE LOCK"
                    elif state == "LOCKED":
                        border_cls = "border-amber-400 bg-amber-50/70 dark:bg-amber-950/30"
                        badge_color = "warning"
                        state_lbl = "LOCKED"
                    elif state == "READ":
                        border_cls = "border-blue-400 bg-blue-50/70 dark:bg-blue-950/30"
                        badge_color = "primary"
                        state_lbl = "READ LOCK"
                    else:
                        border_cls = "border-slate-200 dark:border-slate-700 bg-slate-50/70 dark:bg-slate-800/40"
                        badge_color = "positive"
                        state_lbl = "СВОБОДНА"

                    with ui.column().classes(f"rounded-lg border p-2.5 gap-1 {border_cls}"):
                        with ui.row().classes("w-full items-center justify-between gap-1 no-wrap"):
                            ui.label(src).classes("text-xs font-bold ro-mono truncate")
                            ui.badge(state_lbl, color=badge_color).props("dense")
                        ui.label(SOURCE_DISPLAY_NAMES.get(src, src)).classes(
                            "text-[11px] text-slate-500 truncate w-full"
                        )
                        if holders:
                            holders_txt = ", ".join(f"#{tid}" for tid in holders)
                            ui.label(f"Удерживают: {holders_txt}").classes(
                                "text-[11px] font-semibold ro-mono"
                            )
                        elif not orch_enabled and running_count > 0:
                            ui.label("⚠ Не защищена (без оркестратора)").classes(
                                "text-[10px] text-red-600 dark:text-red-400"
                            )
                        else:
                            ui.label("Доступна для захвата").classes("text-[10px] text-slate-400")

        if self._locks_list_container is not None:
            self._locks_list_container.clear()
            with self._locks_list_container:
                if not active_locks:
                    msg = (
                        "Нет активных блокировок — все ресурсы свободны."
                        if orch_enabled
                        else "LockManager отключён — блокировки не выставляются."
                    )
                    ui.label(msg).classes("text-xs text-slate-400 py-2")
                    return

                for key, holders in sorted(active_locks.items()):
                    kind, color = _classify_lock_key(key)
                    holders_str = ", ".join(f"#{tid}" for tid in holders)
                    with ui.row().classes(
                        "ro-subcard px-2.5 py-1.5 w-full items-center justify-between gap-2 no-wrap"
                    ):
                        with ui.row().classes("items-center gap-1.5 min-w-0"):
                            ui.badge(kind, color=color).props("outline dense")
                            ui.label(str(key)).classes("text-xs ro-mono truncate")
                        ui.label(holders_str).classes("text-xs font-semibold ro-mono shrink-0")

    def _render_events(self, events: list[OrchestratorEvent], total_count: int) -> None:
        if self._events_count_badge is not None:
            self._events_count_badge.text = str(total_count)

        if self._events_container is None:
            return
        self._events_container.clear()
        with self._events_container:
            if not events:
                ui.label("Событий по текущему фильтру пока нет.").classes("text-xs text-slate-400 py-4")
                return

            # Render newest first so live changes appear at top immediately
            for ev in reversed(events[-60:]):
                lvl = (ev.level or "INFO").upper()
                ts_str = ev.timestamp.strftime("%H:%M:%S")
                lvl_cls = {
                    "ERROR": "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300",
                    "CRITICAL": "bg-red-200 text-red-800 dark:bg-red-900/60 dark:text-red-200",
                    "WARNING": "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300",
                    "WARN": "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300",
                    "SUCCESS": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300",
                }.get(lvl, "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300")

                row_bg = (
                    "bg-red-50/70 dark:bg-red-950/25 border-red-200/70 dark:border-red-900/40"
                    if lvl in ("ERROR", "CRITICAL")
                    else "bg-slate-50/60 dark:bg-slate-800/40 border-slate-200/60 dark:border-slate-700/50"
                )

                with ui.row().classes(
                    f"w-full items-start gap-2 px-2.5 py-1.5 rounded-md border text-xs no-wrap {row_bg}"
                ):
                    ui.label(ts_str).classes("text-slate-400 shrink-0 text-[11px]")
                    ui.label(lvl[:5]).classes(
                        f"px-1.5 py-0.5 rounded text-[10px] font-bold shrink-0 {lvl_cls}"
                    )
                    if ev.task_id is not None:
                        ui.label(f"#{ev.task_id}").classes(
                            "text-[11px] font-bold text-indigo-600 dark:text-indigo-400 shrink-0"
                        )
                    ui.label(ev.message).classes("text-xs leading-snug break-words flex-1 font-sans")

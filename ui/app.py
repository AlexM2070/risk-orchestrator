"""Main NiceGUI application for the Risk Orchestrator MVP.

Integrates with any Orchestrator & ReglamentScheduler implementing Section 5.4
contracts, and falls back to the built-in MockOrchestrator when launched standalone.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

# Allow running directly via `python ui/app.py` in addition to `python -m ui.app`
if __package__ in (None, ""):
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from nicegui import app as nicegui_app
from nicegui import ui

from ui.components import (
    ControlPanel,
    Dashboard,
    EventLogAndLocksPanel,
    GanttChartPanel,
    ManualLaunchForm,
    render_conflict_matrix,
)
from ui.contracts import (
    OrchestratorProtocol,
    RandomBatchGenerator,
    ScenarioSpec,
    SchedulerProtocol,
    status_key,
)
from ui.mock_backend import (
    MockOrchestrator,
    MockReglamentScheduler,
    generate_mock_random_batch,
    get_mock_demo_scenarios,
)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STYLES_PATH = ASSETS_DIR / "styles.css"


@dataclass
class _ResolvedRuntime:
    orchestrator: OrchestratorProtocol
    scheduler: SchedulerProtocol | None
    scenarios: list[ScenarioSpec]
    random_batch_fn: RandomBatchGenerator | None
    engine_label: str
    is_mock: bool


def resolve_scenarios(
    scenarios: list[ScenarioSpec] | None = None,
    random_batch_fn: RandomBatchGenerator | None = None,
) -> tuple[list[ScenarioSpec], RandomBatchGenerator | None]:
    """Load scenarios from services.scenarios when available, else use built-in mock scenarios."""
    resolved_scenarios = scenarios
    resolved_random = random_batch_fn

    if resolved_scenarios is None or resolved_random is None:
        try:
            from services.scenarios import (  # type: ignore[import-not-found]
                generate_random_batch,
                get_demo_scenarios,
            )

            if resolved_scenarios is None:
                resolved_scenarios = list(get_demo_scenarios())
            if resolved_random is None:
                resolved_random = generate_random_batch
        except ImportError:
            if resolved_scenarios is None:
                resolved_scenarios = get_mock_demo_scenarios()
            if resolved_random is None:
                resolved_random = generate_mock_random_batch

    return resolved_scenarios, resolved_random


def _resolve_runtime(
    orchestrator: OrchestratorProtocol | None,
    scheduler: SchedulerProtocol | None,
    scenarios: list[ScenarioSpec] | None,
    random_batch_fn: RandomBatchGenerator | None,
) -> _ResolvedRuntime:
    resolved_scenarios, resolved_random = resolve_scenarios(scenarios, random_batch_fn)

    if orchestrator is not None:
        is_mock = isinstance(orchestrator, MockOrchestrator)
        label = "Mock Demo Engine" if is_mock else "Core Engine"
        return _ResolvedRuntime(
            orchestrator=orchestrator,
            scheduler=scheduler,
            scenarios=resolved_scenarios,
            random_batch_fn=resolved_random,
            engine_label=label,
            is_mock=is_mock,
        )

    mock_orch = MockOrchestrator()
    mock_sched = scheduler if scheduler is not None else MockReglamentScheduler(mock_orch, interval=15.0)
    return _ResolvedRuntime(
        orchestrator=mock_orch,
        scheduler=mock_sched,
        scenarios=resolved_scenarios,
        random_batch_fn=resolved_random,
        engine_label="Mock Demo Engine",
        is_mock=True,
    )


class _UIStateHub:
    """Single orchestrator subscriber that safely fans out dirty notifications to active UI tabs."""

    def __init__(self, orchestrator: OrchestratorProtocol) -> None:
        self._sessions: set[_PageSession] = set()
        orchestrator.subscribe(self.notify_all)

    def register(self, session: _PageSession) -> None:
        self._sessions.add(session)

    def unregister(self, session: _PageSession) -> None:
        self._sessions.discard(session)

    def notify_all(self) -> None:
        for session in list(self._sessions):
            session.mark_dirty()


class _PageSession:
    """Per-browser-client controller managing live refresh without leaks or flicker."""

    def __init__(
        self,
        runtime: _ResolvedRuntime,
        hub: _UIStateHub,
    ) -> None:
        self._runtime = runtime
        self._hub = hub
        self._dirty = True
        self._closed = False
        self._is_dark = False

        self._dark_mode: Any = None
        self._theme_btn: Any = None
        self._header_mode_badge: Any = None
        self._control_panel: ControlPanel | None = None
        self._manual_form: ManualLaunchForm | None = None
        self._dashboard: Dashboard | None = None
        self._gantt: GanttChartPanel | None = None
        self._event_log: EventLogAndLocksPanel | None = None
        self._timer: Any = None

    def mark_dirty(self) -> None:
        self._dirty = True

    def close(self) -> None:
        self._closed = True
        self._hub.unregister(self)
        if self._timer is not None:
            try:
                self._timer.deactivate()
            except Exception:
                pass

    def build(self) -> None:
        if STYLES_PATH.exists():
            ui.add_css(STYLES_PATH)

        self._dark_mode = ui.dark_mode(value=False)

        # Register cleanup when client disconnects or is deleted
        client = ui.context.client
        client.on_disconnect(self.close)
        client.on_delete(self.close)
        self._hub.register(self)

        specs = self._runtime.orchestrator.get_process_specs()

        # Conflict Matrix popup dialog
        with ui.dialog() as matrix_dialog, ui.card().classes("w-full max-w-5xl p-5"):
            with ui.row().classes("w-full items-center justify-between mb-2"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("grid_on", size="sm").classes("text-indigo-600")
                    ui.label("Матрица зависимостей и конфликтов READ/WRITE").classes(
                        "text-lg font-semibold"
                    )
                ui.button(icon="close", on_click=matrix_dialog.close).props("flat round dense")
            render_conflict_matrix(specs)

        # Top navigation header
        with ui.header(elevated=True).classes("ro-header px-5 py-2.5 text-white"):
            with ui.row().classes("w-full max-w-[1480px] mx-auto items-center justify-between flex-wrap gap-3"):
                with ui.row().classes("items-center gap-3"):
                    with ui.element("div").classes(
                        "w-9 h-9 rounded-xl bg-blue-600/90 flex items-center justify-center shadow"
                    ):
                        ui.icon("account_tree", size="sm", color="white")
                    with ui.column().classes("gap-0"):
                        ui.label("Risk Orchestrator").classes("text-lg font-bold leading-tight tracking-tight")
                        ui.label("Оркестратор оценки рисков · Логистика и цепи поставок").classes(
                            "text-xs text-slate-300"
                        )

                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.badge(
                        self._runtime.engine_label,
                        color="amber-7" if self._runtime.is_mock else "positive",
                    ).props("outline rounded").classes("text-xs px-2 py-1")

                    self._header_mode_badge = ui.badge("", color="positive").props("rounded").classes(
                        "text-xs px-2.5 py-1 font-semibold"
                    )

                    ui.button(
                        "Матрица конфликтов",
                        icon="grid_on",
                        on_click=matrix_dialog.open,
                    ).props("flat dense no-caps color=white").classes(
                        "text-xs bg-white/10 hover:bg-white/20 px-3 py-1 rounded-lg"
                    )

                    self._theme_btn = ui.button(
                        icon="dark_mode",
                        on_click=self._toggle_theme,
                    ).props("flat round dense color=white")
                    self._theme_btn.tooltip("Переключить светлую / тёмную тему")

        # Main workspace container
        with ui.column().classes("w-full max-w-[1480px] mx-auto px-4 py-4 gap-4"):
            # Row 1: Control Panel (8 cols) + Manual Launch Form (4 cols)
            self._dashboard = Dashboard(self._runtime.orchestrator)

            with ui.element("div").classes("w-full grid grid-cols-1 xl:grid-cols-12 gap-4 items-stretch"):
                with ui.element("div").classes("xl:col-span-8 flex"):
                    self._control_panel = ControlPanel(
                        orchestrator=self._runtime.orchestrator,
                        scheduler=self._runtime.scheduler,
                        scenarios=self._runtime.scenarios,
                        random_batch_fn=self._runtime.random_batch_fn,
                        on_snapshot_mode=self._dashboard.capture_snapshot,
                        on_after_action=self.refresh_now,
                    )
                    self._control_panel.build()

                with ui.element("div").classes("xl:col-span-4 flex"):
                    self._manual_form = ManualLaunchForm(
                        orchestrator=self._runtime.orchestrator,
                        on_submitted=self.refresh_now,
                    )
                    self._manual_form.build()

            # Row 2: Live KPI metrics + 3-column task dashboard
            self._dashboard.build()

            # Row 3: Live Plotly Gantt Timeline
            self._gantt = GanttChartPanel(self._runtime.orchestrator)
            self._gantt.build()

            # Row 4: Active Locks + Event Journal
            self._event_log = EventLogAndLocksPanel(self._runtime.orchestrator)
            self._event_log.build()

            # Row 5: Expandable Conflict Matrix Reference
            with ui.expansion(
                "Справка: Матрица зависимостей READ/WRITE и попарных конфликтов",
                icon="hub",
            ).classes("ro-card w-full"):
                with ui.column().classes("p-2 w-full"):
                    render_conflict_matrix(specs)

        self._update_header_mode()
        self._timer = ui.timer(0.35, self._on_tick)

    def _toggle_theme(self) -> None:
        self._is_dark = not self._is_dark
        if self._dark_mode is not None:
            self._dark_mode.value = self._is_dark
        if self._theme_btn is not None:
            self._theme_btn.props(f"icon={'light_mode' if self._is_dark else 'dark_mode'} flat round dense color=white")
        if self._gantt is not None:
            self._gantt.set_dark(self._is_dark)

    def _update_header_mode(self) -> None:
        if self._header_mode_badge is None:
            return
        enabled = self._runtime.orchestrator.is_orchestration_enabled()
        if enabled:
            self._header_mode_badge.text = "Защита: ВКЛ"
            self._header_mode_badge.props("color=positive rounded")
        else:
            self._header_mode_badge.text = "Режим хаоса: БЕЗ защиты"
            self._header_mode_badge.props("color=negative rounded")

    def refresh_now(self) -> None:
        if self._closed:
            return
        self._dirty = False
        self._update_header_mode()
        if self._control_panel is not None:
            self._control_panel.refresh()
        if self._dashboard is not None:
            self._dashboard.refresh()
        if self._gantt is not None:
            self._gantt.refresh()
        if self._event_log is not None:
            self._event_log.refresh()

    def _on_tick(self) -> None:
        if self._closed:
            return
        has_active = any(
            status_key(t.status) in ("RUNNING", "QUEUED")
            for t in self._runtime.orchestrator.get_tasks()
        )
        if not self._dirty and not has_active:
            return
        self.refresh_now()


_ACTIVE_RUNTIME: _ResolvedRuntime | None = None
_ACTIVE_HUB: _UIStateHub | None = None
_LIFECYCLE_REGISTERED = False


def create_app(
    orchestrator: OrchestratorProtocol | None = None,
    scheduler: SchedulerProtocol | None = None,
    scenarios: list[ScenarioSpec] | None = None,
    random_batch_fn: RandomBatchGenerator | None = None,
    *,
    manage_lifecycle: bool = True,
) -> _ResolvedRuntime:
    """Wire the orchestrator/scheduler to NiceGUI page routes without starting the server yet."""
    global _ACTIVE_RUNTIME, _ACTIVE_HUB, _LIFECYCLE_REGISTERED

    runtime = _resolve_runtime(orchestrator, scheduler, scenarios, random_batch_fn)
    hub = _UIStateHub(runtime.orchestrator)
    _ACTIVE_RUNTIME = runtime
    _ACTIVE_HUB = hub

    if manage_lifecycle and not _LIFECYCLE_REGISTERED:
        _LIFECYCLE_REGISTERED = True

        async def _on_startup() -> None:
            if _ACTIVE_RUNTIME is not None:
                await _ACTIVE_RUNTIME.orchestrator.start()

        async def _on_shutdown() -> None:
            if _ACTIVE_RUNTIME is None:
                return
            sched = _ACTIVE_RUNTIME.scheduler
            if sched is not None:
                sched.set_enabled(False)
                stop_sched = getattr(sched, "stop", None)
                if callable(stop_sched):
                    res = stop_sched()
                    if hasattr(res, "__await__"):
                        await res
            await _ACTIVE_RUNTIME.orchestrator.stop()

        nicegui_app.on_startup(_on_startup)
        nicegui_app.on_shutdown(_on_shutdown)

    @ui.page("/")
    def index_page() -> None:
        assert _ACTIVE_RUNTIME is not None and _ACTIVE_HUB is not None
        session = _PageSession(_ACTIVE_RUNTIME, _ACTIVE_HUB)
        session.build()

    return runtime


def run_ui(
    orchestrator: OrchestratorProtocol | None = None,
    scheduler: SchedulerProtocol | None = None,
    scenarios: list[ScenarioSpec] | None = None,
    random_batch_fn: RandomBatchGenerator | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    native: bool = False,
    reload: bool = False,
    show: bool = True,
    title: str = "Risk Orchestrator — Оркестратор оценки рисков",
    manage_lifecycle: bool = True,
) -> None:
    """Configure routes and start the NiceGUI web/desktop application."""
    create_app(
        orchestrator=orchestrator,
        scheduler=scheduler,
        scenarios=scenarios,
        random_batch_fn=random_batch_fn,
        manage_lifecycle=manage_lifecycle,
    )
    ui.run(
        host=host,
        port=port,
        title=title,
        native=native,
        reload=reload,
        show=show,
        dark=False,
        language="ru",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Risk Orchestrator NiceGUI dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument("--native", action="store_true", help="Launch as desktop native window")
    parser.add_argument("--no-show", action="store_true", help="Do not auto-open browser window")
    return parser.parse_args()


if __name__ in {"__main__", "__mp_main__"}:
    args = _parse_args()
    run_ui(
        host=args.host,
        port=args.port,
        native=args.native,
        reload=False,
        show=not args.no_show,
    )

"""Control panel widget: mode switches, scheduler controls, pause/reset, and 1-click scenarios."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from ui.contracts import (
    OrchestratorProtocol,
    RandomBatchGenerator,
    ScenarioSpec,
    SchedulerProtocol,
)


def _scenario_visuals(scenario_id: str, index: int) -> tuple[str, bool]:
    """Choose a Material icon and stress-highlight flag for a scenario button."""
    lowered = scenario_id.lower()
    if "stress" in lowered or "all" in lowered or index == 0:
        return "bolt", True
    if "manual" in lowered or "reglament" in lowered or "1" in lowered:
        return "schedule_send", False
    if "source" in lowered or "conflict" in lowered or "2" in lowered:
        return "hub", False
    if "date" in lowered or "deadlock" in lowered or "3" in lowered:
        return "sync_problem", False
    return "play_arrow", False


class ControlPanel:
    """Renders orchestrator controls, reglament toggle, and 1-click demo scenarios."""

    def __init__(
        self,
        orchestrator: OrchestratorProtocol,
        scheduler: SchedulerProtocol | None,
        scenarios: list[ScenarioSpec],
        random_batch_fn: RandomBatchGenerator | None = None,
        on_snapshot_mode: Callable[[], None] | None = None,
        on_after_action: Callable[[], None] | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._scheduler = scheduler
        self._scenarios = scenarios
        self._random_batch_fn = random_batch_fn
        self._on_snapshot_mode = on_snapshot_mode
        self._on_after_action = on_after_action

        self._syncing = False
        self._paused_local = self._read_paused()
        self._interval_sec: float = 15.0
        self._random_count: int = 8

        self._orch_switch: Any = None
        self._reglament_switch: Any = None
        self._pause_btn: Any = None
        self._banner_container: Any = None
        self._banner_icon: Any = None
        self._banner_title: Any = None
        self._banner_subtitle: Any = None
        self._mode_state_label: Any = None

    def _read_paused(self) -> bool:
        getter = getattr(self._orchestrator, "is_paused", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                return False
        return False

    def build(self) -> None:
        with ui.column().classes("ro-card p-4 w-full gap-3"):
            # Row 1: Mode switcher + Reglament + Pause/Reset
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-3"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("tune", size="sm").classes("text-blue-600")
                    ui.label("Панель управления и демо-сценарии").classes("text-base font-semibold")

                with ui.row().classes("items-center flex-wrap gap-2"):
                    # Orchestration mode switch
                    with ui.row().classes(
                        "ro-subcard px-3 py-1.5 items-center gap-2"
                    ):
                        self._orch_switch = ui.switch(
                            value=self._orchestrator.is_orchestration_enabled(),
                            on_change=self._handle_mode_toggle,
                        ).props("color=positive keep-color dense")
                        self._mode_state_label = ui.label("").classes("text-xs font-semibold")

                    # Reglament auto-scheduler toggle + interval
                    with ui.row().classes(
                        "ro-subcard px-3 py-1.5 items-center gap-2"
                    ):
                        sched_enabled = self._scheduler.is_enabled() if self._scheduler else False
                        self._reglament_switch = ui.switch(
                            "Регламент",
                            value=sched_enabled,
                            on_change=self._handle_reglament_toggle,
                        ).props("color=primary dense")
                        if self._scheduler is None:
                            self._reglament_switch.disable()
                            self._reglament_switch.tooltip("Планировщик регламента не подключён")
                        interval_sel = (
                            ui.select(
                                options={5: "5с", 10: "10с", 15: "15с", 30: "30с"},
                                value=15,
                                on_change=self._handle_interval_change,
                            )
                            .props("dense outlined hide-bottom-space options-dense")
                            .classes("w-16 text-xs")
                        )
                        if self._scheduler is None:
                            interval_sel.disable()
                        interval_sel.tooltip("Интервал автоматических регламентных запусков")

                    # Pause / Resume
                    self._pause_btn = ui.button(
                        "Пауза",
                        icon="pause",
                        on_click=self._handle_pause_toggle,
                    ).props("outline dense color=amber-8 no-caps").classes("px-3")

                    # Reset
                    ui.button(
                        "Сброс",
                        icon="restart_alt",
                        on_click=self._handle_reset,
                    ).props("outline dense color=negative no-caps").classes("px-3")

            # Row 2: Live Mode Banner (With vs Without Orchestrator)
            with ui.row().classes("w-full items-center gap-3 px-3.5 py-2.5") as banner:
                self._banner_container = banner
                self._banner_icon = ui.icon("verified_user", size="sm")
                with ui.column().classes("gap-0 flex-1"):
                    self._banner_title = ui.label("").classes("text-sm font-semibold")
                    self._banner_subtitle = ui.label("").classes("text-xs opacity-80")

            # Row 3: 1-click Demo Scenarios Grid
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2 mt-1"):
                ui.label("Готовые демо-сценарии (запуск в 1 клик)").classes(
                    "text-xs font-semibold uppercase tracking-wider text-slate-500"
                )
                if self._random_batch_fn is not None:
                    with ui.row().classes("items-center gap-1.5"):
                        ui.label("Случайный стресс-батч:").classes("text-xs text-slate-500")
                        ui.select(
                            options={4: "4", 6: "6", 8: "8", 10: "10", 12: "12"},
                            value=self._random_count,
                            on_change=lambda e: setattr(self, "_random_count", int(e.value)),
                        ).props("dense outlined hide-bottom-space options-dense").classes("w-16 text-xs")
                        ui.button(
                            "Сгенерировать",
                            icon="shuffle",
                            on_click=self._handle_random_batch,
                        ).props("outline dense color=primary no-caps size=sm").classes("px-2.5")

            with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-2 gap-2.5"
            ):
                for idx, scenario in enumerate(self._scenarios):
                    icon_name, is_stress = _scenario_visuals(scenario.scenario_id, idx)
                    btn_cls = (
                        "ro-scenario-btn ro-scenario-btn-stress w-full p-3 text-left"
                        if is_stress
                        else "ro-scenario-btn w-full p-3 text-left bg-white dark:bg-slate-800/70"
                    )
                    with ui.button(
                        on_click=lambda s=scenario: self._handle_run_scenario(s)
                    ).props("flat no-caps align=left").classes(btn_cls):
                        with ui.column().classes("w-full gap-1 items-start"):
                            with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                                with ui.row().classes("items-center gap-1.5 min-w-0"):
                                    ui.icon(
                                        icon_name,
                                        size="xs",
                                        color="white" if is_stress else "primary",
                                    )
                                    ui.label(scenario.title).classes(
                                        "text-sm font-semibold leading-tight "
                                        + ("text-white" if is_stress else "text-slate-800 dark:text-slate-100")
                                    )
                                ui.badge(
                                    f"{len(scenario.tasks)} задач",
                                    color="white" if is_stress else "primary",
                                    text_color="primary" if is_stress else "white",
                                ).props("rounded" if is_stress else "outline rounded")
                            ui.label(scenario.description).classes(
                                "text-xs leading-snug font-normal "
                                + ("text-blue-100" if is_stress else "text-slate-500 dark:text-slate-400")
                            )

        self.refresh()

    def _apply_mode_visuals(self, enabled: bool) -> None:
        if self._mode_state_label is not None:
            if enabled:
                self._mode_state_label.text = "С оркестратором"
                self._mode_state_label.classes(replace="text-xs font-semibold text-emerald-600 dark:text-emerald-400")
            else:
                self._mode_state_label.text = "Без оркестратора"
                self._mode_state_label.classes(replace="text-xs font-semibold text-red-600 dark:text-red-400")

        if self._banner_container is not None:
            self._banner_container.classes(
                replace=(
                    "w-full items-center gap-3 px-3.5 py-2.5 ro-mode-banner-safe"
                    if enabled
                    else "w-full items-center gap-3 px-3.5 py-2.5 ro-mode-banner-chaos"
                )
            )
        if self._banner_icon is not None:
            self._banner_icon.name = "verified_user" if enabled else "warning_amber"
            self._banner_icon.classes(
                replace="text-emerald-600 dark:text-emerald-400"
                if enabled
                else "text-red-600 dark:text-red-400"
            )
        if self._banner_title is not None:
            self._banner_title.text = (
                "Режим: С ОРКЕСТРАТОРОМ (Защита активна)"
                if enabled
                else "Режим: БЕЗ ОРКЕСТРАТОРА (Как было — Хаос)"
            )
            self._banner_title.classes(
                replace="text-sm font-semibold text-emerald-800 dark:text-emerald-300"
                if enabled
                else "text-sm font-semibold text-red-800 dark:text-red-300"
            )
        if self._banner_subtitle is not None:
            self._banner_subtitle.text = (
                "Приоритетная очередь + атомарный LockManager («всё или ничего»): "
                "независимые расчёты идут параллельно, конфликтующие безопасно ждут освобождения витрин."
                if enabled
                else "Задачи запускаются напрямую без блокировок и очереди: "
                "при пересечении витрин и дат возникают DeadlockError, DataCorruptionError и CrossDateCollisionError."
            )

    def _apply_pause_visuals(self, paused: bool) -> None:
        if self._pause_btn is None:
            return
        if paused:
            self._pause_btn.text = "Продолжить"
            self._pause_btn.props("icon=play_arrow outline dense color=positive no-caps")
        else:
            self._pause_btn.text = "Пауза"
            self._pause_btn.props("icon=pause outline dense color=amber-8 no-caps")

    def _handle_mode_toggle(self, event: Any) -> None:
        if self._syncing:
            return
        if self._on_snapshot_mode is not None:
            self._on_snapshot_mode()
        enabled = bool(event.value)
        self._orchestrator.set_orchestration_enabled(enabled)
        self._apply_mode_visuals(enabled)
        ui.notify(
            "Оркестратор ВКЛЮЧЁН — блокировки и очередь активны"
            if enabled
            else "Оркестратор ОТКЛЮЧЁН — режим хаоса «как было»",
            type="positive" if enabled else "negative",
            position="top-right",
        )
        if self._on_after_action is not None:
            self._on_after_action()

    def _handle_reglament_toggle(self, event: Any) -> None:
        if self._syncing or self._scheduler is None:
            return
        enabled = bool(event.value)
        self._scheduler.set_interval(self._interval_sec)
        self._scheduler.set_enabled(enabled)
        ui.notify(
            f"Авторегламент включён (раз в {int(self._interval_sec)} сек)"
            if enabled
            else "Авторегламент остановлен",
            type="info",
            position="top-right",
        )
        if self._on_after_action is not None:
            self._on_after_action()

    def _handle_interval_change(self, event: Any) -> None:
        if event.value is None:
            return
        self._interval_sec = float(event.value)
        if self._scheduler is not None:
            self._scheduler.set_interval(self._interval_sec)
            ui.notify(
                f"Интервал регламента: {int(self._interval_sec)} сек",
                type="info",
                position="top-right",
            )

    def _handle_pause_toggle(self) -> None:
        paused = self._read_paused() if hasattr(self._orchestrator, "is_paused") else self._paused_local
        if paused:
            self._orchestrator.resume()
            self._paused_local = False
            ui.notify("Очередь возобновлена", type="positive", position="top-right")
        else:
            self._orchestrator.pause()
            self._paused_local = True
            ui.notify("Планировщик поставлен на паузу", type="warning", position="top-right")
        self._apply_pause_visuals(self._paused_local)
        if self._on_after_action is not None:
            self._on_after_action()

    async def _handle_reset(self) -> None:
        if self._on_snapshot_mode is not None:
            self._on_snapshot_mode()
        await self._orchestrator.reset()
        self._paused_local = False
        self._apply_pause_visuals(False)
        ui.notify("Оркестратор и дашборд сброшены", type="info", position="top-right")
        if self._on_after_action is not None:
            self._on_after_action()

    async def _handle_run_scenario(self, scenario: ScenarioSpec) -> None:
        try:
            await self._orchestrator.submit_batch(list(scenario.tasks))
            ui.notify(
                f"Запущен «{scenario.title}» ({len(scenario.tasks)} задач)",
                type="info",
                position="top-right",
            )
        except Exception as exc:
            ui.notify(f"Ошибка запуска сценария: {exc}", type="negative", position="top-right")
        if self._on_after_action is not None:
            self._on_after_action()

    async def _handle_random_batch(self) -> None:
        if self._random_batch_fn is None:
            return
        try:
            batch = self._random_batch_fn(self._random_count, None)
            await self._orchestrator.submit_batch(batch)
            ui.notify(
                f"Отправлен случайный батч из {len(batch)} задач",
                type="info",
                position="top-right",
            )
        except Exception as exc:
            ui.notify(f"Ошибка генерации батча: {exc}", type="negative", position="top-right")
        if self._on_after_action is not None:
            self._on_after_action()

    def refresh(self) -> None:
        """Sync switches and banner with live orchestrator & scheduler state."""
        self._syncing = True
        try:
            enabled = self._orchestrator.is_orchestration_enabled()
            if self._orch_switch is not None and bool(self._orch_switch.value) != enabled:
                self._orch_switch.value = enabled
            self._apply_mode_visuals(enabled)

            if self._scheduler is not None and self._reglament_switch is not None:
                sched_on = self._scheduler.is_enabled()
                if bool(self._reglament_switch.value) != sched_on:
                    self._reglament_switch.value = sched_on

            if hasattr(self._orchestrator, "is_paused"):
                self._paused_local = self._read_paused()
            self._apply_pause_visuals(self._paused_local)
        finally:
            self._syncing = False

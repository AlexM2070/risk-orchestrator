"""Manual task launch form widget."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from nicegui import ui

from ui.contracts import (
    OrchestratorProtocol,
    ProcessSpec,
    TaskRequest,
    TriggerType,
)


class ManualLaunchForm:
    """Form for submitting individual risk calculation tasks manually."""

    def __init__(
        self,
        orchestrator: OrchestratorProtocol,
        on_submitted: Callable[[], None] | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._on_submitted = on_submitted

        self._specs: list[ProcessSpec] = orchestrator.get_process_specs()
        self._spec_by_id: dict[str, ProcessSpec] = {s.process_id: s for s in self._specs}

        self._selected_process: str = self._specs[0].process_id if self._specs else ""
        self._selected_date: str = date.today().isoformat()
        self._selected_priority: int = 2
        self._selected_trigger: str = "MANUAL"

        self._date_input: Any = None
        self._contract_preview: Any = None

    def build(self) -> None:
        with ui.column().classes("ro-card p-4 w-full h-full gap-3 justify-between"):
            with ui.column().classes("w-full gap-3"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("rocket_launch", size="sm").classes("text-indigo-600")
                    ui.label("Ручной запуск процесса").classes("text-base font-semibold")

                process_options = {s.process_id: s.display_name for s in self._specs}
                ui.select(
                    options=process_options,
                    label="Микросервис оценки рисков",
                    value=self._selected_process,
                    on_change=self._on_process_change,
                ).props("outlined dense options-dense").classes("w-full")

                # Resource contract preview for selected process
                self._contract_preview = ui.column().classes("ro-subcard p-2.5 w-full gap-1.5")
                self._render_contract_preview()

                # Date input with popup calendar + quick presets
                with ui.column().classes("w-full gap-1.5"):
                    with ui.input(
                        label="Дата расчёта (target_date)",
                        value=self._selected_date,
                        on_change=lambda e: setattr(self, "_selected_date", str(e.value or "").strip()),
                    ).props("outlined dense mask=####-##-##") as date_inp:
                        self._date_input = date_inp
                        date_inp.classes("w-full")
                        with date_inp.add_slot("append"):
                            with ui.icon("event").classes("cursor-pointer text-slate-500"):
                                with ui.menu() as date_menu:
                                    ui.date(
                                        value=self._selected_date,
                                        on_change=lambda e, m=date_menu: self._set_date(str(e.value), m),
                                    )

                    today = date.today()
                    with ui.row().classes("gap-1.5 w-full"):
                        for label, offset in (("Вчера", -1), ("Сегодня", 0), ("Завтра", 1)):
                            preset = (today + timedelta(days=offset)).isoformat()
                            ui.button(
                                label,
                                on_click=lambda d=preset: self._set_date(d),
                            ).props("flat dense size=xs color=primary no-caps").classes(
                                "bg-blue-50 dark:bg-blue-900/30 px-2 rounded"
                            )

                # Priority & Trigger row
                with ui.row().classes("w-full gap-2 no-wrap"):
                    ui.select(
                        options={
                            1: "P1 — Высокий",
                            2: "P2 — Стандартный",
                            3: "P3 — Низкий",
                        },
                        label="Приоритет",
                        value=self._selected_priority,
                        on_change=lambda e: setattr(self, "_selected_priority", int(e.value)),
                    ).props("outlined dense options-dense").classes("flex-1")

                    ui.select(
                        options={
                            "MANUAL": "MANUAL (Ручной)",
                            "AUTO": "AUTO (Регламент)",
                        },
                        label="Тип запуска",
                        value=self._selected_trigger,
                        on_change=lambda e: setattr(self, "_selected_trigger", str(e.value)),
                    ).props("outlined dense options-dense").classes("flex-1")

            ui.button(
                "Запустить процесс",
                icon="play_arrow",
                on_click=self._handle_submit,
            ).props("unelevated color=primary no-caps").classes("w-full py-2 font-semibold rounded-lg")

    def _on_process_change(self, event: Any) -> None:
        if event.value:
            self._selected_process = str(event.value)
            self._render_contract_preview()

    def _set_date(self, iso_date: str, menu: Any = None) -> None:
        if not iso_date:
            return
        self._selected_date = iso_date
        if self._date_input is not None:
            self._date_input.value = iso_date
        if menu is not None:
            menu.close()

    def _render_contract_preview(self) -> None:
        if self._contract_preview is None:
            return
        self._contract_preview.clear()
        spec = self._spec_by_id.get(self._selected_process)
        if spec is None:
            return
        with self._contract_preview:
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-1"):
                ui.label("Ресурсный контракт:").classes("text-xs font-medium text-slate-500")
                if spec.allow_parallel_dates:
                    ui.badge("Разные даты: OK", color="positive").props("outline")
                else:
                    ui.badge("Разные даты: Блок staging", color="warning").props("outline")
            with ui.row().classes("items-center gap-1 flex-wrap"):
                ui.label("R:").classes("text-[11px] font-bold text-blue-600")
                for src in spec.read_sources:
                    ui.label(src).classes(
                        "text-[11px] px-1.5 py-0.5 rounded ro-source-pill-read ro-mono"
                    )
                ui.label("W:").classes("text-[11px] font-bold text-red-600 ml-1")
                for src in spec.write_sources:
                    ui.label(src).classes(
                        "text-[11px] px-1.5 py-0.5 rounded ro-source-pill-write ro-mono"
                    )

    async def _handle_submit(self) -> None:
        if not self._selected_process:
            ui.notify("Выберите процесс для запуска", type="warning", position="top-right")
            return
        target_date = (self._selected_date or "").strip()
        if len(target_date) != 10:
            ui.notify("Укажите дату в формате ГГГГ-ММ-ДД", type="warning", position="top-right")
            return

        trigger = TriggerType.AUTO if self._selected_trigger == "AUTO" else TriggerType.MANUAL
        req = TaskRequest(
            process_id=self._selected_process,
            target_date=target_date,
            trigger=trigger,
            priority=self._selected_priority,
        )
        try:
            task = await self._orchestrator.submit(req)
            ui.notify(
                f"Задача #{task.task_id} ({task.process_id}, {task.target_date}) добавлена",
                type="positive",
                position="top-right",
            )
        except Exception as exc:
            ui.notify(f"Ошибка запуска задачи: {exc}", type="negative", position="top-right")

        if self._on_submitted is not None:
            self._on_submitted()

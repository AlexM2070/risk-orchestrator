"""Interactive READ/WRITE Dependency & Pairwise Conflict Matrix widget."""

from __future__ import annotations

from nicegui import ui

from ui.contracts import ProcessSpec, SOURCE_DISPLAY_NAMES


def _analyze_pair_conflict(a: ProcessSpec, b: ProcessSpec) -> tuple[str, str, str]:
    """Return (badge_label, color_classes, detail_tooltip) for running A and B concurrently."""
    if a.process_id == b.process_id:
        if not a.allow_parallel_dates:
            return (
                "Блок staging",
                "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
                "allow_parallel_dates=False: запрещён параллельный запуск экземпляров за разные даты (общий staging-буфер).",
            )
        return (
            "Разные даты OK",
            "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
            "Разрешён параллельный запуск за разные даты; для одной даты действует WRITE-блокировка витрины.",
        )

    a_w, a_r = set(a.write_sources), set(a.read_sources)
    b_w, b_r = set(b.write_sources), set(b.read_sources)

    ww = sorted(a_w & b_w)
    ab_rw = sorted(a_w & b_r)
    ba_rw = sorted(b_w & a_r)

    if ab_rw and ba_rw:
        return (
            "AB-BA Deadlock",
            "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300 font-semibold",
            f"Взаимный READ/WRITE конфликт: {a.process_id} пишет {', '.join(ab_rw)}, "
            f"{b.process_id} пишет {', '.join(ba_rw)} — без оркестратора возникает AB-BA Deadlock.",
        )
    if ww:
        return (
            f"WRITE-WRITE ({', '.join(ww)})",
            "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
            f"Оба процесса перезаписывают {', '.join(ww)} — риск Lost Update.",
        )
    rw = sorted(set(ab_rw) | set(ba_rw))
    if rw:
        return (
            f"READ-WRITE ({', '.join(rw)})",
            "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
            f"Конфликт чтения и записи на витрине {', '.join(rw)} — риск грязного чтения.",
        )

    shared_reads = sorted(a_r & b_r)
    extra = f" (общее чтение {', '.join(shared_reads)} безопасно)" if shared_reads else ""
    return (
        "Параллельно OK",
        "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300 font-medium",
        f"Конфликтов записи нет{extra} — оркестратор запускает их одновременно.",
    )


def render_conflict_matrix(specs: list[ProcessSpec]) -> None:
    """Render the READ/WRITE contracts table and pairwise conflict matrix."""
    if not specs:
        ui.label("Список процессов пуст.").classes("text-sm text-slate-500")
        return

    with ui.column().classes("w-full gap-4"):
        # Section 1: Process resource contracts
        with ui.column().classes("w-full gap-2"):
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
                ui.label("Ресурсные контракты микросервисов (Lock Keys)").classes(
                    "text-sm font-semibold"
                )
                with ui.row().classes("items-center gap-2 text-xs"):
                    ui.label("READ (shared)").classes(
                        "px-2 py-0.5 rounded-full ro-source-pill-read font-medium"
                    )
                    ui.label("WRITE (exclusive)").classes(
                        "px-2 py-0.5 rounded-full ro-source-pill-write font-medium"
                    )

            with ui.element("div").classes("w-full overflow-x-auto"):
                with ui.element("table").classes("ro-matrix-table"):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            for header in (
                                "Микросервис",
                                "Читает (READ)",
                                "Перезаписывает (WRITE)",
                                "Разные даты параллельно",
                                "Приоритет",
                            ):
                                with ui.element("th"):
                                    ui.label(header).classes("text-xs uppercase tracking-wide")
                    with ui.element("tbody"):
                        for spec in specs:
                            with ui.element("tr"):
                                with ui.element("td"):
                                    ui.label(spec.process_id).classes("font-semibold text-sm")
                                    ui.label(spec.display_name).classes("text-xs text-slate-500")
                                with ui.element("td"):
                                    with ui.row().classes("gap-1 flex-wrap"):
                                        for src in spec.read_sources:
                                            pill = ui.label(src).classes(
                                                "text-xs px-2 py-0.5 rounded-full ro-source-pill-read ro-mono"
                                            )
                                            pill.tooltip(SOURCE_DISPLAY_NAMES.get(src, src))
                                with ui.element("td"):
                                    with ui.row().classes("gap-1 flex-wrap"):
                                        for src in spec.write_sources:
                                            pill = ui.label(src).classes(
                                                "text-xs px-2 py-0.5 rounded-full ro-source-pill-write ro-mono"
                                            )
                                            pill.tooltip(SOURCE_DISPLAY_NAMES.get(src, src))
                                with ui.element("td"):
                                    if spec.allow_parallel_dates:
                                        ui.badge("Разрешено", color="positive").props("outline")
                                    else:
                                        ui.badge("Запрещено (staging)", color="warning").props("outline")
                                with ui.element("td"):
                                    ui.badge(f"P{spec.default_priority}", color="primary").props("outline")

        # Section 2: Pairwise matrix (same target_date)
        with ui.column().classes("w-full gap-2"):
            ui.label("Попарная матрица конфликтов (за одну дату расчёта)").classes(
                "text-sm font-semibold"
            )
            with ui.element("div").classes("w-full overflow-x-auto"):
                with ui.element("table").classes("ro-matrix-table"):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            with ui.element("th"):
                                ui.label("Процесс A \\ Процесс B").classes("text-xs")
                            for col_spec in specs:
                                with ui.element("th"):
                                    ui.label(col_spec.process_id).classes("text-xs ro-mono")
                    with ui.element("tbody"):
                        for row_spec in specs:
                            with ui.element("tr"):
                                with ui.element("th"):
                                    ui.label(row_spec.process_id).classes("text-xs ro-mono")
                                for col_spec in specs:
                                    label, badge_cls, tip = _analyze_pair_conflict(row_spec, col_spec)
                                    with ui.element("td"):
                                        chip = ui.label(label).classes(
                                            f"text-xs px-2 py-1 rounded-md inline-block {badge_cls}"
                                        )
                                        chip.tooltip(tip)

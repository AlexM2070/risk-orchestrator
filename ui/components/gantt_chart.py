"""Live Plotly Gantt timeline component showing parallel vs queued execution."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from nicegui import ui
import plotly.graph_objects as go

from ui.contracts import OrchestratorProtocol, status_key, trigger_key


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _normalize_gantt_rows(orchestrator: OrchestratorProtocol) -> list[dict[str, Any]]:
    """Read get_gantt_data() with resilient normalization and fallback to get_tasks()."""
    raw_rows = orchestrator.get_gantt_data() or []
    normalized: list[dict[str, Any]] = []

    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id") or row.get("id")
        process_id = row.get("process_id") or row.get("process") or row.get("task")
        queued_at = _parse_dt(row.get("queued_at"))
        started_at = _parse_dt(row.get("started_at") or row.get("start"))
        finished_at = _parse_dt(row.get("finished_at") or row.get("finish") or row.get("end"))
        if task_id is None or process_id is None or (queued_at is None and started_at is None):
            continue
        normalized.append(
            {
                "task_id": int(task_id),
                "process_id": str(process_id),
                "target_date": str(row.get("target_date") or ""),
                "trigger": trigger_key(row.get("trigger", "MANUAL")),
                "priority": row.get("priority", 2),
                "status": status_key(row.get("status", "RUNNING")),
                "progress": int(row.get("progress", 0) or 0),
                "queued_at": queued_at or started_at,
                "started_at": started_at,
                "finished_at": finished_at,
                "blocked_by": row.get("blocked_by"),
                "error_message": row.get("error_message"),
            }
        )

    if normalized:
        normalized.sort(key=lambda r: r["task_id"])
        return normalized

    # Fallback directly to canonical TaskRun list
    tasks = orchestrator.get_tasks()
    tasks.sort(key=lambda t: t.task_id)
    for t in tasks:
        normalized.append(
            {
                "task_id": t.task_id,
                "process_id": t.process_id,
                "target_date": t.target_date,
                "trigger": trigger_key(t.trigger),
                "priority": t.priority,
                "status": status_key(t.status),
                "progress": int(t.progress),
                "queued_at": t.queued_at,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
                "blocked_by": t.blocked_by,
                "error_message": t.error_message,
            }
        )
    return normalized


def build_gantt_figure(
    rows: list[dict[str, Any]],
    *,
    show_wait: bool = True,
    is_dark: bool = False,
    now: datetime | None = None,
) -> go.Figure:
    """Build a pure-Plotly (no pandas) Gantt chart Figure with wait & execution segments."""
    now = now or datetime.now()
    text_color = "#e2e8f0" if is_dark else "#1e293b"
    muted_color = "#94a3b8" if is_dark else "#64748b"
    grid_color = "rgba(148, 163, 184, 0.16)" if is_dark else "rgba(148, 163, 184, 0.28)"

    fig = go.Figure()

    if not rows:
        fig.add_annotation(
            text="Запустите демо-сценарий или ручную задачу — таймлайн появится в реальном времени",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font={"size": 13, "color": muted_color},
        )
        fig.update_layout(
            height=260,
            margin={"l": 24, "r": 24, "t": 20, "b": 24},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis={"visible": False},
            yaxis={"visible": False},
        )
        return fig

    # Keep latest 25 tasks visible so the Gantt stays clean during heavy stress tests
    visible_rows = rows[-25:]
    labels = [
        f"#{r['task_id']} {r['process_id']} ({r['target_date']}) [{r['trigger']}]"
        for r in visible_rows
    ]

    # Collect per-phase trace arrays
    wait_y, wait_base, wait_dur, wait_hover = [], [], [], []
    status_buckets: dict[str, tuple[list[str], list[str], list[float], list[str], list[str]]] = {
        "RUNNING": ([], [], [], [], []),
        "COMPLETED": ([], [], [], [], []),
        "FAILED": ([], [], [], [], []),
        "CANCELLED": ([], [], [], [], []),
    }

    min_ts: datetime | None = None
    max_ts: datetime | None = None
    has_active = False

    for r, lbl in zip(visible_rows, labels):
        st = r["status"]
        q_at: datetime = r["queued_at"]
        s_at: datetime | None = r["started_at"]
        f_at: datetime | None = r["finished_at"]

        if st in ("RUNNING", "QUEUED"):
            has_active = True

        if min_ts is None or q_at < min_ts:
            min_ts = q_at

        # Wait segment (queued -> started or now)
        wait_end = s_at if s_at is not None else now
        wait_ms = max(0.0, (wait_end - q_at).total_seconds() * 1000.0)
        if show_wait and (wait_ms >= 80.0 or st == "QUEUED"):
            display_wait_ms = max(wait_ms, 180.0)
            wait_y.append(lbl)
            wait_base.append(q_at.isoformat())
            wait_dur.append(display_wait_ms)
            reason = r.get("blocked_by") or "Ожидание планировщика"
            wait_hover.append(
                f"<b>#{r['task_id']} {r['process_id']}</b><br>"
                f"Фаза: <b>Ожидание (QUEUED)</b><br>"
                f"Дата расчёта: {r['target_date']} | {r['trigger']} | P{r['priority']}<br>"
                f"Ожидание: {wait_ms / 1000.0:.2f}с<br>"
                f"Причина: {reason}"
            )
            if max_ts is None or wait_end > max_ts:
                max_ts = wait_end

        # Execution segment (started -> finished or now)
        if s_at is not None:
            run_end = f_at if f_at is not None else now
            run_ms = max(160.0, (run_end - s_at).total_seconds() * 1000.0)
            bucket_key = st if st in status_buckets else "COMPLETED"
            by, bbase, bdur, btxt, bhover = status_buckets[bucket_key]
            by.append(lbl)
            bbase.append(s_at.isoformat())
            bdur.append(run_ms)
            if st == "RUNNING":
                btxt.append(f"{r['progress']}%")
            elif st == "FAILED":
                btxt.append("СБОЙ")
            elif st == "CANCELLED":
                btxt.append("ОТМ.")
            else:
                btxt.append(f"{run_ms / 1000.0:.1f}с")

            err_line = f"<br><b>Ошибка:</b> {r['error_message']}" if r.get("error_message") else ""
            bhover.append(
                f"<b>#{r['task_id']} {r['process_id']}</b><br>"
                f"Статус: <b>{st}</b><br>"
                f"Дата расчёта: {r['target_date']} | {r['trigger']} | P{r['priority']}<br>"
                f"Ожидание: {wait_ms / 1000.0:.2f}с | Выполнение: {run_ms / 1000.0:.2f}с"
                f"{err_line}"
            )
            if max_ts is None or run_end > max_ts:
                max_ts = run_end

    if wait_y:
        fig.add_trace(
            go.Bar(
                name="Ожидание (QUEUED)",
                orientation="h",
                y=wait_y,
                base=wait_base,
                x=wait_dur,
                marker={
                    "color": "rgba(245, 158, 11, 0.32)",
                    "line": {"color": "#d97706", "width": 1.2},
                },
                hovertext=wait_hover,
                hovertemplate="%{hovertext}<extra></extra>",
                width=0.58,
            )
        )

    style_map = {
        "RUNNING": ("Выполняется (RUNNING)", "#2563eb", "#1d4ed8"),
        "COMPLETED": ("Завершено (COMPLETED)", "#10b981", "#047857"),
        "FAILED": ("Сбой (FAILED)", "#ef4444", "#b91c1c"),
        "CANCELLED": ("Отменено (CANCELLED)", "#94a3b8", "#64748b"),
    }

    for st_key, (trace_name, fill_color, line_color) in style_map.items():
        by, bbase, bdur, btxt, bhover = status_buckets[st_key]
        if not by:
            continue
        fig.add_trace(
            go.Bar(
                name=trace_name,
                orientation="h",
                y=by,
                base=bbase,
                x=bdur,
                text=btxt,
                textposition="inside",
                insidetextanchor="middle",
                textfont={"color": "#ffffff", "size": 11},
                marker={
                    "color": fill_color,
                    "line": {"color": line_color, "width": 1},
                },
                hovertext=bhover,
                hovertemplate="%{hovertext}<extra></extra>",
                width=0.58,
            )
        )

    if min_ts is not None and max_ts is not None:
        span_sec = max(4.0, (max_ts - min_ts).total_seconds())
        pad = timedelta(seconds=max(0.5, span_sec * 0.06))
        x_range = [(min_ts - timedelta(seconds=0.3)).isoformat(), (max_ts + pad).isoformat()]
    else:
        x_range = None

    if has_active:
        fig.add_vline(
            x=now.isoformat(),
            line_width=1.5,
            line_dash="dot",
            line_color="#6366f1",
        )

    chart_height = max(290, min(620, 115 + 34 * len(visible_rows)))

    fig.update_layout(
        barmode="overlay",
        height=chart_height,
        margin={"l": 10, "r": 20, "t": 36, "b": 34},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": text_color, "size": 12},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 11},
        },
        xaxis={
            "type": "date",
            "range": x_range,
            "tickformat": "%H:%M:%S",
            "gridcolor": grid_color,
            "showline": True,
            "linecolor": grid_color,
            "title": {"text": "Время выполнения (параллельные полосы = одновременная работа)", "font": {"size": 11, "color": muted_color}},
        },
        yaxis={
            "categoryorder": "array",
            "categoryarray": list(reversed(labels)),
            "automargin": True,
            "gridcolor": grid_color,
            "tickfont": {"size": 11},
        },
    )
    return fig


class GanttChartPanel:
    """NiceGUI wrapper hosting a live-updating Plotly Gantt timeline."""

    def __init__(self, orchestrator: OrchestratorProtocol) -> None:
        self._orchestrator = orchestrator
        self._show_wait: bool = True
        self._is_dark: bool = False
        self._plot: Any = None
        self._summary_lbl: Any = None
        self._last_fp: Any = None

    def set_dark(self, is_dark: bool) -> None:
        if self._is_dark != is_dark:
            self._is_dark = is_dark
            self.refresh(force=True)

    def build(self) -> None:
        with ui.column().classes("ro-card p-4 w-full gap-2"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("view_timeline", size="sm").classes("text-indigo-600")
                    ui.label("Таймлайн выполнения (Диаграмма Ганта)").classes("text-base font-semibold")
                    self._summary_lbl = ui.label("").classes("text-xs text-slate-500")

                ui.switch(
                    "Показывать ожидание в очереди",
                    value=self._show_wait,
                    on_change=self._toggle_wait,
                ).props("dense color=amber-8")

            initial_fig = build_gantt_figure([], show_wait=self._show_wait, is_dark=self._is_dark)
            self._plot = ui.plotly(initial_fig).classes("w-full")

        self.refresh(force=True)

    def _toggle_wait(self, event: Any) -> None:
        self._show_wait = bool(event.value)
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        if self._plot is None:
            return
        rows = _normalize_gantt_rows(self._orchestrator)
        has_active = any(r["status"] in ("RUNNING", "QUEUED") for r in rows)
        now = datetime.now()

        # Advance timeline smoothly every ~0.5s while tasks are active; static once idle
        clock_bucket = int(now.timestamp() * 2) if has_active else 0
        fp = (
            self._show_wait,
            self._is_dark,
            clock_bucket,
            tuple(
                (r["task_id"], r["status"], r["progress"], r["started_at"], r["finished_at"])
                for r in rows[-25:]
            ),
        )
        if not force and fp == self._last_fp:
            return
        self._last_fp = fp

        if self._summary_lbl is not None:
            if rows:
                shown = min(len(rows), 25)
                self._summary_lbl.text = f"Отображено задач: {shown} из {len(rows)}"
            else:
                self._summary_lbl.text = "Ожидание запусков"

        fig = build_gantt_figure(rows, show_wait=self._show_wait, is_dark=self._is_dark, now=now)
        self._plot.update_figure(fig)

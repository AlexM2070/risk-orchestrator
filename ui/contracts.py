"""Contract bridge between UI and Core/Services modules.

Imports canonical data models from ``core.models`` and ``services.scenarios`` when
available, and falls back to exact Section 5 specification definitions so the UI
can run standalone against ``MockOrchestrator`` before branch integration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

PROCESS_STOCKOUT = "StockoutRisk"
PROCESS_DELAY = "DelayRisk"
PROCESS_SPOILAGE = "SpoilageRisk"
PROCESS_CARRIER_DEFAULT = "CarrierDefaultRisk"

ALL_PROCESS_IDS: tuple[str, ...] = (
    PROCESS_STOCKOUT,
    PROCESS_DELAY,
    PROCESS_SPOILAGE,
    PROCESS_CARRIER_DEFAULT,
)

SOURCE_WAREHOUSE_STOCKS = "WarehouseStocks"
SOURCE_TRANSIT_TELEMETRY = "TransitTelemetry"
SOURCE_WEATHER_TRAFFIC = "WeatherAndTraffic"
SOURCE_SUPPLIER_SLA = "SupplierSLA"

ALL_SOURCE_IDS: tuple[str, ...] = (
    SOURCE_WAREHOUSE_STOCKS,
    SOURCE_TRANSIT_TELEMETRY,
    SOURCE_WEATHER_TRAFFIC,
    SOURCE_SUPPLIER_SLA,
)

SOURCE_DISPLAY_NAMES: dict[str, str] = {
    SOURCE_WAREHOUSE_STOCKS: "Складские остатки",
    SOURCE_TRANSIT_TELEMETRY: "Телеметрия грузов",
    SOURCE_WEATHER_TRAFFIC: "Погода и трафик",
    SOURCE_SUPPLIER_SLA: "SLA поставщиков",
}

USING_CORE_MODELS = False
USING_SERVICES_SCENARIOS = False

try:
    from core.models import (  # type: ignore[import-not-found]
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

    USING_CORE_MODELS = True
except ImportError:

    class TaskStatus(Enum):
        QUEUED = "QUEUED"
        RUNNING = "RUNNING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"
        CANCELLED = "CANCELLED"

    class TriggerType(Enum):
        AUTO = "AUTO"
        MANUAL = "MANUAL"

    @dataclass
    class ProcessSpec:
        process_id: str
        display_name: str
        read_sources: tuple[str, ...]
        write_sources: tuple[str, ...]
        allow_parallel_dates: bool
        default_priority: int

    @dataclass
    class TaskRequest:
        process_id: str
        target_date: str
        trigger: TriggerType
        priority: int | None = None
        delay_sec: float = 0.0

    @dataclass
    class TaskRun:
        task_id: int
        process_id: str
        target_date: str
        trigger: TriggerType
        priority: int
        status: TaskStatus
        progress: int
        queued_at: datetime
        started_at: datetime | None = None
        finished_at: datetime | None = None
        blocked_by: str | None = None
        error_message: str | None = None
        result: dict[str, Any] | None = None

    @dataclass
    class OrchestratorEvent:
        timestamp: datetime
        level: str
        task_id: int | None
        message: str

    ProgressCallback = Callable[[int, int], Awaitable[None]]
    StateChangeCallback = Callable[[], Awaitable[None] | None]

    class DeadlockError(RuntimeError):
        """AB-BA mutual deadlock error in non-orchestrated mode."""

    class DataCorruptionError(RuntimeError):
        """Lost update or dirty read error in non-orchestrated mode."""

    class CrossDateCollisionError(RuntimeError):
        """Same-process cross-date staging buffer collision error."""


try:
    from services.scenarios import ScenarioSpec  # type: ignore[import-not-found]

    USING_SERVICES_SCENARIOS = True
except ImportError:

    @dataclass
    class ScenarioSpec:
        scenario_id: str
        title: str
        description: str
        tasks: list[TaskRequest]


@runtime_checkable
class OrchestratorProtocol(Protocol):
    """Public UI <-> Core contract defined in Section 5.4."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def set_orchestration_enabled(self, enabled: bool) -> None: ...
    def is_orchestration_enabled(self) -> bool: ...
    async def submit(self, request: TaskRequest) -> TaskRun: ...
    async def submit_batch(self, requests: list[TaskRequest]) -> list[TaskRun]: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    async def reset(self) -> None: ...
    def subscribe(self, callback: StateChangeCallback) -> None: ...

    def get_process_specs(self) -> list[ProcessSpec]: ...
    def get_tasks(self, status: TaskStatus | None = None) -> list[TaskRun]: ...
    def get_active_locks(self) -> dict[str, list[int]]: ...
    def get_events(self, limit: int = 100) -> list[OrchestratorEvent]: ...
    def get_gantt_data(self) -> list[dict[str, Any]]: ...
    def get_metrics(self) -> dict[str, Any]: ...


@runtime_checkable
class SchedulerProtocol(Protocol):
    """Public UI <-> ReglamentScheduler contract defined in Section 5.4."""

    def set_enabled(self, enabled: bool) -> None: ...
    def set_interval(self, seconds: float) -> None: ...
    def is_enabled(self) -> bool: ...


RandomBatchGenerator = Callable[[int, int | None], list[TaskRequest]]


def status_key(status: TaskStatus | str) -> str:
    """Normalize TaskStatus enum or string to canonical uppercase name."""
    if isinstance(status, Enum):
        return status.name.upper()
    return str(status).upper()


def trigger_key(trigger: TriggerType | str) -> str:
    """Normalize TriggerType enum or string to canonical uppercase name."""
    if isinstance(trigger, Enum):
        return trigger.name.upper()
    return str(trigger).upper()


def classify_error(error_message: str | None) -> str:
    """Classify a failed task's error message into one of the 3 case failure modes."""
    if not error_message:
        return "other"
    lowered = error_message.lower()
    if "deadlock" in lowered or "взаимн" in lowered or "ab-ba" in lowered:
        return "deadlock"
    if (
        "corruption" in lowered
        or "lost update" in lowered
        or "грязн" in lowered
        or "перезапис" in lowered
        or "порч" in lowered
    ):
        return "corruption"
    if (
        "crossdate" in lowered
        or "cross-date" in lowered
        or "staging" in lowered
        or "разные даты" in lowered
        or "коллиз" in lowered
    ):
        return "cross_date"
    return "other"


__all__ = [
    "ALL_PROCESS_IDS",
    "ALL_SOURCE_IDS",
    "CrossDateCollisionError",
    "DataCorruptionError",
    "DeadlockError",
    "OrchestratorEvent",
    "OrchestratorProtocol",
    "PROCESS_CARRIER_DEFAULT",
    "PROCESS_DELAY",
    "PROCESS_SPOILAGE",
    "PROCESS_STOCKOUT",
    "ProcessSpec",
    "ProgressCallback",
    "RandomBatchGenerator",
    "SOURCE_DISPLAY_NAMES",
    "SOURCE_SUPPLIER_SLA",
    "SOURCE_TRANSIT_TELEMETRY",
    "SOURCE_WAREHOUSE_STOCKS",
    "SOURCE_WEATHER_TRAFFIC",
    "ScenarioSpec",
    "SchedulerProtocol",
    "StateChangeCallback",
    "TaskRequest",
    "TaskRun",
    "TaskStatus",
    "TriggerType",
    "USING_CORE_MODELS",
    "USING_SERVICES_SCENARIOS",
    "classify_error",
    "status_key",
    "trigger_key",
]

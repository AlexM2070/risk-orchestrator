"""NiceGUI dashboard package for Risk Orchestrator."""

from __future__ import annotations

from typing import Any

__all__ = ["create_app", "resolve_scenarios", "run_ui"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from ui import app as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

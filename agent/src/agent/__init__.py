"""Agent core package with lazy public exports.

Importing a lightweight agent submodule should not eagerly initialize the
full ReAct loop and its provider/tool dependency graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = ["AgentLoop", "WorkspaceMemory", "SkillsLoader", "BaseTool", "ToolRegistry"]

_EXPORTS = {
    "AgentLoop": ("src.agent.loop", "AgentLoop"),
    "WorkspaceMemory": ("src.agent.memory", "WorkspaceMemory"),
    "SkillsLoader": ("src.agent.skills", "SkillsLoader"),
    "BaseTool": ("src.agent.tools", "BaseTool"),
    "ToolRegistry": ("src.agent.tools", "ToolRegistry"),
}


def __getattr__(name: str) -> Any:
    """Load public agent classes only when callers actually request them."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from src.agent.loop import AgentLoop
    from src.agent.memory import WorkspaceMemory
    from src.agent.skills import SkillsLoader
    from src.agent.tools import BaseTool, ToolRegistry

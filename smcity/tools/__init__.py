"""Tool registry — every external read goes through here."""

from smcity.tools.context import (
    ACTIVE_WARNINGS_TOOL,
    AQHI_TOOL,
    CURRENT_WEATHER_TOOL,
)
from smcity.tools.geo import ADDRESS_LOOKUP_TOOL
from smcity.tools.meta import ASK_USER_TOOL, WHAT_LANGUAGES_TOOL
from smcity.tools.registry import ToolError, ToolRegistry, ToolResult, ToolSpec
from smcity.tools.transport import MTR_NEXT_TRAINS_TOOL


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in (
        ADDRESS_LOOKUP_TOOL,
        MTR_NEXT_TRAINS_TOOL,
        CURRENT_WEATHER_TOOL,
        ACTIVE_WARNINGS_TOOL,
        AQHI_TOOL,
        ASK_USER_TOOL,
        WHAT_LANGUAGES_TOOL,
    ):
        registry.register(spec)
    return registry


__all__ = [
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_default_registry",
]

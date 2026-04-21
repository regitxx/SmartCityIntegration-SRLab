"""Tool registry — every external read goes through here."""

from smcity.tools.context import (
    ACTIVE_WARNINGS_TOOL,
    AQHI_TOOL,
    CURRENT_WEATHER_TOOL,
)
from smcity.tools.facility import FIND_NEARBY_COURTS_TOOL, FIND_NEARBY_POOLS_TOOL
from smcity.tools.geo import ADDRESS_LOOKUP_TOOL
from smcity.tools.housing import GET_ESTATE_INFO_TOOL, LIST_ESTATES_TOOL
from smcity.tools.meta import ASK_USER_TOOL, WHAT_LANGUAGES_TOOL
from smcity.tools.registry import ToolError, ToolRegistry, ToolResult, ToolSpec
from smcity.tools.transport import MTR_NEXT_TRAINS_TOOL
from smcity.tools.transport_citybus import CITYBUS_ETA_TOOL, CITYBUS_ROUTE_STOPS_TOOL
from smcity.tools.transport_kmb import KMB_ETA_BY_ROUTE_STOP_TOOL, KMB_ETA_BY_STOP_TOOL
from smcity.tools.transport_search import (
    FIND_STOPS_BY_NAME_TOOL,
    FIND_STOPS_NEAR_POINT_TOOL,
)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in (
        # geo
        ADDRESS_LOOKUP_TOOL,
        # transport
        MTR_NEXT_TRAINS_TOOL,
        KMB_ETA_BY_STOP_TOOL,
        KMB_ETA_BY_ROUTE_STOP_TOOL,
        CITYBUS_ETA_TOOL,
        CITYBUS_ROUTE_STOPS_TOOL,
        FIND_STOPS_NEAR_POINT_TOOL,
        FIND_STOPS_BY_NAME_TOOL,
        # context
        CURRENT_WEATHER_TOOL,
        ACTIVE_WARNINGS_TOOL,
        AQHI_TOOL,
        # facility
        FIND_NEARBY_COURTS_TOOL,
        FIND_NEARBY_POOLS_TOOL,
        # housing
        GET_ESTATE_INFO_TOOL,
        LIST_ESTATES_TOOL,
        # meta
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

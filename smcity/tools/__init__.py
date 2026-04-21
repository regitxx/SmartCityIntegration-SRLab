"""Tool registry — every external read goes through here."""

from smcity.tools.context import (
    ACTIVE_WARNINGS_TOOL,
    AQHI_TOOL,
    CURRENT_WEATHER_TOOL,
    NINE_DAY_FORECAST_TOOL,
)
from smcity.tools.facility import FIND_NEARBY_COURTS_TOOL, FIND_NEARBY_POOLS_TOOL
from smcity.tools.geo import ADDRESS_LOOKUP_TOOL
from smcity.tools.housing import GET_ESTATE_INFO_TOOL, LIST_ESTATES_TOOL
from smcity.tools.meta import ASK_USER_TOOL, FORGET_ME_TOOL, WHAT_LANGUAGES_TOOL
from smcity.tools.osm_pois import SEARCH_OSM_POIS_TOOL
from smcity.tools.registry import ToolError, ToolRegistry, ToolResult, ToolSpec
from smcity.tools.transport import MTR_NEXT_TRAINS_TOOL
from smcity.tools.transport_citybus import CITYBUS_ETA_TOOL, CITYBUS_ROUTE_STOPS_TOOL
from smcity.tools.transport_gmb import GMB_ETA_TOOL
from smcity.tools.transport_kmb import KMB_ETA_BY_ROUTE_STOP_TOOL, KMB_ETA_BY_STOP_TOOL
from smcity.tools.transport_planner import PLAN_SIMPLE_ROUTE_TOOL
from smcity.tools.transport_search import (
    FIND_STOPS_BY_NAME_TOOL,
    FIND_STOPS_NEAR_POINT_TOOL,
)
from smcity.tools.transport_simple_modes import (
    PLAN_JOURNEY_TOOL,
    PLAN_TAXI_TOOL,
    PLAN_WALKING_TOOL,
)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in (
        # geo
        ADDRESS_LOOKUP_TOOL,
        SEARCH_OSM_POIS_TOOL,
        # transport
        MTR_NEXT_TRAINS_TOOL,
        KMB_ETA_BY_STOP_TOOL,
        KMB_ETA_BY_ROUTE_STOP_TOOL,
        CITYBUS_ETA_TOOL,
        CITYBUS_ROUTE_STOPS_TOOL,
        GMB_ETA_TOOL,
        FIND_STOPS_NEAR_POINT_TOOL,
        FIND_STOPS_BY_NAME_TOOL,
        PLAN_SIMPLE_ROUTE_TOOL,
        PLAN_WALKING_TOOL,
        PLAN_TAXI_TOOL,
        PLAN_JOURNEY_TOOL,
        # context
        CURRENT_WEATHER_TOOL,
        ACTIVE_WARNINGS_TOOL,
        AQHI_TOOL,
        NINE_DAY_FORECAST_TOOL,
        # facility
        FIND_NEARBY_COURTS_TOOL,
        FIND_NEARBY_POOLS_TOOL,
        # housing
        GET_ESTATE_INFO_TOOL,
        LIST_ESTATES_TOOL,
        # meta
        ASK_USER_TOOL,
        WHAT_LANGUAGES_TOOL,
        FORGET_ME_TOOL,
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

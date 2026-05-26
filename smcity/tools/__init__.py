"""Tool registry — every external read goes through here."""

from typing import Any

from smcity.tools.context import (
    ACTIVE_WARNINGS_TOOL,
    AQHI_TOOL,
    CURRENT_WEATHER_TOOL,
    NINE_DAY_FORECAST_TOOL,
)
from smcity.tools.csdi import CSDI_QUERY_TOOL
from smcity.tools.facility import FIND_NEARBY_COURTS_TOOL, FIND_NEARBY_POOLS_TOOL
from smcity.tools.geo import ADDRESS_LOOKUP_TOOL
from smcity.tools.housing import GET_ESTATE_INFO_TOOL, LIST_ESTATES_TOOL
from smcity.tools.meta import ASK_USER_TOOL, FORGET_ME_TOOL, WHAT_LANGUAGES_TOOL
from smcity.tools.osm_pois import FIND_POI_TOOL
from smcity.tools.otp2 import PLAN_MULTIMODAL_JOURNEY_TOOL
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
    PLAN_WALKING_TOOL,
)


def build_default_registry() -> ToolRegistry:
    # Annotated as `ToolSpec[Any, Any]` because the catalog is heterogeneous
    # in its generic parameters (each tool has its own ArgsT / ResultT). The
    # registry's `.register()` accepts any `ToolSpec[Any, Any]`, but a bare
    # tuple of mixed-generic specs upcasts to `tuple[object, ...]`, which
    # mypy strict rejects. Explicit annotation pins the variance.
    registry = ToolRegistry()
    specs: list[ToolSpec[Any, Any]] = [
        # geo
        ADDRESS_LOOKUP_TOOL,
        # v0.6.0 — single geo.find_poi tool, category Literal over 30 slugs.
        # Replaces the per-category geo.find_dentist / geo.find_convenience_store
        # / … fleet (12K tokens of duplicated schema in every prompt).
        FIND_POI_TOOL,
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
        PLAN_JOURNEY_TOOL,
        PLAN_MULTIMODAL_JOURNEY_TOOL,
        # context
        CURRENT_WEATHER_TOOL,
        ACTIVE_WARNINGS_TOOL,
        AQHI_TOOL,
        NINE_DAY_FORECAST_TOOL,
        # facility
        FIND_NEARBY_COURTS_TOOL,
        FIND_NEARBY_POOLS_TOOL,
        # CSDI (generic live FeatureServer query)
        CSDI_QUERY_TOOL,
        # housing
        GET_ESTATE_INFO_TOOL,
        LIST_ESTATES_TOOL,
        # meta
        ASK_USER_TOOL,
        WHAT_LANGUAGES_TOOL,
        FORGET_ME_TOOL,
    ]
    for spec in specs:
        registry.register(spec)
    return registry


__all__ = [
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_default_registry",
]

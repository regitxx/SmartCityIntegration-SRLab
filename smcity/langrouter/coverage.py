"""Dataset language-coverage matrix.

Source of truth for: "does upstream X natively accept language Y?".
When the answer is no, the orchestrator routes through the translation
fallback (in Phase 1a: let the LLM translate; Phase 2 adds NLLB-200).
"""

from __future__ import annotations

from typing import Literal

SupportedLang = Literal["en", "zh-Hant", "zh-Hans"]

# Per-tool native language support, derived from docs/research/01_*.md and
# docs/research/02_*.md. Keys are tool namespaces — see TOOL_CATALOG.md.
DATASET_COVERAGE: dict[str, set[SupportedLang]] = {
    # Transport — from 01_datagovhk_transport_apis.md
    "transport.get_mtr_next_trains": {"en", "zh-Hant"},
    "transport.get_kmb_eta_by_stop": {"en", "zh-Hant", "zh-Hans"},
    "transport.get_kmb_eta_by_route_stop": {"en", "zh-Hant", "zh-Hans"},
    "transport.get_citybus_eta_by_route_stop": {"en", "zh-Hant", "zh-Hans"},
    "transport.get_citybus_route_stops": {"en", "zh-Hant", "zh-Hans"},
    "transport.find_stops_near_point": {"en", "zh-Hant", "zh-Hans"},
    "transport.find_stops_by_name": {"en", "zh-Hant", "zh-Hans"},
    "transport.plan_simple_route": {"en", "zh-Hant"},
    "transport.plan_walking_route": {"en", "zh-Hant"},
    "transport.plan_journey": {"en", "zh-Hant"},
    "transport.get_gmb_eta": {"en", "zh-Hant", "zh-Hans"},
    "transport.plan_multimodal_journey": {"en", "zh-Hant"},  # OTP2 + HK GTFS feeds
    # Context
    "context.get_9day_forecast": {"en", "zh-Hant", "zh-Hans"},
    # Geo — OpenStreetMap (multilingual tags; EN/繁體 usually populated).
    # Per-category POI tools are inserted at module load via the loop below
    # so we don't repeat 30 entries here.
    # Context — from 02_datagovhk_housing_context_apis.md
    "context.get_current_weather": {"en", "zh-Hant", "zh-Hans"},
    "context.get_active_warnings": {"en", "zh-Hant", "zh-Hans"},
    "context.get_aqhi": {"en", "zh-Hant"},
    # Geo
    "geo.address_lookup": {"en", "zh-Hant"},
    # Facility (live CSDI — both EN and 繁體 native on the FeatureServer).
    "facility.find_nearby_courts": {"en", "zh-Hant"},
    "facility.find_nearby_pools": {"en", "zh-Hant"},
    # Housing (live HKHA JSON — EN-native; 繁體 via hkha_name_map_tc.json overlay).
    "housing.get_estate_info": {"en", "zh-Hant"},
    "housing.list_estates_in_district": {"en", "zh-Hant"},
    # CSDI generic ArcGIS FeatureServer querier (bilingual attributes per dataset).
    "csdi.query_features": {"en", "zh-Hant"},
}


# Populate the OSM POI tool entry from the same source of truth used by
# the registry. Done at module-import time so DATASET_COVERAGE stays a
# flat dict for everything downstream. v0.6.0: was 30 entries (one per
# category); collapsed to the single geo.find_poi tool.
def _seed_osm_poi_coverage() -> None:
    from smcity.tools.osm_pois import POI_TOOL  # local import — avoids cycle at module top

    DATASET_COVERAGE[POI_TOOL] = {"en", "zh-Hant", "zh-Hans"}


_seed_osm_poi_coverage()

# Maps our internal primary_lang codes → the user's query language tag to use
# when calling upstream. Cantonese and Mandarin collapse to zh-Hant for the
# native path; non-CJK defaults to en.
_PRIMARY_TO_QUERY_LANG: dict[str, SupportedLang] = {
    "yue": "zh-Hant",
    "zho": "zh-Hant",  # refined below when script=Hans
    "eng": "en",
}


def choose_query_lang(tool_name: str, primary_lang: str, script: str) -> tuple[SupportedLang, bool]:
    """Pick the best language param for `tool_name` and flag if translation is needed.

    Returns `(query_lang, translation_applied)`.
    - `translation_applied=True` means the user wrote in a language the dataset
      doesn't natively serve; the orchestrator will translate the query before
      the tool call and translate the response back at the end.
    """
    supported = DATASET_COVERAGE.get(tool_name, {"en"})

    # Cantonese is never natively served by data.gov.hk — map to the closest
    # supported language and flag translation on.
    if primary_lang == "yue":
        if "zh-Hant" in supported:
            return "zh-Hant", True
        if "zh-Hans" in supported:
            return "zh-Hans", True
        return "en", True

    # Natively supported path for Mandarin + English.
    direct = _PRIMARY_TO_QUERY_LANG.get(primary_lang)
    if primary_lang == "zho" and script == "Hans":
        direct = "zh-Hans"
    if direct and direct in supported:
        return direct, False

    # Fallback to English with translation flag.
    return "en", True


def is_natively_supported(tool_name: str, primary_lang: str, script: str) -> bool:
    _, translated = choose_query_lang(tool_name, primary_lang, script)
    return not translated

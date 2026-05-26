"""Dataset catalogue the fuzzer probes — seed material for synth prompts.

Mirrors `docs/DATASETS.md` but condenses the 35 xlsx IDs + live extras
into ~20 categories that each map onto one production tool. For each
category the synth LLM is told: "a user wants something from this
category — ask a realistic question about it."

We intentionally DON'T hand-write the questions here; the synth LLM
invents them based on the persona + description. This file just tells
it what topics exist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class DatasetTopic:
    id: str
    title_en: str
    title_tc: str
    expected_tools: tuple[str, ...]
    description_en: str


TOPICS: tuple[DatasetTopic, ...] = (
    # --- Transportation (live ETAs) ---------------------------------------
    DatasetTopic(
        id="mtr_next_trains",
        title_en="MTR next trains",
        title_tc="港鐵下班車時間",
        expected_tools=("transport.get_mtr_next_trains",),
        description_en="Live arrival times at an MTR station (e.g. 'next train at Central').",
    ),
    DatasetTopic(
        id="kmb_eta",
        title_en="KMB bus ETA",
        title_tc="九巴到站時間",
        expected_tools=("transport.get_kmb_eta_by_stop", "transport.get_kmb_eta_by_route_stop"),
        description_en="Live arrival time for a KMB bus at a stop or route-stop.",
    ),
    DatasetTopic(
        id="citybus_eta",
        title_en="Citybus ETA",
        title_tc="城巴到站時間",
        expected_tools=(
            "transport.get_citybus_eta_by_route_stop",
            "transport.get_citybus_route_stops",
        ),
        description_en="Live Citybus arrivals for a route + stop pair.",
    ),
    DatasetTopic(
        id="gmb_eta",
        title_en="Green minibus ETA",
        title_tc="綠色專線小巴到站時間",
        expected_tools=("transport.get_gmb_eta",),
        description_en="Live green minibus arrivals — user may not know route numbers.",
    ),
    DatasetTopic(
        id="journey_planning",
        title_en="Multimodal journey planning",
        title_tc="路線規劃",
        expected_tools=(
            "transport.plan_journey",
            "transport.plan_simple_route",
            "transport.plan_walking_route",
        ),
        description_en="Get from A to B by MTR / bus / walk; user may not specify mode.",
    ),
    DatasetTopic(
        id="stops_near_me",
        title_en="Nearby transit stops",
        title_tc="附近巴士站/地鐵站",
        expected_tools=("transport.find_stops_near_point", "transport.find_stops_by_name"),
        description_en="Find bus / MTR stops near a location or by name.",
    ),
    # --- Weather / warnings -----------------------------------------------
    DatasetTopic(
        id="current_weather",
        title_en="Current weather",
        title_tc="目前天氣",
        expected_tools=("context.get_current_weather",),
        description_en="Temperature, humidity, rainfall right now.",
    ),
    DatasetTopic(
        id="weather_warnings",
        title_en="Active weather warnings",
        title_tc="天氣警告",
        expected_tools=("context.get_active_warnings",),
        description_en="Typhoon / rainstorm / hot weather signals in effect.",
    ),
    DatasetTopic(
        id="nine_day_forecast",
        title_en="9-day forecast",
        title_tc="九天天氣預報",
        expected_tools=("context.get_9day_forecast",),
        description_en="Outlook for the coming 9 days — user may ask about a weekend trip.",
    ),
    DatasetTopic(
        id="air_quality",
        title_en="Air Quality Health Index",
        title_tc="空氣質素健康指數",
        expected_tools=("context.get_aqhi",),
        description_en="EPD AQHI per station — jogging / asthma-relevant.",
    ),
    # --- Facilities (live CSDI) -------------------------------------------
    DatasetTopic(
        id="basketball_courts",
        title_en="LCSD basketball courts",
        title_tc="康文署籃球場",
        expected_tools=("facility.find_nearby_courts",),
        description_en="Free or bookable public basketball courts across HK.",
    ),
    DatasetTopic(
        id="swimming_pools",
        title_en="LCSD swimming pools",
        title_tc="康文署游泳池",
        expected_tools=("facility.find_nearby_pools",),
        description_en="Public swimming pools; hours vary seasonally.",
    ),
    # --- Housing (bundled) ------------------------------------------------
    DatasetTopic(
        id="public_housing",
        title_en="Public rental housing estates",
        title_tc="公共屋邨",
        expected_tools=("housing.get_estate_info", "housing.list_estates_in_district"),
        description_en=(
            "Info about HKHA estates. NOTE: the agent must REFUSE to advise "
            "on personal eligibility / applications — those are legal and "
            "redirected to the official portal."
        ),
    ),
    # --- OSM POIs (shared tool, many categories) --------------------------
    DatasetTopic(
        id="public_toilets",
        title_en="Public toilets",
        title_tc="公廁",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Nearest public toilets — a common 'I need this now' query. "
            "Agent should call geo.find_poi with category='public_toilet'."
        ),
    ),
    DatasetTopic(
        id="convenience_stores",
        title_en="Convenience stores",
        title_tc="便利店",
        expected_tools=("geo.find_poi",),
        description_en=(
            "7-Eleven, Circle K, Vango — anywhere, 24h. Agent should call "
            "geo.find_poi with category='convenience_store'."
        ),
    ),
    DatasetTopic(
        id="places_of_worship",
        title_en="Places of worship",
        title_tc="宗教場所",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Temples, churches, mosques, monasteries. Agent should call "
            "geo.find_poi with category='place_of_worship'."
        ),
    ),
    DatasetTopic(
        id="mtr_station_entrances",
        title_en="MTR station entrances",
        title_tc="港鐵站出入口",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Specific lettered entrance (A1, B, D) — useful for meetups. "
            "Agent should call geo.find_poi with category='mtr_station_entrance'."
        ),
    ),
    DatasetTopic(
        id="drinking_water",
        title_en="Drinking water fountains",
        title_tc="飲水機",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Free refill spots, parks, sports grounds. Agent should call "
            "geo.find_poi with category='drinking_water'."
        ),
    ),
    DatasetTopic(
        id="benches_shelters",
        title_en="Benches + shelters",
        title_tc="座椅同避雨亭",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Rest / shade for elderly walkers or hikers. Agent should call "
            "geo.find_poi with category='bench' or category='shelter'."
        ),
    ),
    DatasetTopic(
        id="dentists",
        title_en="Dentists",
        title_tc="牙醫",
        expected_tools=("geo.find_poi",),
        description_en=(
            "Private dental clinics — useful for urgent pain. Agent should "
            "call geo.find_poi with category='dentist'."
        ),
    ),
    # --- Geocoding --------------------------------------------------------
    DatasetTopic(
        id="address_lookup",
        title_en="Address lookup",
        title_tc="地址搜尋",
        expected_tools=("geo.address_lookup",),
        description_en="Resolve a free-text address to lat/lng + canonical EN/TC names.",
    ),
    # --- Meta / refusal -----------------------------------------------------
    DatasetTopic(
        id="language_support",
        title_en="Supported languages",
        title_tc="支援嘅語言",
        expected_tools=("meta.what_languages_are_supported",),
        description_en="User asks what languages the agent speaks.",
    ),
)


def by_id(topic_id: str) -> DatasetTopic:
    for t in TOPICS:
        if t.id == topic_id:
            return t
    raise KeyError(f"unknown topic: {topic_id!r}")

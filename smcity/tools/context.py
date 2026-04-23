"""Context tools — HKO weather + warnings, EPD AQHI."""

from __future__ import annotations

import re
from typing import Literal
from xml.etree import ElementTree as ET

import httpx
from pydantic import BaseModel, Field

from smcity.tools.registry import ToolContext, ToolSpec, ToolUpstreamError

HKO_BASE = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
# Per-station RSS — the legacy JSON API was retired and now returns 404.
# This feed publishes one <item> per monitoring station with the AQHI band,
# health-risk label, and timestamp in the <description> CDATA.
AQHI_STATIONS_URL = "https://www.aqhi.gov.hk/epd/ddata/html/out/aqhi_ind_rss_Eng.xml"


def _lang_param(query_lang: str) -> Literal["en", "tc", "sc"]:
    if query_lang == "zh-Hans":
        return "sc"
    if query_lang == "zh-Hant":
        return "tc"
    return "en"


# --- weather ---------------------------------------------------------------


class CurrentWeatherArgs(BaseModel):
    pass


class CurrentWeatherResult(BaseModel):
    update_time: str | None = None
    temperature_c: float | None = None
    humidity_pct: int | None = None
    rainfall_mm_past_hour: float | None = None
    uv_index: float | None = None
    general_summary: str | None = None
    source: str = "hko.rhrread"


async def _weather_handler(args: CurrentWeatherArgs, ctx: ToolContext) -> CurrentWeatherResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(
                HKO_BASE, params={"dataType": "rhrread", "lang": _lang_param(ctx.query_lang)}
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"HKO rhrread failed: {err}") from err

    temps = (data.get("temperature") or {}).get("data") or []
    temp_c: float | None = None
    if temps:
        # Prefer Hong Kong Observatory station reading; otherwise first entry.
        for t in temps:
            if t.get("place") in ("Hong Kong Observatory", "香港天文台"):
                temp_c = float(t.get("value"))
                break
        if temp_c is None:
            temp_c = float(temps[0].get("value"))

    humid = (data.get("humidity") or {}).get("data") or []
    rh = int(humid[0]["value"]) if humid else None

    rainfall_vals = (data.get("rainfall") or {}).get("data") or []
    rain = None
    if rainfall_vals:
        rain = max(
            (
                float(r.get("max", r.get("value", 0.0)))
                for r in rainfall_vals
                if r.get("max") is not None or r.get("value") is not None
            ),
            default=None,
        )

    uv = None
    uv_block = data.get("uvindex") or {}
    uv_list = uv_block.get("data") if isinstance(uv_block, dict) else None
    if isinstance(uv_list, list) and uv_list:
        uv = float(uv_list[0].get("value"))

    return CurrentWeatherResult(
        update_time=data.get("updateTime"),
        temperature_c=temp_c,
        humidity_pct=rh,
        rainfall_mm_past_hour=rain,
        uv_index=uv,
        general_summary=None,
    )


CURRENT_WEATHER_TOOL: ToolSpec[CurrentWeatherArgs, CurrentWeatherResult] = ToolSpec(
    name="context.get_current_weather",
    description_en=(
        "Current weather in Hong Kong from HKO: temperature, humidity, past-hour "
        "rainfall, UV index. No arguments. Call this whenever the user mentions "
        "weather, going outside, or outdoor activities."
    ),
    args_schema=CurrentWeatherArgs,
    result_schema=CurrentWeatherResult,
    handler=_weather_handler,
    ttl_seconds=300,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.weather.gov.hk/rhrread",
)


# --- warnings --------------------------------------------------------------


class ActiveWarningsArgs(BaseModel):
    pass


class Warning(BaseModel):
    code: str
    name: str
    action_code: str | None = None
    issue_time: str | None = None


class ActiveWarningsResult(BaseModel):
    has_active: bool
    warnings: list[Warning]
    source: str = "hko.warnsum"


async def _warnings_handler(args: ActiveWarningsArgs, ctx: ToolContext) -> ActiveWarningsResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(
                HKO_BASE, params={"dataType": "warnsum", "lang": _lang_param(ctx.query_lang)}
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"HKO warnsum failed: {err}") from err

    warnings: list[Warning] = []
    for code, payload in data.items():
        if not isinstance(payload, dict):
            continue
        warnings.append(
            Warning(
                code=str(code),
                name=str(payload.get("name", code)),
                action_code=payload.get("actionCode"),
                issue_time=payload.get("issueTime") or payload.get("updateTime"),
            )
        )
    return ActiveWarningsResult(has_active=bool(warnings), warnings=warnings)


ACTIVE_WARNINGS_TOOL: ToolSpec[ActiveWarningsArgs, ActiveWarningsResult] = ToolSpec(
    name="context.get_active_warnings",
    description_en=(
        "Active Hong Kong Observatory warnings (typhoon signal, rainstorm, "
        "thunderstorm, landslip, hot weather, cold weather, fire danger). No args. "
        "Call whenever the user asks about going outside, travel plans, or weather "
        "hazards — results should be surfaced in the reply when has_active=true."
    ),
    args_schema=ActiveWarningsArgs,
    result_schema=ActiveWarningsResult,
    handler=_warnings_handler,
    ttl_seconds=60,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.weather.gov.hk/warnsum",
)


# --- 9-day forecast (HKO fnd) ---------------------------------------------


class NineDayForecastArgs(BaseModel):
    pass


class ForecastDay(BaseModel):
    forecast_date: str  # YYYYMMDD as returned by HKO
    week: str
    forecast_maxtemp_c: float | None = None
    forecast_mintemp_c: float | None = None
    forecast_maxrh_pct: int | None = None
    forecast_minrh_pct: int | None = None
    forecast_weather: str
    forecast_wind: str | None = None
    psr: str | None = None  # probability of significant rainfall


class NineDayForecastResult(BaseModel):
    general_situation: str
    days: list[ForecastDay]
    update_time: str | None = None
    source: str = "hko.fnd"


async def _nine_day_handler(args: NineDayForecastArgs, ctx: ToolContext) -> NineDayForecastResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(
                HKO_BASE, params={"dataType": "fnd", "lang": _lang_param(ctx.query_lang)}
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"HKO fnd failed: {err}") from err

    days: list[ForecastDay] = []
    for rec in data.get("weatherForecast") or []:
        max_t = (rec.get("forecastMaxtemp") or {}).get("value")
        min_t = (rec.get("forecastMintemp") or {}).get("value")
        max_rh = (rec.get("forecastMaxrh") or {}).get("value")
        min_rh = (rec.get("forecastMinrh") or {}).get("value")
        days.append(
            ForecastDay(
                forecast_date=str(rec.get("forecastDate", "")),
                week=str(rec.get("week", "")),
                forecast_maxtemp_c=float(max_t) if max_t is not None else None,
                forecast_mintemp_c=float(min_t) if min_t is not None else None,
                forecast_maxrh_pct=int(max_rh) if max_rh is not None else None,
                forecast_minrh_pct=int(min_rh) if min_rh is not None else None,
                forecast_weather=str(rec.get("forecastWeather", "")),
                forecast_wind=rec.get("forecastWind"),
                psr=rec.get("PSR"),
            )
        )

    return NineDayForecastResult(
        general_situation=str(data.get("generalSituation", "")),
        days=days,
        update_time=data.get("updateTime"),
    )


NINE_DAY_FORECAST_TOOL: ToolSpec[NineDayForecastArgs, NineDayForecastResult] = ToolSpec(
    name="context.get_9day_forecast",
    description_en=(
        "9-day weather outlook from HKO — daily max/min temperature, humidity "
        "range, wind summary, probability of significant rainfall, and the "
        "general regional situation. Use when the user asks about tomorrow / "
        "this week / next few days / weekend weather. For 'right now' use "
        "context.get_current_weather instead."
    ),
    args_schema=NineDayForecastArgs,
    result_schema=NineDayForecastResult,
    handler=_nine_day_handler,
    ttl_seconds=60 * 60,  # refreshed ~5 times/day upstream
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant", "zh-Hans"}),
    upstream="data.weather.gov.hk/fnd",
)


# --- AQHI ------------------------------------------------------------------


class AQHIArgs(BaseModel):
    near: str | None = Field(
        default=None,
        description="Optional HK district or station name to prefer. If omitted, "
        "returns the summary reading.",
    )


class AQHIStation(BaseModel):
    station: str
    aqhi: str | int | None = None
    health_risk: str | None = None
    update_time: str | None = None


class AQHIResult(BaseModel):
    stations: list[AQHIStation]
    source: str = "epd.aqhi"


_AQHI_DESC_RE = re.compile(
    r"^\s*(?P<station>[^-]+?)\s*-\s*"
    r"(?:General|Roadside)\s+Stations?:\s*(?P<aqhi>[\d/+\-]+)\s+"
    r"(?P<risk>Low|Moderate|High|Very\s+High|Serious|Health\s+Risk)\s*-\s*"
    r"(?P<time>.+?)\s*$"
)


def _parse_aqhi_rss(xml_text: str) -> list[AQHIStation]:
    """Extract station rows from the EPD per-station RSS feed.

    Each <item> has shape:
        <title>Central/Western</title>
        <description><![CDATA[Central/Western - General Stations: 3 Low - <ts>]]></description>

    We use `xml.etree` (stdlib) to avoid an lxml dependency.
    """
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 — trusted EPD gov.hk endpoint
    except ET.ParseError:
        return []
    stations: list[AQHIStation] = []
    for item in root.iter("item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        name = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
        desc = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""
        if not name or not desc:
            continue
        match = _AQHI_DESC_RE.match(desc)
        if match:
            aqhi_raw = match.group("aqhi").strip()
            aqhi: int | str | None
            try:
                aqhi = int(aqhi_raw)
            except ValueError:
                aqhi = aqhi_raw or None
            stations.append(
                AQHIStation(
                    station=name,
                    aqhi=aqhi,
                    health_risk=match.group("risk").strip(),
                    update_time=match.group("time").strip(),
                )
            )
        else:
            # Fallback: keep the station name even if the description doesn't parse.
            stations.append(
                AQHIStation(station=name, aqhi=None, health_risk=None, update_time=None)
            )
    return stations


async def _aqhi_handler(args: AQHIArgs, ctx: ToolContext) -> AQHIResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(AQHI_STATIONS_URL)
            r.raise_for_status()
            xml_text = r.text
    except httpx.HTTPError as err:
        raise ToolUpstreamError(f"EPD AQHI failed: {err}") from err

    stations = _parse_aqhi_rss(xml_text)

    if args.near and stations:
        needle = args.near.lower()
        stations.sort(key=lambda s: 0 if needle in s.station.lower() else 1)

    return AQHIResult(stations=stations[:5] if stations else [])


AQHI_TOOL: ToolSpec[AQHIArgs, AQHIResult] = ToolSpec(
    name="context.get_aqhi",
    description_en=(
        "Air Quality Health Index (AQHI) from the Hong Kong EPD. Returns current "
        "band per monitoring station. Use when the user asks about air quality, "
        "going outside, outdoor exercise, or sensitive groups (asthma, elderly)."
    ),
    args_schema=AQHIArgs,
    result_schema=AQHIResult,
    handler=_aqhi_handler,
    ttl_seconds=300,
    budget_ms=1500,
    upstream_langs=frozenset({"en", "zh-Hant"}),
    upstream="aqhi.gov.hk",
)

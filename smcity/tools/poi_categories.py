# ruff: noqa: RUF003  # CJK glyphs / typographic marks in data + prose are intentional.
"""Canonical POI-category registry — the single source of truth.

Everything about a POI category lives in ONE `CategorySpec` per slug:

- `tags`  — the Overpass tag filters used to build the OSM query
  (`smcity/tools/osm_pois.py::_build_query`).
- `hint`  — the bilingual display string the LLM reads in the `find_poi`
  `category` field schema (`category_field_description`).
- `en` / `raw` / `zh` — the matching lexicon the deterministic router uses
  to map free user text → slug (`categorize`), which the chain-rules engine
  calls to auto-complete the `address_lookup` → `find_poi` chain without a
  second (expensive) LLM hop.

Before this module those four facts lived in four parallel tables across two
files (`_CATEGORIES`, `_CATEGORY_HINTS`, `PoiCategory`, and chain_rules'
`_POI_CATEGORY_PATTERNS`). They drifted: the LLM's notion of "what words mean
`beauty_shop`" and the router's notion were maintained by hand, separately,
and disagreed. Unifying them means the model's view and the router's view
cannot diverge, and adding a category is a one-place edit.

Two matcher mechanisms (not per-keyword hacks) make the lexicon robust:

1. Script normalisation — user text is run through `simplified_to_hk` before
   matching, so a single HK-Traditional term covers Simplified input too
   (百货 ⇒ 百貨). Chinese terms are therefore written in Traditional ONLY.
2. Plural tolerance — every English noun is compiled with an optional
   trailing plural (`shop` ⇒ `shops`), killing the `\b`-before-plural-`s`
   quirk that previously dropped "clothes shops", "public toilets",
   "kiosks", "bookstores", "marketplaces". Each slug also matches its own
   literal form, since synth corpora occasionally leak the slug verbatim
   (e.g. "variety_store").

Deterministic-router-first (rather than always re-prompting the LLM to pick
the category) is a deliberate choice for this deployment: LM Studio sustains
~0.08 q/s, so an extra LLM hop costs ~12s. The matcher is instant and
testable; the LLM hint remains the fallback when no term matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from re import Pattern

from smcity.langrouter.normalize import simplified_to_hk


@dataclass(slots=True, frozen=True)
class CategorySpec:
    """All facts about one POI category, in one place.

    `tags` drives the Overpass query; `hint` is the LLM-facing display string;
    `en`/`raw`/`zh` are the matching lexicon for the deterministic router.
    """

    tags: tuple[tuple[str, str | None], ...]  # (key, value or None for presence check)
    hint: str  # bilingual display string shown to the LLM
    en: tuple[str, ...] = ()  # English nouns — compiled with plural tolerance + alnum boundaries
    raw: tuple[str, ...] = ()  # raw regex fragments (brands with digits/optionals)
    zh: tuple[str, ...] = ()  # Chinese terms, HK-Traditional (Simplified handled by normalisation)


# THE registry. Order matters twice over and the two orders happen to agree:
#   - display order of the LLM hint list (cosmetic), and
#   - router first-match-wins priority (functional).
# Chinese terms are kept specific enough that no category's term is a substring
# of another's — e.g. bare 市場 is deliberately NOT in `marketplace`, so it
# cannot swallow 超級市場. The cross-contamination test locks this invariant.
CATEGORIES: dict[str, CategorySpec] = {
    # --- shops (workbook S514–S530) ---------------------------------------
    "convenience_store": CategorySpec(
        tags=(("shop", "convenience"),),
        hint="便利店 / 7-Eleven / Circle K / VanGO",
        en=("convenience store", "vango"),
        raw=(r"7-?eleven", r"7-?11", r"circle\s*k"),
        zh=("便利店", "7仔"),
    ),
    "supermarket": CategorySpec(
        tags=(("shop", "supermarket"),),
        hint="超市 / 超級市場 (Wellcome, Park'n Shop, AEON)",
        en=("supermarket", "wellcome", "aeon", "fusion"),
        raw=(r"park\s*n\s*shop",),
        zh=("超市", "超級市場"),
    ),
    "hardware_store": CategorySpec(
        tags=(("shop", "hardware"),),
        hint="五金舖 / 五金店",
        en=("hardware store", "hardware shop", "hardware supplier", "tool shop"),
        zh=("五金舖", "五金店", "五金", "硬件", "工具店", "水電零件", "水電材料"),
    ),
    "hairdresser": CategorySpec(
        tags=(("shop", "hairdresser"),),
        hint="髮型屋 / 理髮店 / 髮廊",
        en=("hairdresser", "barber", "hair salon", "barbershop", "hair shop", "haircut"),
        zh=("髮型屋", "理髮店", "髮廊", "美髮", "頭髮", "剪髮"),
    ),
    "clothes_shop": CategorySpec(
        tags=(("shop", "clothes"),),
        hint="服裝店 / 衫舖",
        en=("clothes shop", "clothes store", "clothing shop", "clothing store", "apparel"),
        zh=("服裝", "服飾", "衫舖"),
    ),
    "electronics_shop": CategorySpec(
        tags=(("shop", "electronics"),),
        hint="電器店 / 電子產品店",
        en=("electronics shop", "electronics store", "gadget shop"),
        zh=("電器店", "電子產品", "電子店", "電子商店", "電子舖"),
    ),
    "department_store": CategorySpec(
        tags=(("shop", "department_store"),),
        hint="百貨公司 (SOGO, Yata, Lane Crawford)",
        en=("department store", "sogo", "yata", "lane crawford"),
        zh=("百貨公司", "百貨"),
    ),
    "variety_store": CategorySpec(
        tags=(("shop", "variety_store"),),
        hint="雜貨店 / 日本城 / 多多",
        en=("variety store", "dollar store", "japan home", "don don"),
        zh=("日本城", "多多", "雜貨", "生活用品", "綜合商店"),
    ),
    "houseware_shop": CategorySpec(
        tags=(("shop", "houseware"),),
        hint="家品店 / 家居用品店",
        en=("houseware", "household goods", "home goods"),
        zh=("家品店", "家居用品", "家用品", "家居店", "家用嘢"),
    ),
    "beauty_shop": CategorySpec(
        tags=(("shop", "beauty"),),
        hint="美妝店 / 化妝品店 (SaSa, Bonjour)",
        en=("beauty shop", "beauty store", "cosmetics shop", "cosmetics store", "sasa", "bonjour"),
        zh=("美妝店", "化妝品店", "化妝品", "美容"),
    ),
    "optician": CategorySpec(
        tags=(("shop", "optician"),),
        hint="眼鏡舖",
        en=("optician", "eyewear", "glasses shop"),
        zh=("眼鏡舖", "眼鏡店", "眼鏡", "配鏡", "驗眼"),
    ),
    "shoe_shop": CategorySpec(
        tags=(("shop", "shoes"),),
        hint="鞋舖 / 鞋店",
        en=("shoe shop", "shoe store", "shoemaker", "cobbler"),
        zh=("鞋舖", "鞋店"),
    ),
    "greengrocer": CategorySpec(
        tags=(("shop", "greengrocer"),),
        hint="生果舖 / 蔬果店",
        en=("greengrocer", "fruit shop", "fruit store", "vegetable shop", "vegetable store"),
        zh=("生果舖", "生果店", "菜舖", "蔬果", "蔬菜", "水果", "生果", "生鮮"),
    ),
    "bookstore": CategorySpec(
        tags=(("shop", "books"),),
        hint="書店 / 書局",
        en=("bookstore", "book shop", "bookshop"),
        zh=("書店", "書局"),
    ),
    "laundry": CategorySpec(
        tags=(("shop", "laundry"),),
        hint="洗衣店 / 乾洗店",
        en=("laundry", "laundromat", "laundrette", "dry cleaner", "dry cleaning"),
        zh=("洗衣店", "乾洗店", "洗衣", "洗衫"),
    ),
    "kiosk": CategorySpec(
        tags=(("shop", "kiosk"),),
        hint="報攤 / 小賣亭",
        en=("kiosk", "news stand", "newsagent"),
        zh=("報攤", "小賣亭", "信息亭", "資訊亭", "售賣亭"),
    ),
    "bookmaker": CategorySpec(
        tags=(("shop", "bookmaker"),),
        hint="馬會投注站 (off-course Jockey Club)",
        en=("bookmaker", "jockey club", "betting shop"),
        raw=(r"off-course",),
        zh=("馬會", "投注站", "投注", "博彩"),
    ),
    # --- amenities (workbook S531–S540) -----------------------------------
    "public_toilet": CategorySpec(
        tags=(("amenity", "toilets"),),
        hint="公廁 / 公共廁所 / 洗手間",
        en=(
            "public toilet",
            "public restroom",
            "public washroom",
            "toilet",
            "restroom",
            "washroom",
            "loo",
        ),
        zh=("公廁", "廁所", "洗手間"),
    ),
    "place_of_worship": CategorySpec(
        tags=(("amenity", "place_of_worship"),),
        hint="廟宇 / 教堂 / 寺廟 / 清真寺",
        en=("temple", "church", "mosque", "shrine", "place of worship"),
        zh=("廟宇", "寺廟", "教堂", "清真寺", "佛寺", "寺院", "信眾"),
    ),
    "recycling_location": CategorySpec(
        tags=(("amenity", "recycling"),),
        hint="回收站 / 回收箱 / 回收點",
        en=("recycling", "recycle bin", "recycling point"),
        zh=("回收", "垃圾分類"),
    ),
    "veterinarian": CategorySpec(
        tags=(("amenity", "veterinary"),),
        hint="獸醫 / 動物診所",
        en=("vet", "veterinarian", "animal clinic"),
        zh=("獸醫", "動物診所", "動物醫生", "寵物醫生"),
    ),
    "marketplace": CategorySpec(
        tags=(("amenity", "marketplace"),),
        hint="街市 / 菜市場",
        en=("wet market", "marketplace", "public market"),
        # 市場 with a lookbehind so 超級市場 stays the supermarket's — bare
        # 市場 would make categorize_all() see both and defer every turn.
        raw=(r"(?<![級级])市場", r"(?<![級级])市场"),
        zh=("街市", "菜市場", "公開市場"),
    ),
    "drinking_water": CategorySpec(
        tags=(("amenity", "drinking_water"),),
        hint="飲水機 / 公眾飲水器",
        en=("drinking water", "water fountain", "water dispenser"),
        zh=("飲水", "飲用水"),
    ),
    "government_office": CategorySpec(
        tags=(("office", "government"),),
        hint="政府辦事處 / 民政事務處",
        en=(
            "government office",
            "district office",
            "home affairs",
            "administrative building",
            "government building",
        ),
        zh=("政府辦事處", "民政事務處", "政府機構", "政府部門", "行政大樓"),
    ),
    "dentist": CategorySpec(
        tags=(("amenity", "dentist"), ("healthcare", "dentist")),
        hint="牙醫 / 牙科診所",
        en=("dentist", "dental"),
        zh=("牙醫", "牙科"),
    ),
    # --- infrastructure / road facilities (workbook S541–S549) ------------
    "mtr_station_entrance": CategorySpec(
        tags=(("railway", "subway_entrance"),),
        hint="港鐵 / 地鐵出入口",
        raw=(
            r"\bmtr\s+(?:entrances?|exits?|stations?)\b",
            r"(?:港鐵|地鐵|mtr).{0,8}?(?:出入口|出口|入口|口)",
        ),
    ),
    "public_elevator": CategorySpec(
        tags=(("highway", "elevator"),),
        hint="公共升降機 / 街道電梯",
        en=("public lift", "public elevator", "street elevator", "footbridge lift"),
        zh=("升降機", "電梯"),
    ),
    "bench": CategorySpec(
        tags=(("amenity", "bench"), ("bench", "yes")),
        hint="公眾長凳 / 休憩座椅",
        en=("bench", "public seat"),
        zh=("長凳", "長椅", "坐椅", "座椅"),
    ),
    "shelter": CategorySpec(
        tags=(("amenity", "shelter"), ("shelter", "yes")),
        hint="公眾遮蔭處 / 涼亭 / 巴士站候車亭",
        en=("shelter", "rain shelter", "bus shelter", "public shelter", "awning"),
        zh=("涼亭", "遮蔭處", "避難", "庇護", "候車亭"),
    ),
    "handrail": CategorySpec(
        tags=(("handrail", "yes"),),
        hint="扶手 / 公眾欄杆",
        en=("handrail", "railing", "grab rail"),
        zh=("扶手", "欄杆"),
    ),
}


# Order-stable tuple of slugs — the source for the `PoiCategory` Literal and
# the `POI_CATEGORIES` set in osm_pois.py.
POI_CATEGORY_SLUGS: tuple[str, ...] = tuple(CATEGORIES.keys())


# --- LLM-facing schema description ----------------------------------------


def category_field_description() -> str:
    """Bilingual index of every slug → meaning for the `category` field.

    Built from `CATEGORIES` so the table is the single source of truth. The
    string is kept byte-stable (locked by a golden test) — the LLM's routing
    cues must not change silently underneath a quality measurement.
    """
    parts = [f"{slug} ({spec.hint})" for slug, spec in CATEGORIES.items()]
    return "Category of POI to search for. Pick ONE slug from: " + "; ".join(parts) + "."


# --- deterministic text → slug router -------------------------------------


def _compile(slug: str, spec: CategorySpec) -> Pattern[str]:
    """Compile one category's lexicon into a single alternation.

    English nouns get word boundaries + optional-plural tolerance; the slug's
    own literal forms are added; raw fragments pass through verbatim; Chinese
    terms match as bare substrings (CJK has no word boundary).
    """
    frags: list[str] = []
    # English nouns + the slug's literal forms, all plural-tolerant.
    #
    # Boundaries use ASCII-alnum lookarounds, NOT `\b`: `\b` treats CJK as a
    # word char, so it fails to fire between a Chinese char and a jammed-on
    # Latin token ("搵個shelter"), which is exactly how Cantonese text mixes
    # English. Inner whitespace is rebuilt from per-token escapes so a literal
    # space can't be mangled by `re.escape` (which escapes spaces).
    literals = [*spec.en, slug, slug.replace("_", " ")]
    seen: set[str] = set()
    for noun in literals:
        if noun in seen:
            continue
        seen.add(noun)
        core = r"\s+".join(re.escape(tok) for tok in noun.split())
        frags.append(rf"(?<![a-zA-Z0-9]){core}(?:e?s)?(?![a-zA-Z0-9])")
    frags.extend(spec.raw)
    frags.extend(re.escape(z) for z in spec.zh)
    return re.compile("|".join(frags), re.IGNORECASE)


@cache
def _patterns() -> dict[str, Pattern[str]]:
    return {slug: _compile(slug, spec) for slug, spec in CATEGORIES.items()}


def categorize_all(text: str) -> tuple[str, ...]:
    """All slugs whose lexicon matches the text, in registry order.

    Callers that need binary confidence (the classifier fast path) require
    exactly one match — two or more means the turn is ambiguous and must
    defer to the LLM. `categorize()` keeps the historic first-match-wins
    contract for the chain-rules engine.
    """
    if not text:
        return ()
    haystack = text + "\n" + simplified_to_hk(text)
    return tuple(slug for slug, pattern in _patterns().items() if pattern.search(haystack))


def categorize(text: str) -> str | None:
    """Map free user text to a POI slug, or None if no category term matches.

    Matches against the raw text AND its Simplified→HK-Traditional form, so a
    single Traditional lexicon covers both scripts. First match in registry
    order wins (the registry is ordered most-specific-first).
    """
    slugs = categorize_all(text)
    return slugs[0] if slugs else None


def strip_category_terms(text: str, slug: str) -> str:
    """Blank out the slug's matched lexicon terms (raw + Simplified-normalised).

    Used by the classifier's location extractor: removing the category words
    first means the remaining text is mostly the location phrase, so CJK
    extraction (which has no word boundaries) can't swallow the category term.
    """
    pattern = _patterns()[slug]
    return pattern.sub(" ", text + "\n" + simplified_to_hk(text))


__all__ = [
    "CATEGORIES",
    "POI_CATEGORY_SLUGS",
    "CategorySpec",
    "categorize",
    "categorize_all",
    "category_field_description",
    "strip_category_terms",
]

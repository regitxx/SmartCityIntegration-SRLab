# CJK glyphs in test data + prose are intentional.
"""Tests for the canonical POI-category registry and its text→slug router.

`poi_categories.py` is the single source of truth shared by the LLM-facing
`find_poi` schema and the deterministic chain-rules router. These tests lock:

- the byte-stable LLM schema string (a quality measurement must not shift
  underneath a silent prompt change);
- registry ↔ enum ↔ public-set consistency;
- the two matcher mechanisms (Simplified↔Traditional normalisation, English
  plural + CJK-jammed-Latin boundary handling);
- a no-cross-contamination invariant that keeps first-match-wins safe as
  categories are added;
- the real v0.7.0 misroute corpus that motivated the routing fix.
"""

from __future__ import annotations

import hashlib

from smcity.tools.osm_pois import POI_CATEGORIES, PoiCategory
from smcity.tools.poi_categories import (
    CATEGORIES,
    POI_CATEGORY_SLUGS,
    categorize,
    category_field_description,
)

# --- single-source invariants --------------------------------------------


def test_registry_has_30_categories() -> None:
    assert len(CATEGORIES) == 30
    assert len(POI_CATEGORY_SLUGS) == 30


def test_registry_enum_and_public_set_agree() -> None:
    """The CATEGORIES keys, the PoiCategory Literal, and POI_CATEGORIES (the
    set exported for fuzz contracts) must all describe the same 30 slugs."""
    literal_values = frozenset(PoiCategory.__args__)  # type: ignore[attr-defined]
    assert frozenset(CATEGORIES) == literal_values
    assert frozenset(CATEGORIES) == POI_CATEGORIES


def test_llm_schema_description_is_byte_stable() -> None:
    """The `category` field description the LLM reads is locked to a known
    hash. If a category's hint legitimately changes, update this snapshot in
    the SAME commit — never let routing cues drift silently between releases.
    """
    desc = category_field_description()
    digest = hashlib.sha256(desc.encode()).hexdigest()
    assert len(desc) == 985
    assert digest == "e2d92a93a3ccee5f122652887c35471d78868a388bb18ae4445142817c9b07b4"


def test_every_category_has_a_lexicon() -> None:
    """A category with no English/raw/Chinese terms can only be reached if the
    LLM emits the slug verbatim — the deterministic router would be blind to
    it. Every slug must carry at least one human-phrasing term."""
    for slug, spec in CATEGORIES.items():
        assert spec.en or spec.raw or spec.zh, slug


# --- no cross-contamination (locks first-match-wins ordering) -------------


def test_each_lexicon_term_self_routes() -> None:
    """Every English and Chinese term must categorise to its OWN slug.

    This is the structural guard against ordering bugs: if term X of category
    A also matches category B and B is earlier in the registry, this fails —
    forcing the term (or the registry order) to be fixed rather than silently
    mis-routing in production.
    """
    for slug, spec in CATEGORIES.items():
        for term in (*spec.en, *spec.zh):
            got = categorize(term)
            assert got == slug, f"{term!r} routed to {got!r}, expected {slug!r}"


# --- matcher mechanisms ---------------------------------------------------


def test_english_plurals_match() -> None:
    """The historical `\\b`-before-plural-`s` quirk must stay dead."""
    cases = {
        "clothes shops": "clothes_shop",
        "public toilets": "public_toilet",
        "all kiosks": "kiosk",
        "marketplaces nearby": "marketplace",
        "list bookstores": "bookstore",
    }
    for text, slug in cases.items():
        assert categorize(text) == slug, text


def test_latin_token_jammed_against_cjk_matches() -> None:
    """Cantonese mixes English with no spaces — `\\b` fails there, alnum
    lookarounds don't."""
    assert categorize("搵下啲bookmaker點樣") == "bookmaker"
    assert categorize("可唔可以搵到邊個shelter") == "shelter"


def test_simplified_input_matches_via_normalisation() -> None:
    """A single HK-Traditional lexicon must cover Simplified input."""
    assert categorize("哪里有牙医") == "dentist"
    assert categorize("哪些百货商场") == "department_store"
    assert categorize("绿色蔬菜店") == "greengrocer"


def test_hk_synonyms_route() -> None:
    """The synonym gaps that produced v0.7.0 misroutes."""
    cases = {
        "專業美容店": "beauty_shop",
        "美髮店": "hairdresser",
        "賣水果的地方": "greengrocer",
        "佛寺": "place_of_worship",
        "供信眾禮拜的地方": "place_of_worship",
        "安裝咗欄杆": "handrail",
        "自助洗衣房": "laundry",
        "硬件供應商": "hardware_store",
    }
    for text, slug in cases.items():
        assert categorize(text) == slug, text


def test_slug_literal_leak_matches() -> None:
    """Synth corpora sometimes emit the slug verbatim."""
    assert categorize("查詢到相關的 variety_store 點位") == "variety_store"


def test_non_poi_and_unknown_category_return_none() -> None:
    assert categorize("how's the weather today") is None
    assert categorize("") is None
    # Vending machine is intentionally NOT one of the 30 categories; routing it
    # to `kiosk` would be wrong, so None (→ LLM decides) is correct.
    assert categorize("附近有冇自助售賣機") is None

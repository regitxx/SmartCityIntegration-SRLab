"""Language router — detect + normalise + dataset coverage matrix.

See docs/research/04_multilingual_language_stack.md for the full design.
Phase 1a uses particle-heuristic + unicode-script detection (no fastText /
transformer yet — those land in Phase 2).
"""

from smcity.langrouter.coverage import (
    DATASET_COVERAGE,
    choose_query_lang,
    is_natively_supported,
)
from smcity.langrouter.detect import LangDetection, detect
from smcity.langrouter.normalize import hk_to_simplified, simplified_to_hk

__all__ = [
    "DATASET_COVERAGE",
    "LangDetection",
    "choose_query_lang",
    "detect",
    "hk_to_simplified",
    "is_natively_supported",
    "simplified_to_hk",
]

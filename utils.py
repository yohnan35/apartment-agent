"""
Shared utilities: logging, Hebrew text normalisation.
"""
import logging
import re
import unicodedata
from typing import Optional


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the given module name."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger


# Hebrew final-letter mapping: final form → regular form
_HEBREW_FINALS_MAP = str.maketrans("ךםןףץ", "כמנפצ")


def normalize_hebrew_finals(s: Optional[str]) -> Optional[str]:
    """Convert Hebrew final letters (ךםןףץ) to their non-final forms (כמנפצ).

    Used for fuzzy city-name matching so that e.g. "ירושלים" and "ירושלמ"
    compare equal after normalisation.
    """
    if s is None:
        return None
    return s.translate(_HEBREW_FINALS_MAP)


# Regex matching invisible/control Unicode characters (zero-width joiners,
# directional marks, soft hyphens, etc.) that appear in scraped Hebrew text.
_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\u00ad\ufeff\u061c]"
)


def normalize_text(s: Optional[str]) -> Optional[str]:
    """Strip invisible Unicode characters and normalise whitespace.

    Returns None when the input is None or becomes empty after cleaning.
    """
    if s is None:
        return None
    # NFC normalisation first, then strip invisible chars
    s = unicodedata.normalize("NFC", s)
    s = _INVISIBLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None

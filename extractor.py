"""
Extracts structured apartment fields from Hebrew listing text using claude-haiku.
"""
import json
import os
import re
from typing import Any

import anthropic

import utils

_log = utils.get_logger(__name__)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """אתה מחלץ מידע מובנה ממודעות דירות בישראל.
כל המודעות הן מפייסבוק מרקטפלייס ישראל — כולן בישראל.
תחזיר אך ורק JSON תקין ללא הסבר.
השדות:
- rooms: מספר חדרים (float, null אם לא ידוע)
- city: שם עיר בעברית (string, null) — לדוגמה: "תל אביב", "ירושלים", "חיפה"
- neighborhood: שכונה בעברית (string, null)
- floor: קומה (int, null) — קרקע = 0
- total_floors: סה"כ קומות בבניין (int, null)
- broker: האם תיווך? (true=תיווך, false=ישיר, null=לא ברור)
- size_sqm: שטח במ"ר (float, null)
- property_type: סוג הנכס (string כמו "דירה","קוטג'","דופלקס","פנטהאוז","סטודיו", null)
- for_rent: להשכרה=true, למכירה=false (null אם לא ברור)
- entry_date: תאריך כניסה / זמינות (string, null) — לדוגמה: "מיידי", "01/06/2025", "אפריל 2025", "גמיש". אם לא מוזכר — null.
- phone: מספר טלפון של המפרסם (string, null) — לדוגמה: "050-1234567", "0521234567". אם לא מוזכר — null.
"""


def _parse_one(raw: str) -> dict[str, Any]:
    """Parse a single JSON blob returned by the model, tolerating markdown fences and preamble."""
    raw = raw.strip()
    # Extract from fenced block anywhere in the response (handles preamble text)
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if m:
        raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("_parse_one JSON decode error: %s — raw[:120]: %s", exc, raw[:120])
        return {}


def extract_one(listing: dict) -> dict[str, Any]:
    """Extract fields for a single listing."""
    results = bulk_extract([listing])
    return results[0] if results else {}


def bulk_extract(listings: list[dict]) -> list[dict[str, Any]]:
    """
    Extract structured fields for a batch of listings in one API call.
    Returns a list aligned with the input (same length, same order).
    Empty dicts for any listing that fails to parse.
    """
    if not listings:
        return []

    client = _get_client()

    # Build a numbered prompt so the model returns a JSON array
    # Normalize Hebrew text before sending to AI to strip invisible characters
    items_text = "\n\n".join(
        f"[{i}] כותרת: {utils.normalize_text(l.get('title')) or ''}\n"
        f"מחיר: {utils.normalize_text(l.get('price_text')) or ''}\n"
        f"מיקום: {utils.normalize_text(l.get('location')) or ''}\n"
        f"תיאור: {utils.normalize_text(l.get('description')) or ''}"
        for i, l in enumerate(listings)
    )

    user_msg = (
        f"יש {len(listings)} מודעות. החזר מערך JSON עם {len(listings)} אובייקטים "
        f"לפי הסדר:\n\n{items_text}"
    )

    # Wrap API call — a network error, auth error or rate-limit must not crash the scrape
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
    except Exception as exc:
        _log.error("bulk_extract API call failed: %s — returning empty extractions", exc, exc_info=True)
        return [{} for _ in listings]

    # Use regex-based fence stripping (handles ```json, ```JSON, extra blank lines, etc.)
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            # Pad or truncate to match input length
            result = (parsed + [{}] * len(listings))[: len(listings)]
        else:
            # Wrapped in a key? (e.g. {"listings": [...]})
            result = None
            for v in parsed.values():
                if isinstance(v, list):
                    result = (v + [{}] * len(listings))[: len(listings)]
                    break
            if result is None:
                raise json.JSONDecodeError("no list found in wrapped response", raw, 0)

        # Apply query hint in ALL paths — overrides whatever the AI said
        # This is the sale-only hard-lock at the extraction layer.
        for i, (r, l) in enumerate(zip(result, listings)):
            hint = l.get("_query_hint_for_rent")
            if hint is not None:
                result[i] = {**r, "for_rent": hint}
        return result

    except json.JSONDecodeError as exc:
        _log.warning("bulk_extract JSON parse failed (%s) — falling back to per-listing calls", exc)

    # Fallback: call one-by-one (slower but more reliable)
    # Apply hint here too so sale-only lock is never bypassed
    results = [_extract_single_fallback(client, l) for l in listings]
    for i, (r, l) in enumerate(zip(results, listings)):
        hint = l.get("_query_hint_for_rent")
        if hint is not None:
            results[i] = {**r, "for_rent": hint}
    return results


def _extract_single_fallback(client: anthropic.Anthropic, listing: dict) -> dict[str, Any]:
    text = (
        f"כותרת: {utils.normalize_text(listing.get('title')) or ''}\n"
        f"מחיר: {utils.normalize_text(listing.get('price_text')) or ''}\n"
        f"מיקום: {utils.normalize_text(listing.get('location')) or ''}\n"
        f"תיאור: {utils.normalize_text(listing.get('description')) or ''}"
    )
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return _parse_one(resp.content[0].text)

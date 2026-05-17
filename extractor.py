"""
Extracts structured apartment fields from Hebrew listing text using claude-haiku.
"""
import json
import os
import re
from typing import Any

import anthropic

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
- floor: קומה (int, null)
- total_floors: סה"כ קומות בבניין (int, null)
- broker: האם תיווך? (true=תיווך, false=ישיר, null=לא ברור)
- size_sqm: שטח במ"ר (float, null)
- property_type: סוג הנכס (string כמו "דירה","קוטג'","דופלקס","פנטהאוז","סטודיו", null)
- for_rent: להשכרה=true, למכירה=false (null אם לא ברור)
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
    except json.JSONDecodeError:
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
    items_text = "\n\n".join(
        f"[{i}] כותרת: {l.get('title','')}\n"
        f"מחיר: {l.get('price_text','')}\n"
        f"מיקום: {l.get('location','')}\n"
        f"תיאור: {l.get('description','')}"
        for i, l in enumerate(listings)
    )

    user_msg = (
        f"יש {len(listings)} מודעות. החזר מערך JSON עם {len(listings)} אובייקטים "
        f"לפי הסדר:\n\n{items_text}"
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()

    # Try to parse as array
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            # Pad or truncate to match input length
            result = (parsed + [{}] * len(listings))[: len(listings)]
            return result
        # Wrapped in a key?
        for v in parsed.values():
            if isinstance(v, list):
                result = (v + [{}] * len(listings))[: len(listings)]
                return result
    except json.JSONDecodeError:
        pass

    # Fallback: call one-by-one
    return [_extract_single_fallback(client, l) for l in listings]


def _extract_single_fallback(client: anthropic.Anthropic, listing: dict) -> dict[str, Any]:
    text = (
        f"כותרת: {listing.get('title','')}\n"
        f"מחיר: {listing.get('price_text','')}\n"
        f"מיקום: {listing.get('location','')}\n"
        f"תיאור: {listing.get('description','')}"
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return _parse_one(resp.content[0].text)

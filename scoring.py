"""
AI scoring for apartment listings using claude-haiku.
Scores each apartment 1-10 with a Hebrew explanation.
"""
import json
import os
import re
from typing import Any, Optional

import anthropic

import apartments_db as db
import utils

_log = utils.get_logger(__name__)
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# Valid AI smart-tag values — only these may appear in the "tags" array.
_VALID_TAGS: frozenset[str] = frozenset([
    "מתחת למחיר השוק",
    'פוטנציאל לתמ"א/פינוי בינוי',
    "נכס להשקעה",
    "משופצת מהיסוד",
    "זקוקה לשיפוץ",
])

SYSTEM_PROMPT = """אתה סוכן נדל"ן מומחה בישראל. דרג כל דירה מ-1 עד 10.
שקול את הגורמים הבאים:
1. מחיר ביחס לממוצע השוק באותה עיר (משפיע הכי הרבה)
2. ישיר מבעלים (עדיף) מול תיווך
3. שלמות מידע (חדרים, שטח, קומה, תיאור)
4. חשד לתיווך מוסתר (מילים כמו "משרד", "נכסים", "נדל"ן", "מלווה", "דמי תיווך" בתיאור אבל broker=false)
5. איכות התיאור ופרטים רלוונטיים

בנוסף, הוסף עד 2 תגיות חכמות לכל דירה מהרשימה הבאה בלבד (רק אם מתאים, אחרת מערך ריק):
- "מתחת למחיר השוק"      (מחיר נמוך ב-10%+ מממוצע העיר)
- "פוטנציאל לתמ\"א/פינוי בינוי"  (בניין ישן, 4 קומות ומטה, עיר מרכזית)
- "נכס להשקעה"           (תשואה פוטנציאלית גבוהה, מחיר נמוך, ביקוש גבוה)
- "משופצת מהיסוד"        (מילים כמו: שופץ, חדש לגמרי, ריהוט חדש, מטבח חדש)
- "זקוקה לשיפוץ"          (מילים כמו: דרוש שיפוץ, מחיר נמוך לגודל, ישן, במצב תחזוקה)

החזר מערך JSON בלבד, ללא הסבר, ללא markdown.
כל אובייקט: {"score": X.X, "score_reason": "הסבר קצר 1-2 משפטים בעברית", "is_broker_suspect": true/false, "tags": ["תגית1"]}
"""


def _parse_scores(raw: str, n: int) -> list[Optional[dict]]:
    """Parse the model response into a list of score dicts, length n."""
    raw = raw.strip()

    # Strip markdown fences
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if m:
        raw = m.group(1).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            result = (parsed + [None] * n)[:n]
            return result
        # Wrapped in a key?
        for v in parsed.values():
            if isinstance(v, list):
                result = (v + [None] * n)[:n]
                return result
    except (json.JSONDecodeError, AttributeError):
        pass

    return [None] * n


def bulk_score(
    listings: list[dict],
    extracted_list: list[dict],
) -> list[dict[str, Any]]:
    """
    Score a batch of listings in one API call.

    Args:
        listings: Raw listing dicts (from scraper) — contain title, price, price_text, location, description.
        extracted_list: Extracted field dicts (from extractor) — contain city, rooms, broker, size_sqm, etc.

    Returns:
        List of dicts aligned with inputs, each with keys:
            score (float|None), score_reason (str|None), is_broker_suspect (bool|None)
        On failure, all values are None.
    """
    if not listings:
        return []

    # Fetch market context
    try:
        city_stats = db.get_city_stats()
        market_ctx = {
            row["city"]: {
                "avg_price": row["avg_price"],
                "avg_price_sqm": row["avg_price_sqm"],
                "count": row["count"],
            }
            for row in city_stats
            if row.get("city")
        }
    except Exception:
        market_ctx = {}

    # Build market summary string
    if market_ctx:
        market_lines = [
            f"  {city}: ממוצע ₪{info['avg_price']:,}" +
            (f", ₪{info['avg_price_sqm']:,}/מ\"ר" if info.get("avg_price_sqm") else "")
            for city, info in list(market_ctx.items())[:15]
        ]
        market_summary = "נתוני שוק לפי עיר:\n" + "\n".join(market_lines)
    else:
        market_summary = "אין נתוני שוק זמינים."

    # Build numbered listing descriptions
    items = []
    for i, (listing, extracted) in enumerate(zip(listings, extracted_list)):
        city = extracted.get("city") or listing.get("location", "")
        avg = market_ctx.get(city, {}).get("avg_price")
        market_note = f", ממוצע שוק בעיר: ₪{avg:,}" if avg else ""

        broker_val = extracted.get("broker")
        broker_str = "תיווך" if broker_val is True else ("ישיר מבעלים" if broker_val is False else "לא ידוע")

        items.append(
            f"[{i}] כותרת: {listing.get('title', '')}\n"
            f"מחיר: {listing.get('price_text', '') or listing.get('price', '')}{market_note}\n"
            f"מיקום: {listing.get('location', '')}\n"
            f"עיר: {extracted.get('city', '')}, שכונה: {extracted.get('neighborhood', '')}\n"
            f"חדרים: {extracted.get('rooms', '')}, שטח: {extracted.get('size_sqm', '')} מ\"ר, "
            f"קומה: {extracted.get('floor', '')}/{extracted.get('total_floors', '')}\n"
            f"סוג: {extracted.get('property_type', '')}, עסקה: {'השכרה' if extracted.get('for_rent') else 'מכירה'}\n"
            f"מתווך: {broker_str}\n"
            f"תיאור: {(listing.get('description') or '')[:300]}"
        )

    items_text = "\n\n".join(items)
    user_msg = (
        f"{market_summary}\n\n"
        f"יש {len(listings)} דירות לדירוג. החזר מערך JSON עם בדיוק {len(listings)} אובייקטים לפי הסדר:\n\n"
        f"{items_text}"
    )

    client = _get_client()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text
    except Exception as exc:
        _log.error("API error during bulk_score: %s", exc, exc_info=True)
        return [{"score": None, "score_reason": None, "is_broker_suspect": None, "tags": []}] * len(listings)

    parsed = _parse_scores(raw, len(listings))

    results = []
    for item in parsed:
        if item and isinstance(item, dict):
            # Validate tags — only accept values from _VALID_TAGS whitelist
            raw_tags = item.get("tags") or []
            valid_tags = [t for t in raw_tags if isinstance(t, str) and t in _VALID_TAGS]
            results.append({
                "score": float(item["score"]) if item.get("score") is not None else None,
                "score_reason": item.get("score_reason"),
                "is_broker_suspect": bool(item["is_broker_suspect"]) if item.get("is_broker_suspect") is not None else None,
                "tags": valid_tags,
            })
        else:
            results.append({"score": None, "score_reason": None, "is_broker_suspect": None, "tags": []})

    return results

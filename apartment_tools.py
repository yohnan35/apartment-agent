"""
Agent tool implementations — called by the FastAPI chat endpoint.
"""
import asyncio
from typing import Any, Optional

import apartments_db as db
import extractor
import scraper
import scoring
import utils
import yad2_scraper

_log = utils.get_logger(__name__)


def scrape_apartments(query: str = "דירות למכירה", max_results: int = 40) -> dict[str, Any]:
    """Scrape Facebook Marketplace for FOR-SALE listings, extract fields, store in DB."""
    # Hard-lock: always append "למכירה" if not already present so the search targets sales.
    if "למכירה" not in query:
        query = query.strip() + " למכירה"

    listings = asyncio.run(scraper.scrape(query=query, max_results=max_results))
    if not listings:
        return {"scraped": 0, "stored": 0, "message": "לא נמצאו תוצאות"}

    # Always set for_rent=False — scraper is sale-only
    for l in listings:
        l["_query_hint_for_rent"] = False

    extracted_list = extractor.bulk_extract(listings)

    # Hard-lock: force for_rent=False on every extracted item regardless of AI output.
    # Belt-and-suspenders — the extractor already applies the hint, but this catches
    # any edge case where the hint was not propagated (fallback path, empty dict, etc.)
    for ext in extracted_list:
        ext["for_rent"] = False

    # Score all listings in one API call
    try:
        score_list = scoring.bulk_score(listings, extracted_list)
    except Exception as exc:
        _log.error("FB scoring failed: %s", exc, exc_info=True)
        score_list = [{"score": None, "score_reason": None, "is_broker_suspect": None, "tags": []}] * len(listings)

    # Merge scores into extracted dicts
    merged_list = []
    for extracted, score_data in zip(extracted_list, score_list):
        merged = {**extracted, **(score_data or {})}
        merged_list.append(merged)

    stored = 0
    failed = 0
    for listing, extracted in zip(listings, merged_list):
        try:
            db.upsert_apartment(listing, extracted)
            stored += 1
        except Exception as exc:
            failed += 1
            _log.error("upsert failed for listing %s: %s", listing.get("listing_id"), exc, exc_info=True)

    return {
        "scraped": len(listings),
        "stored": stored,
        "failed": failed,
        "message": f"נשמרו {stored} דירות מתוך {len(listings)} שנמצאו" + (f" ({failed} נכשלו)" if failed else ""),
    }


def scrape_yad2_apartments(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    max_results: int = 40,
) -> dict[str, Any]:
    """Scrape Yad2 FOR-SALE listings, use pre-structured data, store in DB."""
    listings = yad2_scraper.scrape_yad2(
        city=city,
        min_rooms=min_rooms,
        max_rooms=max_rooms,
        min_price=min_price,
        max_price=max_price,
        for_rent=False,   # hard-locked; rental path removed
        max_results=max_results,
    )
    if not listings:
        return {"scraped": 0, "stored": 0, "message": "לא נמצאו תוצאות ביד2"}

    # Separate listings from their pre-extracted data
    extracted_list = [listing.pop("_extracted", {}) for listing in listings]

    # Score all yad2 listings in one API call
    try:
        score_list = scoring.bulk_score(listings, extracted_list)
    except Exception as exc:
        _log.error("Yad2 scoring failed: %s", exc, exc_info=True)
        score_list = [{"score": None, "score_reason": None, "is_broker_suspect": None, "tags": []}] * len(listings)

    # Merge scores into extracted dicts
    merged_list = [
        {**extracted, **(score_data or {})}
        for extracted, score_data in zip(extracted_list, score_list)
    ]

    stored = 0
    failed = 0
    for listing, extracted in zip(listings, merged_list):
        try:
            db.upsert_apartment(listing, extracted)
            stored += 1
        except Exception as exc:
            failed += 1
            _log.error("Yad2 upsert failed for %s: %s", listing.get("listing_id"), exc, exc_info=True)

    return {
        "scraped": len(listings),
        "stored": stored,
        "failed": failed,
        "message": f"נשמרו {stored} דירות מיד2" + (f" ({failed} נכשלו)" if failed else ""),
    }


def filter_apartments(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    broker: Optional[bool] = None,
    floor: Optional[int] = None,
    for_rent: Optional[bool] = None,
) -> list[dict]:
    return db.get_apartments(
        city=city,
        min_rooms=min_rooms,
        max_rooms=max_rooms,
        min_price=min_price,
        max_price=max_price,
        broker=broker,
        floor=floor,
        for_rent=for_rent,
    )


def get_price_history(listing_id: str) -> list[dict]:
    return db.get_price_history(listing_id)


def get_apartment_stats() -> dict:
    return db.get_apartment_stats()


def scraper_health_check() -> dict:
    return asyncio.run(scraper.health_check())


# ---------------------------------------------------------------------------
# Tool schema definitions for Anthropic tool_use
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "scrape_apartments",
        "description": "מחפש דירות בפייסבוק מרקטפלייס, מחלץ מידע ושומר בבסיס הנתונים",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "מילות החיפוש (ברירת מחדל: דירה)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "מספר מקסימלי של תוצאות (ברירת מחדל: 40)",
                },
            },
        },
    },
    {
        "name": "filter_apartments",
        "description": "מסנן דירות למכירה לפי פרמטרים מבסיס הנתונים המקומי (מכירה בלבד)",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "שם עיר"},
                "min_rooms": {"type": "number", "description": "מינימום חדרים"},
                "max_rooms": {"type": "number", "description": "מקסימום חדרים"},
                "min_price": {"type": "integer", "description": "מחיר מינימלי ₪"},
                "max_price": {"type": "integer", "description": "מחיר מקסימלי ₪"},
                "broker": {"type": "boolean", "description": "true=תיווך, false=ישיר"},
                "floor": {"type": "integer", "description": "קומה ספציפית"},
            },
        },
    },
    {
        "name": "get_price_history",
        "description": "מחזיר היסטוריית מחירים עבור דירה לפי מזהה",
        "input_schema": {
            "type": "object",
            "properties": {
                "listing_id": {"type": "string", "description": "מזהה המודעה"}
            },
            "required": ["listing_id"],
        },
    },
    {
        "name": "get_apartment_stats",
        "description": "מחזיר סטטיסטיקות כלליות: סה\"כ דירות, ממוצע מחיר, תיווך/ישיר",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scraper_health_check",
        "description": "בודק אם הסקרייפר עובד תקין",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scrape_yad2_apartments",
        "description": "מחפש דירות למכירה באתר יד2 ושומר בבסיס הנתונים (מכירה בלבד)",
        "input_schema": {
            "type": "object",
            "properties": {
                "city":       {"type": "string",  "description": "שם עיר בעברית"},
                "min_rooms":  {"type": "number",  "description": "מינימום חדרים"},
                "max_rooms":  {"type": "number",  "description": "מקסימום חדרים"},
                "min_price":  {"type": "integer", "description": "מחיר מינימלי ₪"},
                "max_price":  {"type": "integer", "description": "מחיר מקסימלי ₪"},
                "max_results":{"type": "integer", "description": "מספר מקסימלי תוצאות"},
            },
        },
    },
]

TOOL_MAP = {
    "scrape_apartments": scrape_apartments,
    "scrape_yad2_apartments": scrape_yad2_apartments,
    "filter_apartments": filter_apartments,
    "get_price_history": get_price_history,
    "get_apartment_stats": get_apartment_stats,
    "scraper_health_check": scraper_health_check,
}

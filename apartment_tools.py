"""
Agent tool implementations — called by the FastAPI chat endpoint.
"""
import asyncio
from typing import Any, Optional

import apartments_db as db
import extractor
import scraper
import scoring
import yad2_scraper


def scrape_apartments(query: str = "דירה", max_results: int = 40) -> dict[str, Any]:
    """Scrape Facebook Marketplace, extract fields, store in DB."""
    listings = asyncio.run(scraper.scrape(query=query, max_results=max_results))
    if not listings:
        return {"scraped": 0, "stored": 0, "message": "לא נמצאו תוצאות"}

    # העבר hint לextractor: אם ה-query כולל "למכירה" → for_rent=false
    is_sale = "למכירה" in query
    for l in listings:
        l["_query_hint_for_rent"] = False if is_sale else None

    extracted_list = extractor.bulk_extract(listings)

    # Score all listings in one API call
    try:
        score_list = scoring.bulk_score(listings, extracted_list)
    except Exception as exc:
        print(f"[scoring error] {exc}")
        score_list = [{"score": None, "score_reason": None, "is_broker_suspect": None}] * len(listings)

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
            print(f"[upsert error] listing {listing.get('listing_id')}: {exc}")

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
    for_rent: bool = True,
    max_results: int = 40,
) -> dict[str, Any]:
    """Scrape Yad2, use pre-structured data (no extractor needed), store in DB."""
    listings = yad2_scraper.scrape_yad2(
        city=city,
        min_rooms=min_rooms,
        max_rooms=max_rooms,
        min_price=min_price,
        max_price=max_price,
        for_rent=for_rent,
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
        print(f"[yad2 scoring error] {exc}")
        score_list = [{"score": None, "score_reason": None, "is_broker_suspect": None}] * len(listings)

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
            print(f"[yad2 upsert error] {listing.get('listing_id')}: {exc}")

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
        "description": "מסנן דירות לפי פרמטרים מבסיס הנתונים המקומי",
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
                "for_rent": {"type": "boolean", "description": "true=השכרה, false=מכירה"},
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
        "description": "מחפש דירות באתר יד2 ושומר בבסיס הנתונים",
        "input_schema": {
            "type": "object",
            "properties": {
                "city":       {"type": "string",  "description": "שם עיר בעברית"},
                "min_rooms":  {"type": "number",  "description": "מינימום חדרים"},
                "max_rooms":  {"type": "number",  "description": "מקסימום חדרים"},
                "min_price":  {"type": "integer", "description": "מחיר מינימלי ₪"},
                "max_price":  {"type": "integer", "description": "מחיר מקסימלי ₪"},
                "for_rent":   {"type": "boolean", "description": "true=השכרה, false=מכירה"},
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

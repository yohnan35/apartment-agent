"""
Agent tool implementations — called by the FastAPI chat endpoint.
"""
import asyncio
from typing import Any, Optional

import apartments_db as db
import extractor
import scraper


def scrape_apartments(query: str = "דירה", max_results: int = 40) -> dict[str, Any]:
    """Scrape Facebook Marketplace, extract fields, store in DB."""
    listings = asyncio.run(scraper.scrape(query=query, max_results=max_results))
    if not listings:
        return {"scraped": 0, "stored": 0, "message": "לא נמצאו תוצאות"}

    extracted_list = extractor.bulk_extract(listings)

    stored = 0
    failed = 0
    for listing, extracted in zip(listings, extracted_list):
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
]

TOOL_MAP = {
    "scrape_apartments": scrape_apartments,
    "filter_apartments": filter_apartments,
    "get_price_history": get_price_history,
    "get_apartment_stats": get_apartment_stats,
    "scraper_health_check": scraper_health_check,
}

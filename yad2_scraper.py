"""
Yad2 real-estate scraper using the public JSON API.
No login or Playwright required.
"""
import re
from typing import Optional

import httpx

YAD2_RENT_URL = "https://gw.yad2.co.il/feed-search-legacy/realestate/rent"
YAD2_SALE_URL = "https://gw.yad2.co.il/feed-search-legacy/realestate/forsale"
YAD2_ITEM_BASE = "https://www.yad2.co.il/item/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
    "Referer": "https://www.yad2.co.il/",
    "Origin": "https://www.yad2.co.il",
}


def scrape_yad2(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    for_rent: bool = True,
    max_results: int = 40,
) -> list[dict]:
    """
    Fetch listings from Yad2 API.
    Returns list of listing dicts compatible with apartments_db.upsert_apartment.
    Each dict also contains '_extracted' with pre-parsed structured fields.
    """
    url = YAD2_RENT_URL if for_rent else YAD2_SALE_URL

    params: dict = {}
    if city:
        params["text"] = city
    if min_rooms is not None or max_rooms is not None:
        lo = min_rooms or 1
        hi = max_rooms or 10
        params["rooms"] = f"{lo}-{hi}"
    if min_price is not None or max_price is not None:
        lo = min_price or 0
        hi = max_price or 99_999_999
        params["price"] = f"{lo}-{hi}"

    listings: list[dict] = []
    seen: set[str] = set()

    for page in range(1, 6):  # max 5 pages
        if len(listings) >= max_results:
            break
        params["page"] = page
        try:
            resp = httpx.get(url, headers=_HEADERS, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[yad2] page {page} error: {exc}")
            break

        # Support multiple API response shapes
        feed_items = (
            data.get("data", {}).get("feed", {}).get("feed_items")
            or data.get("feed", {}).get("feed_items")
            or data.get("feed_items")
            or []
        )

        if not feed_items:
            break

        for item in feed_items:
            if len(listings) >= max_results:
                break
            # Skip promotional/ad rows
            if item.get("type") in ("ad", "commercial", "promotion"):
                continue
            parsed = _parse_item(item, for_rent)
            if parsed and parsed["listing_id"] not in seen:
                seen.add(parsed["listing_id"])
                listings.append(parsed)

    return listings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_int(val) -> Optional[int]:
    if val is None:
        return None
    m = re.search(r'\d[\d,]*', str(val))
    if m:
        try:
            return int(m.group().replace(",", ""))
        except ValueError:
            pass
    return None


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    m = re.search(r'[\d.]+', str(val))
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _parse_item(item: dict, for_rent: bool) -> Optional[dict]:
    raw_id = str(item.get("id") or item.get("token") or "")
    if not raw_id:
        return None

    listing_id = f"yad2_{raw_id}"
    token = item.get("link_token") or raw_id
    url = f"{YAD2_ITEM_BASE}{token}"

    # Title
    title = (
        item.get("title")
        or item.get("row_1")
        or item.get("subtitle")
        or ""
    ).strip()

    # Price
    price_raw = item.get("price") or item.get("price_only")
    price = _to_int(price_raw)
    price_text = str(price_raw).strip() if price_raw else ""
    # Sanity check
    if price is not None and (price < 200 or price > 100_000_000):
        price = None

    # Location
    city = (item.get("city_text") or item.get("city") or "").strip()
    neighborhood = (item.get("neighborhood_text") or item.get("neighborhood") or "").strip()
    location = ", ".join(p for p in [city, neighborhood] if p)

    # Description from additional rows
    desc_parts = [
        str(item.get("row_2") or ""),
        str(item.get("row_3") or ""),
        str(item.get("row_4") or ""),
    ]
    description = " | ".join(p.strip() for p in desc_parts if p.strip())

    # Rooms
    rooms = _to_float(item.get("rooms_text") or item.get("rooms"))

    # Floor
    floor = _to_int(item.get("floor_text") or item.get("floor"))

    # Total floors
    total_floors = _to_int(item.get("max_floor") or item.get("total_floors"))

    # Size
    size_sqm = _to_float(item.get("square_meters") or item.get("squaremeter") or item.get("area"))

    # Broker: agency_type=1 = private owner, >1 = broker
    agency_type = item.get("agency_type")
    broker: Optional[bool] = None
    if agency_type is not None:
        try:
            broker = int(agency_type) > 1
        except (ValueError, TypeError):
            pass

    if not title:
        title = f"דירה ב-{city}" if city else "דירה"

    return {
        "listing_id": listing_id,
        "url": url,
        "title": title,
        "price": price,
        "price_text": price_text,
        "location": location,
        "description": description,
        # Pre-structured fields — skip extractor for Yad2
        "_extracted": {
            "rooms": rooms,
            "city": city or None,
            "neighborhood": neighborhood or None,
            "floor": floor,
            "total_floors": total_floors,
            "broker": broker,
            "size_sqm": size_sqm,
            "property_type": "דירה",
            "for_rent": for_rent,
        },
    }

"""
Yad2 real-estate scraper using Playwright + __NEXT_DATA__ extraction.
The legacy JSON API (gw.yad2.co.il/feed-search-legacy) was shut down;
we now load the page in a headless browser and grab the embedded SSR payload.
"""
import asyncio
import json
import re
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

YAD2_BASE = "https://www.yad2.co.il"
YAD2_RENT_PATH = "/realestate/rent"
YAD2_SALE_PATH = "/realestate/forsale"
YAD2_ITEM_BASE = "https://www.yad2.co.il/item/"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['he-IL','he','en-US','en']});
    window.chrome = { runtime: {} };
"""


# ---------------------------------------------------------------------------
# Public sync API (wraps async)
# ---------------------------------------------------------------------------

def scrape_yad2(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    for_rent: bool = True,
    max_results: int = 40,
) -> list[dict]:
    """Scrape Yad2 listings using Playwright. Returns listing dicts."""
    return asyncio.run(
        _scrape_yad2_async(
            city=city,
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            min_price=min_price,
            max_price=max_price,
            for_rent=for_rent,
            max_results=max_results,
        )
    )


# ---------------------------------------------------------------------------
# Async implementation
# ---------------------------------------------------------------------------

async def _scrape_yad2_async(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    for_rent: bool = True,
    max_results: int = 40,
) -> list[dict]:
    path = YAD2_RENT_PATH if for_rent else YAD2_SALE_PATH

    # Build query params
    params: list[str] = []
    if city:
        params.append(f"text={city}")
    if min_rooms is not None or max_rooms is not None:
        lo = min_rooms or 1
        hi = max_rooms or 10
        params.append(f"rooms={lo}-{hi}")
    if min_price is not None or max_price is not None:
        lo = min_price or 0
        hi = max_price or 99_999_999
        params.append(f"price={lo}-{hi}")

    url = YAD2_BASE + path
    if params:
        url += "?" + "&".join(params)

    listings: list[dict] = []
    seen: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            extra_http_headers={"Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8"},
        )
        await ctx.add_init_script(_STEALTH_SCRIPT)
        page = await ctx.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            # Wait a moment for JS to hydrate
            await asyncio.sleep(2.5)

            # Extract __NEXT_DATA__ SSR payload
            next_data_str: Optional[str] = await page.evaluate(
                "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
            )

            if not next_data_str:
                print("[yad2] __NEXT_DATA__ not found on page")
                await browser.close()
                return []

            data = json.loads(next_data_str)

            # Feed items can live at different paths depending on Yad2 page version
            feed_items = (
                data.get("props", {}).get("pageProps", {}).get("feed", {}).get("feed_items")
                or data.get("props", {}).get("pageProps", {}).get("feedItems")
                or data.get("props", {}).get("pageProps", {}).get("initialData", {}).get("feed_items")
                or []
            )

            for item in feed_items:
                if len(listings) >= max_results:
                    break
                if item.get("type") in ("ad", "commercial", "promotion"):
                    continue
                parsed = _parse_item(item, for_rent)
                if parsed and parsed["listing_id"] not in seen:
                    seen.add(parsed["listing_id"])
                    listings.append(parsed)

        except PWTimeout:
            print("[yad2] page load timed out")
        except Exception as exc:
            print(f"[yad2] scrape error: {exc}")
        finally:
            await browser.close()

    print(f"[yad2] scraped {len(listings)} listings from {url}")
    return listings


# ---------------------------------------------------------------------------
# Parsing helpers
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

    title = (
        item.get("title")
        or item.get("row_1")
        or item.get("subtitle")
        or ""
    ).strip()

    price_raw = item.get("price") or item.get("price_only")
    price = _to_int(price_raw)
    price_text = str(price_raw).strip() if price_raw else ""
    if price is not None and (price < 200 or price > 100_000_000):
        price = None

    city = (item.get("city_text") or item.get("city") or "").strip()
    neighborhood = (item.get("neighborhood_text") or item.get("neighborhood") or "").strip()
    location = ", ".join(p for p in [city, neighborhood] if p)

    desc_parts = [
        str(item.get("row_2") or ""),
        str(item.get("row_3") or ""),
        str(item.get("row_4") or ""),
    ]
    description = " | ".join(p.strip() for p in desc_parts if p.strip())

    rooms = _to_float(item.get("rooms_text") or item.get("rooms"))
    floor = _to_int(item.get("floor_text") or item.get("floor"))
    total_floors = _to_int(item.get("max_floor") or item.get("total_floors"))
    size_sqm = _to_float(item.get("square_meters") or item.get("squaremeter") or item.get("area"))

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

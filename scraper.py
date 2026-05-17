"""
Facebook Marketplace apartment scraper using Playwright with saved session.
Run `python scraper.py --login` once to save cookies to fb_session.json.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout

_DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
SESSION_FILE = _DATA_DIR / "fb_session.json"
MARKETPLACE_BASE = "https://www.facebook.com/marketplace/112308178781459"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape(query: str = "דירה", max_results: int = 40) -> list[dict]:
    """Return a list of listing dicts from Facebook Marketplace."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        ctx = await _load_session(browser)
        page = await ctx.new_page()
        try:
            listings = await _scrape_page(page, query, max_results)
        finally:
            await browser.close()
    return listings


async def health_check() -> dict:
    """
    Returns {"ok": True/False, "reason": str, "count": int}.
    Flags broken if zero results or >80% listings missing price.
    """
    try:
        listings = await scrape(query="דירה", max_results=20)
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "count": 0}

    if not listings:
        return {"ok": False, "reason": "zero results returned", "count": 0}

    missing_price = sum(1 for l in listings if not l.get("price"))
    ratio = missing_price / len(listings)
    if ratio > 0.8:
        return {
            "ok": False,
            "reason": f"{missing_price}/{len(listings)} listings missing price — scraper likely blocked",
            "count": len(listings),
        }
    return {"ok": True, "reason": "ok", "count": len(listings)}


async def login_and_save_session() -> None:
    """Open a visible browser for manual login, then save cookies."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com/login")
        print("Log in manually in the browser window, then press ENTER here...")
        input()
        storage = await ctx.storage_state()
        SESSION_FILE.write_text(json.dumps(storage), encoding="utf-8")
        print(f"Session saved to {SESSION_FILE}")
        await browser.close()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CONTEXT_OPTIONS = dict(
    user_agent=_USER_AGENT,
    viewport={"width": 1280, "height": 800},
    locale="he-IL",
    timezone_id="Asia/Jerusalem",
    extra_http_headers={
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    },
)

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['he-IL','he','en-US','en']});
    window.chrome = { runtime: {} };
"""


async def _load_session(browser) -> BrowserContext:
    if SESSION_FILE.exists():
        state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        ctx = await browser.new_context(storage_state=state, **_CONTEXT_OPTIONS)
    else:
        ctx = await browser.new_context(**_CONTEXT_OPTIONS)
    await ctx.add_init_script(_STEALTH_SCRIPT)
    return ctx


async def _scrape_page(page: Page, query: str, max_results: int) -> list[dict]:
    from urllib.parse import quote

    # Target Israeli marketplace using Facebook's numeric location ID for Israel
    search_url = (
        f"{MARKETPLACE_BASE}/search/"
        f"?query={quote(query)}"
        f"&category_id=propertyrentals"
    )

    await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

    # Detect login wall — FB redirects to /login or shows a login form
    if "/login" in page.url or await page.locator('input[name="email"]').count() > 0:
        raise RuntimeError("פייסבוק דורש התחברות — יש לייצא Cookies מחדש מהדפדפן")

    # Small human-like delay after page load
    await asyncio.sleep(1.2)

    # Dismiss cookie/login dialogs if present
    for selector in [
        '[aria-label="Close"]',
        '[data-testid="cookie-policy-dialog-accept-button"]',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
        except Exception:
            pass

    listings: list[dict] = []
    seen_urls: set[str] = set()
    scroll_attempts = 0
    max_scrolls = max(20, max_results // 2)

    while len(listings) < max_results and scroll_attempts < max_scrolls:
        cards = await page.locator('a[href*="/marketplace/item/"]').all()
        for card in cards:
            if len(listings) >= max_results:
                break
            try:
                href = await card.get_attribute("href")
                if not href:
                    continue
                url = "https://www.facebook.com" + href if href.startswith("/") else href
                # Normalise — strip query params for dedup
                base_url = url.split("?")[0].rstrip("/")
                if base_url in seen_urls:
                    continue
                seen_urls.add(base_url)

                listing = await _extract_card_data(card, url)
                if listing:
                    listings.append(listing)
            except Exception:
                continue

        # Check for login wall mid-scrape
        if "/login" in page.url or await page.locator('input[name="email"]').count() > 0:
            print("[scraper] session expired mid-scrape — returning partial results")
            break

        await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
        await asyncio.sleep(1.5)
        scroll_attempts += 1

    return listings


_HEBREW_RE = re.compile(r'[א-ת]')  # Hebrew Unicode block

async def _extract_card_data(card, url: str) -> Optional[dict]:
    """Pull title, price, location text from a marketplace card element."""
    try:
        all_text = (await card.inner_text()).strip()
    except Exception:
        return None

    if not all_text:
        return None

    # Israel-only filter: skip cards with no Hebrew characters at all
    if not _HEBREW_RE.search(all_text):
        return None

    lines = [l.strip() for l in all_text.splitlines() if l.strip()]

    title = lines[0] if lines else ""
    price_text = ""
    price = None
    location = ""
    description = ""

    for line in lines:
        # Price: ₪, ש"ח, ILS, or $
        if re.search(r"[₪$]|ש[\"״]ח|ILS", line):
            price_text = line
            # Prefer number immediately adjacent to currency symbol
            m = re.search(r'₪\s*([\d,]+)|([\d,]+)\s*(?:₪|ש[\"״]ח)', line)
            if m:
                num_str = (m.group(1) or m.group(2)).replace(",", "")
                try:
                    price = int(num_str)
                except ValueError:
                    pass
            if price is None:
                # Fallback: take the largest number > 200 in the line
                candidates = []
                for n in re.findall(r'\d[\d,]*', line):
                    try:
                        v = int(n.replace(",", ""))
                        if v > 200:
                            candidates.append(v)
                    except ValueError:
                        pass
                if candidates:
                    price = max(candidates)
            # Sanity check: reject obviously wrong values
            if price is not None and (price < 200 or price > 100_000_000):
                price = None
            continue
        # Location: first non-title, non-price line
        if not location and len(line) > 2 and line != title:
            location = line

    # Remaining lines become description
    description = " | ".join(lines[1:4])

    listing_id = _extract_id(url)

    return {
        "listing_id": listing_id,
        "url": url,
        "title": title,
        "price": price,
        "price_text": price_text,
        "location": location,
        "description": description,
    }


def _extract_id(url: str) -> str:
    m = re.search(r"/item/(\d+)", url)
    return m.group(1) if m else url.split("/")[-1].split("?")[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--login" in sys.argv:
        asyncio.run(login_and_save_session())
    elif "--health" in sys.argv:
        result = asyncio.run(health_check())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        query = sys.argv[1] if len(sys.argv) > 1 else "דירה להשכרה"
        results = asyncio.run(scrape(query=query, max_results=10))
        print(json.dumps(results, ensure_ascii=False, indent=2))

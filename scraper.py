"""
Facebook Marketplace apartment scraper using Playwright with saved session.
Run `python scraper.py --login` once to save cookies to fb_session.json.
"""
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout
try:
    from playwright_stealth import stealth_async
    _STEALTH_LIB = True
except ImportError:
    _STEALTH_LIB = False

_DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
SESSION_FILE = _DATA_DIR / "fb_session.json"
MARKETPLACE_BASE = "https://www.facebook.com/marketplace/112308178781459"

# Optional proxy — set PROXY_URL=http://user:pass@host:port in env
PROXY_URL = os.environ.get("PROXY_URL")  # e.g. "http://user:pass@1.2.3.4:8080"


# ---------------------------------------------------------------------------
# Rotating User-Agents (Windows Chrome — realistic & up to date)
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape(query: str = "דירה", max_results: int = 40) -> list[dict]:
    """Return a list of listing dicts from Facebook Marketplace."""
    user_agent = random.choice(_USER_AGENTS)
    proxy = {"server": PROXY_URL} if PROXY_URL else None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-extensions",
                "--window-size=1280,800",
                "--disable-gpu",
            ],
        )
        ctx = await _load_session(browser, user_agent=user_agent)
        page = await ctx.new_page()
        if _STEALTH_LIB:
            await stealth_async(page)
        try:
            listings = await _scrape_page(page, query, max_results)
            # Save updated cookies back to DB to keep session fresh
            await _save_session(ctx)
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

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['he-IL','he','en-US','en']});
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    Object.defineProperty(navigator, 'permissions', {
        get: () => ({ query: () => Promise.resolve({ state: 'granted' }) })
    });
"""


def _build_context_options(user_agent: str) -> dict:
    # Randomise viewport slightly so every session looks different
    width  = random.choice([1280, 1366, 1440, 1536, 1920])
    height = random.choice([720, 768, 800, 864, 900, 1080])
    return dict(
        user_agent=user_agent,
        viewport={"width": width, "height": height},
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
        extra_http_headers={
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


# Keep a stable version for the debug endpoint / _load_session fallback
_CONTEXT_OPTIONS = _build_context_options(_USER_AGENTS[0])


async def _load_session(browser, user_agent: str = _USER_AGENTS[0]) -> BrowserContext:
    import apartments_db as db
    state = None

    # 1. Try DB (survives redeploys)
    raw = db.get_kv("fb_session")
    if raw:
        try:
            state = json.loads(raw)
        except Exception:
            state = None

    # 2. Fallback: legacy file (for local dev)
    if state is None and SESSION_FILE.exists():
        try:
            state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            # Migrate to DB so future runs use DB
            db.set_kv("fb_session", json.dumps(state))
        except Exception:
            state = None

    opts = _build_context_options(user_agent)
    if state is not None:
        ctx = await browser.new_context(storage_state=state, **opts)
    else:
        ctx = await browser.new_context(**opts)
    await ctx.add_init_script(_STEALTH_SCRIPT)
    return ctx


async def _save_session(ctx: BrowserContext) -> None:
    """Persist updated cookies back to DB after scraping (keeps session alive)."""
    try:
        import apartments_db as db
        storage = await ctx.storage_state()
        db.set_kv("fb_session", json.dumps(storage))
    except Exception:
        pass


async def _human_mouse_move(page: Page) -> None:
    """Move mouse to a random position on the page (mimics human behaviour)."""
    try:
        x = random.randint(200, 1000)
        y = random.randint(200, 600)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        # Small second move
        await page.mouse.move(x + random.randint(-30, 30), y + random.randint(-30, 30))
    except Exception:
        pass


async def _scrape_page(page: Page, query: str, max_results: int) -> list[dict]:
    from urllib.parse import quote

    search_url = (
        f"{MARKETPLACE_BASE}/search/"
        f"?query={quote(query)}"
        f"&category_id=propertyrentals"
    )

    await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

    # Detect login wall
    if "/login" in page.url or await page.locator('input[name="email"]').count() > 0:
        raise RuntimeError("פייסבוק דורש התחברות — יש לייצא Cookies מחדש מהדפדפן")

    # Human-like delay after page load (2–4 seconds)
    await asyncio.sleep(random.uniform(2.0, 4.0))

    # Random mouse movement after load
    await _human_mouse_move(page)

    # Dismiss cookie/login dialogs if present
    for selector in [
        '[aria-label="Close"]',
        '[data-testid="cookie-policy-dialog-accept-button"]',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await _human_mouse_move(page)
                await btn.click()
                await asyncio.sleep(random.uniform(0.5, 1.2))
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
                base_url = url.split("?")[0].rstrip("/")
                if base_url in seen_urls:
                    continue
                seen_urls.add(base_url)

                listing = await _extract_card_data(card, url)
                if listing:
                    listings.append(listing)
                    # Random pause between cards (human reads each listing)
                    await asyncio.sleep(random.uniform(0.3, 0.9))
            except Exception:
                continue

        # Check for login wall mid-scrape
        if "/login" in page.url or await page.locator('input[name="email"]').count() > 0:
            print("[scraper] session expired mid-scrape — returning partial results")
            break

        # Occasional random mouse movement while scrolling
        if random.random() < 0.4:
            await _human_mouse_move(page)

        # Human-like scroll: 2–3 small scrolls with pauses
        num_scrolls = random.randint(2, 3)
        for _ in range(num_scrolls):
            scroll_px = random.randint(400, 900)
            await page.evaluate(f"window.scrollBy(0, {scroll_px})")
            await asyncio.sleep(random.uniform(0.4, 0.9))

        # Pause between scroll rounds
        await asyncio.sleep(random.uniform(1.5, 3.0))
        scroll_attempts += 1

    return listings


_HEBREW_RE = re.compile(r'[א-ת]')


async def _extract_card_data(card, url: str) -> Optional[dict]:
    """Pull title, price, location text from a marketplace card element."""
    try:
        all_text = (await card.inner_text()).strip()
    except Exception:
        return None

    if not all_text:
        return None

    if not _HEBREW_RE.search(all_text):
        return None

    lines = [l.strip() for l in all_text.splitlines() if l.strip()]

    title = lines[0] if lines else ""
    price_text = ""
    price = None
    location = ""

    for line in lines:
        if re.search(r"[₪$]|ש[\"״]ח|ILS", line):
            price_text = line
            m = re.search(r'₪\s*([\d,]+)|([\d,]+)\s*(?:₪|ש[\"״]ח)', line)
            if m:
                num_str = (m.group(1) or m.group(2)).replace(",", "")
                try:
                    price = int(num_str)
                except ValueError:
                    pass
            if price is None:
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
            if price is not None and (price < 200 or price > 100_000_000):
                price = None
            continue
        if not location and len(line) > 2 and line != title:
            location = line

    description = " | ".join(lines[1:4])
    listing_id = _extract_id(url)

    photo_url = None
    try:
        img_el = card.locator("img").first
        src = await img_el.get_attribute("src", timeout=1500)
        if src and src.startswith("http") and ("fbcdn" in src or "scontent" in src):
            photo_url = src
    except Exception:
        pass

    return {
        "listing_id": listing_id,
        "url": url,
        "title": title,
        "price": price,
        "price_text": price_text,
        "location": location,
        "description": description,
        "photo_url": photo_url,
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
        query = sys.argv[1] if len(sys.argv) > 1 else "דירה למכירה"
        results = asyncio.run(scrape(query=query, max_results=10))
        print(json.dumps(results, ensure_ascii=False, indent=2))

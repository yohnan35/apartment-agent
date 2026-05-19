"""
FastAPI backend for the Facebook Marketplace apartment agent.
Run: uvicorn api.main:app --reload --port 8000
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import apartment_tools as tools
import apartments_db as db
import scraper as scraper_module

# ---------------------------------------------------------------------------
# Auto-scrape background task
# ---------------------------------------------------------------------------

AUTO_SCRAPE_HOURS = int(os.environ.get("AUTO_SCRAPE_HOURS", "3"))
AUTO_SCRAPE_ENABLED = os.environ.get("AUTO_SCRAPE_ENABLED", "false").lower() == "true"


async def _auto_scrape_loop() -> None:
    """Background task: scrape FB every AUTO_SCRAPE_HOURS hours."""
    await asyncio.sleep(60)  # wait 1 min after startup before first run
    while True:
        print(f"[auto-scrape] starting scheduled scrape (every {AUTO_SCRAPE_HOURS}h)")
        try:
            await asyncio.to_thread(tools.scrape_apartments, query="דירה", max_results=40)
        except Exception as exc:
            print(f"[auto-scrape] FB error: {exc}")
        print(f"[auto-scrape] done — sleeping {AUTO_SCRAPE_HOURS}h")
        await asyncio.sleep(AUTO_SCRAPE_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if AUTO_SCRAPE_ENABLED:
        task = asyncio.create_task(_auto_scrape_loop())
        print(f"[auto-scrape] enabled — interval: {AUTO_SCRAPE_HOURS}h")
    else:
        task = None
    yield
    if task:
        task.cancel()


app = FastAPI(title="FB Marketplace Apartment Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

AGENT_SYSTEM_PROMPT = """אתה סוכן חיפוש דירות חכם בעברית.
אתה עוזר למשתמשים למצוא דירות בפייסבוק מרקטפלייס.
יש לך כלים לסרוק מודעות, לסנן לפי פרמטרים, לבדוק היסטוריית מחירים ולהציג סטטיסטיקות.
תמיד ענה בעברית. היה ידידותי, ישיר ותמציתי.
כשמשתמש מבקש לחפש דירות — השתמש בכלי scrape_apartments לפני filter_apartments.
הצג תוצאות בצורה ברורה עם פרטי מחיר, חדרים ומיקום.
"""


# ---------------------------------------------------------------------------
# Facebook session management
# ---------------------------------------------------------------------------

class CookiesRequest(BaseModel):
    cookies: list[dict]  # Raw cookies from Cookie-Editor browser extension


def _normalize_same_site(val: str | None) -> str:
    """Map Cookie-Editor sameSite values → Playwright accepted values (Strict|Lax|None)."""
    if not val:
        return "Lax"
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",   # Chrome extension value
        "unspecified": "Lax",
    }
    return mapping.get(str(val).lower(), "Lax")


def _convert_cookies_to_playwright(raw_cookies: list[dict]) -> dict:
    """Convert Cookie-Editor extension format → Playwright storage_state format."""
    pw_cookies = []
    for c in raw_cookies:
        pw_cookie: dict[str, Any] = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            pw_cookie["expires"] = float(exp)
        else:
            pw_cookie["expires"] = -1
        pw_cookies.append(pw_cookie)
    return {"cookies": pw_cookies, "origins": []}


@app.post("/session/import")
def import_session(req: CookiesRequest):
    """Accept cookies from the browser extension and save as Playwright session (stored in DB)."""
    if not req.cookies:
        raise HTTPException(status_code=400, detail="רשימת Cookies ריקה")

    fb_cookies = [c for c in req.cookies if "facebook.com" in c.get("domain", "")]
    if not fb_cookies:
        raise HTTPException(status_code=400, detail="לא נמצאו Cookies של פייסבוק")

    state = _convert_cookies_to_playwright(fb_cookies)
    # Persist in DB (survives Railway redeploys)
    db.set_kv("fb_session", json.dumps(state, ensure_ascii=False))
    # Also write legacy file for local dev convenience
    try:
        scraper_module.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        scraper_module.SESSION_FILE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass
    return {"ok": True, "cookies_saved": len(fb_cookies)}


@app.get("/session/status")
def session_status():
    """Check whether a Facebook session exists (DB-backed)."""
    raw = db.get_kv("fb_session")
    if raw and len(raw) > 10:
        return {"connected": True}
    # Fallback: check legacy file
    exists = scraper_module.SESSION_FILE.exists()
    size = scraper_module.SESSION_FILE.stat().st_size if exists else 0
    return {"connected": exists and size > 10}


@app.delete("/session")
def delete_session():
    """Remove saved Facebook session from DB and file."""
    db.delete_kv("fb_session")
    if scraper_module.SESSION_FILE.exists():
        try:
            scraper_module.SESSION_FILE.unlink()
        except Exception:
            pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat / SSE streaming
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]


async def _agent_stream(messages: list[dict]) -> AsyncGenerator[str, None]:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    history = list(messages)
    iterations = 0
    max_iterations = 10

    while iterations < max_iterations:
        iterations += 1
        async with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=AGENT_SYSTEM_PROMPT,
            tools=tools.TOOLS,
            messages=history,
        ) as stream:
            tool_calls: list[dict] = []
            current_tool: dict | None = None
            input_buf = ""
            full_text = ""

            async for event in stream:
                etype = type(event).__name__

                if etype == "RawContentBlockStartEvent":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool = {"id": block.id, "name": block.name}
                        input_buf = ""

                elif etype == "RawContentBlockDeltaEvent":
                    delta = event.delta
                    if hasattr(delta, "text"):
                        full_text += delta.text
                        yield f"data: {json.dumps({'type': 'text', 'text': delta.text})}\n\n"
                    elif hasattr(delta, "partial_json"):
                        input_buf += delta.partial_json

                elif etype == "RawContentBlockStopEvent":
                    if current_tool is not None:
                        try:
                            current_tool["input"] = json.loads(input_buf) if input_buf else {}
                        except json.JSONDecodeError:
                            current_tool["input"] = {}
                        tool_calls.append(current_tool)
                        current_tool = None
                        input_buf = ""

            final_msg = await stream.get_final_message()
            stop_reason = final_msg.stop_reason

        if stop_reason == "end_turn" or not tool_calls:
            break

        tool_results = []
        for tc in tool_calls:
            fn = tools.TOOL_MAP.get(tc["name"])
            if fn is None:
                result = {"error": f"unknown tool {tc['name']}"}
            else:
                yield f"data: {json.dumps({'type': 'tool_call', 'name': tc['name'], 'input': tc['input']})}\n\n"
                try:
                    result = await asyncio.to_thread(fn, **tc["input"])
                except Exception as exc:
                    result = {"error": str(exc)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        history.append({"role": "assistant", "content": final_msg.content})
        history.append({"role": "user", "content": tool_results})

    if iterations >= max_iterations:
        yield f"data: {json.dumps({'type': 'text', 'text': '\n[הגעתי למגבלת הפעולות המקסימלית]'})}\n\n"

    yield "data: [DONE]\n\n"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(
        _agent_stream(req.messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Apartments REST endpoints
# ---------------------------------------------------------------------------

@app.get("/apartments")
def get_apartments(
    city: Optional[str] = None,
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    broker: Optional[bool] = None,
    floor: Optional[int] = None,
    for_rent: Optional[bool] = None,
    hours_fresh: Optional[int] = None,
    limit: int = Query(default=200, le=500),
):
    return db.get_apartments(
        city=city,
        min_rooms=min_rooms,
        max_rooms=max_rooms,
        min_price=min_price,
        max_price=max_price,
        broker=broker,
        floor=floor,
        for_rent=for_rent,
        hours_fresh=hours_fresh,
        limit=limit,
    )


@app.delete("/apartments")
def delete_all_apartments():
    count = db.clear_all_apartments()
    return {"ok": True, "deleted": count}


@app.get("/apartments/stats")
def apartment_stats():
    return db.get_apartment_stats()


@app.get("/apartments/city-stats")
def city_stats():
    return db.get_city_stats()


@app.get("/apartments/price-trends")
def price_trends():
    return db.get_price_trends()


@app.get("/apartments/history/{listing_id}")
def price_history(listing_id: str):
    return db.get_price_history(listing_id)


class ScrapeRequest(BaseModel):
    query: str = "דירה"
    max_results: int = 40


@app.post("/apartments/scrape")
def trigger_scrape(req: ScrapeRequest):
    try:
        return tools.scrape_apartments(query=req.query, max_results=req.max_results)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class Yad2ScrapeRequest(BaseModel):
    city: Optional[str] = None
    min_rooms: Optional[float] = None
    max_rooms: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    for_rent: bool = True
    max_results: int = 40


@app.post("/apartments/scrape/yad2")
def trigger_yad2_scrape(req: Yad2ScrapeRequest):
    try:
        return tools.scrape_yad2_apartments(
            city=req.city,
            min_rooms=req.min_rooms,
            max_rooms=req.max_rooms,
            min_price=req.min_price,
            max_price=req.max_price,
            for_rent=req.for_rent,
            max_results=req.max_results,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Simple liveness probe — always returns 200 if the process is alive."""
    return {"ok": True}


@app.get("/debug/scraper")
async def debug_scraper():
    """Debug: load FB Marketplace and return page URL, title, card count.
    Tries without session first (public access), then with session."""
    from playwright.async_api import async_playwright
    from urllib.parse import quote
    result = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            # Try WITHOUT session first
            ctx_no_session = await browser.new_context(**scraper_module._CONTEXT_OPTIONS)
            await ctx_no_session.add_init_script(scraper_module._STEALTH_SCRIPT)
            page_no_session = await ctx_no_session.new_page()
            url = (f"https://www.facebook.com/marketplace/112308178781459/search/"
                   f"?query={quote('דירה')}&category_id=propertyrentals")
            await page_no_session.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)
            await page_no_session.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)
            result["no_session_url"] = page_no_session.url
            result["no_session_login"] = "/login" in page_no_session.url
            cards_no = await page_no_session.locator('a[href*="/marketplace/item/"]').all()
            result["no_session_cards"] = len(cards_no)
            await ctx_no_session.close()

            # Also try WITH session (if exists)
            ctx = await scraper_module._load_session(browser)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)
            # Scroll to trigger lazy-load
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)
            result["final_url"] = page.url
            result["title"] = await page.title()
            result["is_login"] = "/login" in page.url
            result["email_input"] = await page.locator('input[name="email"]').count() > 0
            # Try multiple selectors
            cards1 = await page.locator('a[href*="/marketplace/item/"]').all()
            cards2 = await page.locator('[data-testid*="marketplace"]').all()
            result["card_count_item"] = len(cards1)
            result["card_count_testid"] = len(cards2)
            # Sample HTML to find real selector
            html_snip = await page.content()
            # Find first occurrence of /marketplace/item/ in HTML
            idx = html_snip.find("/marketplace/item/")
            result["item_link_found"] = idx > -1
            result["item_link_sample"] = html_snip[max(0,idx-50):idx+80] if idx > -1 else "NOT FOUND"
            body_text = await page.locator("body").inner_text()
            result["body_preview"] = body_text[:400].replace("\n", " ")
            await browser.close()
    except Exception as e:
        result["error"] = str(e)
    return result


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    html_file = FRONTEND_DIR / "apartments.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

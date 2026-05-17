# סוכן דירות - Facebook Marketplace

Agent for scraping, extracting, and filtering Hebrew apartment listings from Facebook Marketplace.

## Setup

```powershell
cd fb-marketplace-agent
pip install -r requirements.txt
playwright install chromium
```

Set your Anthropic API key:
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## First-time Facebook login

Run once to save your session cookies:
```powershell
python scraper.py --login
```
Log in manually in the browser window that opens, then press ENTER. This saves `fb_session.json`.

## Run the server

```powershell
uvicorn api.main:app --reload --port 8000
```

Open http://localhost:8000 in your browser.

## File structure

| File | Purpose |
|------|---------|
| `scraper.py` | Playwright-based Facebook Marketplace scraper |
| `extractor.py` | claude-haiku field extraction from Hebrew text |
| `apartments_db.py` | SQLite storage with price history |
| `apartment_tools.py` | Agent tool implementations + Anthropic tool schemas |
| `api/main.py` | FastAPI backend + SSE streaming chat |
| `frontend/apartments.html` | Single-file dark-theme RTL frontend |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Frontend UI |
| GET | `/apartments` | Filter listings |
| GET | `/apartments/stats` | Aggregate stats |
| GET | `/apartments/history/{id}` | Price history |
| POST | `/apartments/scrape` | Trigger scrape |
| POST | `/chat/stream` | SSE agent chat |

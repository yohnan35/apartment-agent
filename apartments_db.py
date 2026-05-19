"""
SQLite storage for apartment listings and price history.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

_DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DATA_DIR / "apartments.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartments (
    listing_id      TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT,
    price           INTEGER,
    price_text      TEXT,
    location        TEXT,
    description     TEXT,
    rooms           REAL,
    city            TEXT,
    neighborhood    TEXT,
    floor           INTEGER,
    total_floors    INTEGER,
    broker          INTEGER,   -- 1=broker 0=direct NULL=unknown
    size_sqm        REAL,
    property_type   TEXT,
    for_rent        INTEGER,   -- 1=rent 0=sale NULL=unknown
    score           REAL,      -- AI score 1-10
    score_reason    TEXT,      -- Hebrew explanation
    is_broker_suspect INTEGER, -- 1=suspect 0=not NULL=unknown
    entry_date      TEXT,      -- תאריך כניסה (free text, e.g. "מיידי", "01/06/2025")
    phone           TEXT,      -- מספר טלפון של המפרסם
    photo_url       TEXT,      -- URL of first listing photo (from scraper)
    first_seen      TEXT NOT NULL,
    last_updated    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apartment_price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id      TEXT NOT NULL,
    price           INTEGER,
    recorded_at     TEXT NOT NULL,
    FOREIGN KEY(listing_id) REFERENCES apartments(listing_id)
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON apartment_price_history(listing_id);

CREATE INDEX IF NOT EXISTS idx_apartments_city ON apartments(city);
CREATE INDEX IF NOT EXISTS idx_apartments_price ON apartments(price);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(SCHEMA)
        # Migrate: add columns if they don't exist yet (safe for existing DBs)
        for col_def in [
            ("score",             "REAL"),
            ("score_reason",      "TEXT"),
            ("is_broker_suspect", "INTEGER"),
            ("entry_date",        "TEXT"),
            ("phone",             "TEXT"),
            ("photo_url",         "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE apartments ADD COLUMN {col_def[0]} {col_def[1]}")
                con.commit()
            except sqlite3.OperationalError:
                # Column already exists — that's fine
                pass


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_apartment(listing: dict, extracted: dict) -> None:
    """
    Insert or update an apartment.
    Records a price_history entry whenever price changes.
    """
    now = datetime.utcnow().isoformat()
    lid = listing["listing_id"]

    def _bool(v) -> Optional[int]:
        if v is None:
            return None
        return 1 if v else 0

    with _conn() as con:
        existing = con.execute(
            "SELECT price FROM apartments WHERE listing_id = ?", (lid,)
        ).fetchone()

        row = {
            "listing_id": lid,
            "url": listing.get("url", ""),
            "title": listing.get("title", ""),
            "price": listing.get("price"),
            "price_text": listing.get("price_text", ""),
            "location": listing.get("location", ""),
            "description": listing.get("description", ""),
            "rooms": extracted.get("rooms"),
            "city": extracted.get("city"),
            "neighborhood": extracted.get("neighborhood"),
            "floor": extracted.get("floor"),
            "total_floors": extracted.get("total_floors"),
            "broker": _bool(extracted.get("broker")),
            "size_sqm": extracted.get("size_sqm"),
            "property_type": extracted.get("property_type"),
            "for_rent": _bool(extracted.get("for_rent")),
            "score": extracted.get("score"),
            "score_reason": extracted.get("score_reason"),
            "is_broker_suspect": _bool(extracted.get("is_broker_suspect")),
            "entry_date": extracted.get("entry_date"),
            "phone": extracted.get("phone"),
            "photo_url": listing.get("photo_url"),
            "last_updated": now,
        }

        if existing is None:
            row["first_seen"] = now
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row.keys())
            con.execute(f"INSERT INTO apartments ({cols}) VALUES ({placeholders})", row)
            # Record initial price
            if row["price"] is not None:
                con.execute(
                    "INSERT INTO apartment_price_history (listing_id, price, recorded_at) VALUES (?,?,?)",
                    (lid, row["price"], now),
                )
        else:
            old_price = existing["price"]
            new_price = row["price"]
            update_fields = {k: v for k, v in row.items() if k != "listing_id"}
            set_clause = ", ".join(f"{k} = :{k}" for k in update_fields)
            update_fields["listing_id"] = lid
            con.execute(
                f"UPDATE apartments SET {set_clause} WHERE listing_id = :listing_id",
                update_fields,
            )
            # Record price history only on change
            if new_price is not None and new_price != old_price:
                con.execute(
                    "INSERT INTO apartment_price_history (listing_id, price, recorded_at) VALUES (?,?,?)",
                    (lid, new_price, now),
                )


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

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
    limit: int = 200,
) -> list[dict]:
    conditions = []
    params: list[Any] = []

    if city:
        conditions.append("LOWER(city) LIKE LOWER(?)")
        params.append(f"%{city}%")
    if min_rooms is not None:
        conditions.append("rooms >= ?")
        params.append(min_rooms)
    if max_rooms is not None:
        conditions.append("rooms <= ?")
        params.append(max_rooms)
    if min_price is not None:
        conditions.append("price >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("price <= ?")
        params.append(max_price)
    if broker is not None:
        conditions.append("broker = ?")
        params.append(1 if broker else 0)
    if floor is not None:
        conditions.append("floor = ?")
        params.append(floor)
    if for_rent is not None:
        conditions.append("for_rent = ?")
        params.append(1 if for_rent else 0)
    if hours_fresh is not None:
        conditions.append("first_seen >= datetime('now', ?)")
        params.append(f"-{hours_fresh} hours")

    conditions.append("(price_text IS NULL OR price_text = '' OR (price_text NOT LIKE '%$%' AND price_text NOT LIKE '%USD%'))")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT * FROM apartments
        {where}
        ORDER BY last_updated DESC
        LIMIT ?
    """
    params.append(limit)

    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
        # Fetch prev prices separately
        prev_prices: dict[str, Optional[int]] = {}
        for r in rows:
            lid = r["listing_id"]
            ph = con.execute(
                "SELECT price FROM apartment_price_history "
                "WHERE listing_id = ? ORDER BY recorded_at DESC LIMIT 2",
                (lid,),
            ).fetchall()
            prev_prices[lid] = ph[1]["price"] if len(ph) >= 2 else None

    results = []
    for r in rows:
        d = dict(r)
        prev = prev_prices.get(d["listing_id"])
        curr = d.get("price")
        d["price_change"] = (curr - prev) if (curr is not None and prev is not None) else None
        d["price_change_pct"] = (
            round((curr - prev) / prev * 100, 1)
            if (curr is not None and prev is not None and prev != 0)
            else None
        )
        for field in ("broker", "for_rent"):
            val = d.get(field)
            d[field] = None if val is None else bool(val)
        results.append(d)

    return results


def clear_all_apartments() -> int:
    """Delete all apartments and price history. Returns number of deleted rows."""
    with _conn() as con:
        count = con.execute("SELECT COUNT(*) FROM apartments").fetchone()[0]
        con.execute("DELETE FROM apartment_price_history")
        con.execute("DELETE FROM apartments")
    return count


def get_price_history(listing_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT price, recorded_at FROM apartment_price_history "
            "WHERE listing_id = ? ORDER BY recorded_at ASC",
            (listing_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_city_stats() -> list[dict]:
    """Average price, count and avg price/sqm per city — for market chart."""
    with _conn() as con:
        rows = con.execute("""
            SELECT
                city,
                COUNT(*)          AS count,
                AVG(price)        AS avg_price,
                AVG(size_sqm)     AS avg_sqm,
                AVG(CASE WHEN price IS NOT NULL AND size_sqm > 0
                         THEN CAST(price AS REAL)/size_sqm END) AS avg_price_sqm
            FROM apartments
            WHERE city IS NOT NULL AND price IS NOT NULL
              AND (price_text IS NULL OR price_text = ''
                   OR (price_text NOT LIKE '%$%' AND price_text NOT LIKE '%USD%'))
            GROUP BY city
            HAVING count >= 2
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()
    return [
        {
            "city": r["city"],
            "count": r["count"],
            "avg_price": round(r["avg_price"]) if r["avg_price"] else None,
            "avg_sqm": round(r["avg_sqm"], 1) if r["avg_sqm"] else None,
            "avg_price_sqm": round(r["avg_price_sqm"]) if r["avg_price_sqm"] else None,
        }
        for r in rows
    ]


def get_price_trends() -> list[dict]:
    """Monthly average price — for trend line chart."""
    with _conn() as con:
        rows = con.execute("""
            SELECT
                strftime('%Y-%m', first_seen) AS month,
                AVG(price)                    AS avg_price,
                COUNT(*)                      AS count
            FROM apartments
            WHERE price IS NOT NULL AND first_seen IS NOT NULL
            GROUP BY month
            ORDER BY month ASC
            LIMIT 24
        """).fetchall()
    return [
        {"month": r["month"], "avg_price": round(r["avg_price"]), "count": r["count"]}
        for r in rows
    ]


def get_apartment_stats() -> dict:
    with _conn() as con:
        row = con.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN broker = 0 THEN 1 ELSE 0 END) AS direct_count,
                SUM(CASE WHEN broker = 1 THEN 1 ELSE 0 END) AS broker_count,
                AVG(price) AS avg_price,
                MIN(price) AS min_price,
                MAX(price) AS max_price
            FROM apartments
        """).fetchone()
    d = dict(row)
    if d.get("avg_price"):
        d["avg_price"] = round(d["avg_price"])
    return d


# ---------------------------------------------------------------------------
# Init on import
# ---------------------------------------------------------------------------

init_db()

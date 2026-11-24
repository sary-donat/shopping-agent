"""SQLite database: stores, products, prices and the user's remembered choices."""
import re
import sqlite3

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    chain TEXT, store_id INTEGER, name TEXT, address TEXT, city TEXT, lat REAL, lon REAL,
    PRIMARY KEY (chain, store_id)
);
CREATE TABLE IF NOT EXISTS geocache (query TEXT PRIMARY KEY, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS streetcache (query TEXT PRIMARY KEY, lines TEXT);
CREATE TABLE IF NOT EXISTS osm_shops (shop_id TEXT PRIMARY KEY, name TEXT, type TEXT, address TEXT, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS reputation (
    shop_id TEXT PRIMARY KEY, verdict TEXT, summary TEXT, sources TEXT, checked TEXT
);
CREATE TABLE IF NOT EXISTS items (
    item_code TEXT PRIMARY KEY, name TEXT, manufacturer TEXT, quantity TEXT, unit TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    chain TEXT, store_id INTEGER, item_code TEXT, price REAL, updated TEXT,
    PRIMARY KEY (chain, store_id, item_code)
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_code UNINDEXED, name, manufacturer);
CREATE TABLE IF NOT EXISTS preferences (query TEXT PRIMARY KEY, item_code TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _fts_query(terms: list[str]) -> str:
    """'גבינה לבנה', 'קוטג' -> ("גבינה"* AND "לבנה"*) OR ("קוטג"*)"""
    groups = []
    for term in terms:
        words = re.findall(r"\w+", term)
        if words:
            groups.append("(" + " AND ".join(f'"{w}"*' for w in words) + ")")
    return " OR ".join(groups)


def search_items(conn, terms: list[str], limit: int) -> list[dict]:
    """Full-text search in product names. Returns only products that have a price somewhere."""
    query = _fts_query(terms)
    if not query:
        return []
    rows = conn.execute(
        """
        WITH hits AS (
            SELECT item_code, bm25(items_fts) AS score
            FROM items_fts WHERE items_fts MATCH ?
            ORDER BY score LIMIT 300
        )
        SELECT i.item_code, i.name, i.manufacturer, i.quantity, i.unit,
               MIN(p.price) AS min_price, COUNT(*) AS n_stores
        FROM hits h
        JOIN items i ON i.item_code = h.item_code
        JOIN prices p ON p.item_code = i.item_code
        GROUP BY i.item_code
        ORDER BY MIN(h.score), n_stores DESC
        LIMIT ?
        """,
        (query, limit * 6),
    ).fetchall()
    # bm25 alone favors "שוקולד חלב קרם חלב" and "חלבון" over real milk. Products whose name starts with
    # the searched word as a whole word ("חלב תנובה 3%") come first, then the whole word anywhere, then the rest.
    firsts = [re.findall(r"\w+", t)[0] for t in terms if re.findall(r"\w+", t)]

    # Within the same rank, products sold in more stores first: the basic product ("לחם אחיד פרוס", 16 stores)
    # before the specialty one with a short name ("לחם מחמצת", 1 store), which bm25 prefers.
    def rank(r):
        words = re.findall(r"\w+", r["name"] or "")
        return (not (words and words[0] in firsts), not any(w in firsts for w in words), -r["n_stores"])

    return [dict(r) for r in sorted(rows, key=rank)[:limit]]


def get_item(conn, item_code: str) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE item_code = ?", (item_code,)).fetchone()
    return dict(row) if row else None


def prices_by_store(conn, item_codes: list[str]) -> dict[tuple, dict[str, float]]:
    """{(chain, store_id): {item_code: price}} for the requested products."""
    marks = ",".join("?" * len(item_codes))
    result: dict[tuple, dict[str, float]] = {}
    for r in conn.execute(f"SELECT * FROM prices WHERE item_code IN ({marks})", item_codes):
        result.setdefault((r["chain"], r["store_id"]), {})[r["item_code"]] = r["price"]
    return result


def store_locations(conn) -> dict[tuple, tuple[float, float]]:
    """{(chain, store_id): (lat, lon)} for every store that has coordinates."""
    rows = conn.execute("SELECT chain, store_id, lat, lon FROM stores WHERE lat IS NOT NULL")
    return {(r["chain"], r["store_id"]): (r["lat"], r["lon"]) for r in rows}


def store_label(conn, key: tuple) -> str:
    row = conn.execute("SELECT name, address FROM stores WHERE chain=? AND store_id=?", key).fetchone()
    if not row:
        return f"{key[0]} #{key[1]}"
    return f"{key[0]} - {row['name']} ({row['address']})".replace(" ()", "")


def get_preference(conn, query: str) -> str | None:
    row = conn.execute("SELECT item_code FROM preferences WHERE query = ?", (query.strip(),)).fetchone()
    return row["item_code"] if row else None


def save_preference(conn, query: str, item_code: str) -> None:
    conn.execute("INSERT OR REPLACE INTO preferences VALUES (?, ?)", (query.strip(), item_code))
    conn.commit()

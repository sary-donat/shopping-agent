"""Fill shopping.db with a few made-up products, to try the agent before the real download works.

    python -m data.demo

The prices here are invented. Run `python -m data.prices build` afterwards to replace them with real data.
"""
from data import db

STORES = [  # chain, store_id, name, address, city, lat, lon (points in Bnei Brak, about 700 m apart)
    ("DEMO_A", 1, "רשת א' (דמו)", "רחוב הדוגמה 1", "בני ברק", 32.0850, 34.8340),
    ("DEMO_B", 2, "רשת ב' (דמו)", "רחוב הדוגמה 2", "בני ברק", 32.0910, 34.8370),
]
# item_code, name, manufacturer, price at A, price at B (None = not sold there)
ITEMS = [
    ("7290000000011", "חלב טרי 3% בקרטון 1 ליטר", "תנובה", 7.1, 6.9),
    ("7290000000028", "גבינה לבנה 5% 250 גרם", "תנובה", 5.9, 5.5),
    ("7290000000035", "גבינה לבנה 5% 250 גרם", "שטראוס", 5.7, None),
    ("7290000000042", "גבינה לבנה 9% 500 גרם", "תנובה", 10.9, 10.2),
    ("7290000000059", "לחם אחיד פרוס 750 גרם", "אנג'ל", 8.2, 8.2),
    ("7290000000066", "ביצים L תבנית 12", "יצרן ביצים", 12.9, 14.5),
    ("7290000000073", "ביצים M תבנית 12", "יצרן ביצים", 11.9, 12.9),
    ("7290000000080", "קוטג' 5% 250 גרם", "תנובה", 5.9, 5.2),
]


def main() -> None:
    conn = db.connect()
    conn.executemany("INSERT OR REPLACE INTO stores VALUES (?, ?, ?, ?, ?, ?, ?)", STORES)
    for code, name, maker, price_a, price_b in ITEMS:
        conn.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?, '', '')", (code, name, maker))
        for (chain, store_id, *_), price in zip(STORES, (price_a, price_b)):
            if price is not None:
                conn.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, 'demo')", (chain, store_id, code, price))
    conn.execute("DELETE FROM items_fts")
    conn.execute("INSERT INTO items_fts SELECT item_code, name, manufacturer FROM items")
    conn.commit()
    print("Demo database ready (invented prices).")


if __name__ == "__main__":
    main()

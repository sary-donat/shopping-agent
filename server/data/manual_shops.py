"""Shops that are missing from OpenStreetMap and have no price files, added by hand so the agent can show
them as "shops on the way without price data". Edit the list and run again.

    python -m data.manual_shops

Netiv HaChesed (נתיב החסד, also Barkal) publishes prices on a server that has been down since 2026-07,
so for now its branches appear without prices. When the branch address has no house number,
the location is the street's middle, and the address says so.
"""
from data import db, geo

# (name shown to the user, address to search on the map [, what to show as the address])
# The address may be "X פינת Y". Without a house number the location is approximate, and is shown that way.
SHOPS = [
    ("נתיב החסד - ירמיהו", "ירמיהו"),
    ("נתיב החסד - נויפלד", "נויפלד"),
    ("נתיב החסד - שיכון ה' קלישר", "קלישר"),
    ("נתיב החסד - תורת חיים", "תורת חיים"),
    ("נתיב החסד - גניחובסקי", "גניחובסקי"),
    ("נתיב החסד - הרב פרדו", "פרדו"),
    ("נתיב החסד - הרב קוק", "הרב קוק"),
    ("נתיב החסד - חבקוק", "חבקוק"),
    ("נתיב החסד - מלצר", "מלצר"),
    ("בר כל טוב (ברכל) - מתתיהו", "מתתיהו"),
    ("בר כל טוב (ברכל) - שלמה המלך", "שלמה המלך"),
    ("בר כל טוב (ברכל) - רמת אהרון", "יצחק מאיר הכהן"),
]


# What people who shop there say about the prices, by the start of the shop name.
# Shown instead of a web search (agent/nearby.py), marked as local knowledge, not as a price comparison.
LOCAL_NOTES = {
    "נתיב החסד": "המחירים עלולים להיות מעט יקרים, אבל לפעמים יש מבצעים שווים.",
    "בר כל טוב": "המחירים עלולים להיות מעט יקרים, אבל לפעמים יש מבצעים שווים.",
}


def main() -> None:
    conn = db.connect()
    conn.execute("DELETE FROM osm_shops WHERE shop_id LIKE 'manual/%'")
    from agent.route import _locate_point  # understands "X פינת Y" (the junction of two streets)

    for n, (name, address, *shown_as) in enumerate(SHOPS, 1):
        point = _locate_point(conn, address)
        if not point:
            print(f"  ✗ {name} | '{address}' not found on the map - write a house number or another spelling")
            continue
        exact = any(ch.isdigit() for ch in address)
        shown = address if exact else f"{shown_as[0] if shown_as else address} (מיקום משוער)"
        conn.execute("INSERT INTO osm_shops VALUES (?, ?, ?, ?, ?, ?)",
                     (f"manual/{n}", name, "supermarket", shown, *point))
        note = next((text for prefix, text in LOCAL_NOTES.items() if name.startswith(prefix)), None)
        conn.execute("DELETE FROM reputation WHERE shop_id = ?", (f"manual/{n}",))
        if note:  # verdict "local": never replaced by a web search (agent/nearby.py)
            conn.execute("INSERT INTO reputation VALUES (?, 'local', ?, '[]', date('now'))", (f"manual/{n}", note))
        print(f"  ✓ {name} | {shown}")
    conn.commit()


if __name__ == "__main__":
    main()

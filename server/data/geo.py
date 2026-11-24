"""OpenStreetMap access: addresses -> map points (Nominatim), street lines and nearby shops (Overpass).
Free, no key. Usage rules: identify the app, at most 1 request per second, cache results.

    python -m data.geo stores    # add coordinates to the chain stores in the database (once, after build)
    python -m data.geo shops     # save all food shops in the city from the map (once; then no map calls at run time)
"""
import difflib
import json
import math
import re
import sys
import time
from collections import Counter

import requests

from config import CITY, CITY_RADIUS_M, USER_AGENT
from data import db

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [  # the main server first, then public mirrors
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
FOOD_SHOP_TYPES = ["supermarket", "convenience", "greengrocer", "butcher", "deli", "bakery", "dairy", "cheese"]

Point = tuple[float, float]  # (lat, lon)
_last_request = 0.0


def _polite_wait() -> None:
    global _last_request
    time.sleep(max(0.0, 1.0 - (time.time() - _last_request)))  # max 1 request per second
    _last_request = time.time()


RETRY_STATUS = {429, 502, 503, 504}  # "busy" answers: worth waiting and trying again
MAIN_SERVER_WAITS = [0, 5, 15]       # seconds to wait before each try on the main server


def _overpass(query: str) -> list[dict]:
    """Main Overpass server (several tries when it is busy), then the mirrors. Prints why a try failed."""
    tries = [(OVERPASS_URLS[0], wait) for wait in MAIN_SERVER_WAITS] + [(url, 0) for url in OVERPASS_URLS[1:]]
    error, main_failed_for_real = None, False
    for url, wait in tries:
        if url == OVERPASS_URLS[0] and main_failed_for_real:
            continue  # a real error on the main server: waiting will not help, go to the mirrors
        time.sleep(wait)
        _polite_wait()
        try:
            response = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=60)
            response.raise_for_status()
            return response.json()["elements"]
        except requests.RequestException as e:
            error = e
            print(f"   [Overpass {url.split('/')[2]}] {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            busy = isinstance(e, requests.Timeout) or getattr(e.response, "status_code", None) in RETRY_STATUS
            main_failed_for_real = url == OVERPASS_URLS[0] and not busy
    raise error


def geocode(conn, address: str) -> Point | None:
    """Address or place name -> (lat, lon). Adds the city if missing. Cached in the database."""
    query = address if CITY in address else f"{address}, {CITY}"
    row = conn.execute("SELECT lat, lon FROM geocache WHERE query = ?", (query,)).fetchone()
    if row:
        return (row["lat"], row["lon"]) if row["lat"] is not None else None

    _polite_wait()
    response = requests.get(
        NOMINATIM,
        params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "il"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    hits = response.json()
    point = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
    conn.execute("INSERT OR REPLACE INTO geocache VALUES (?, ?, ?)", (query, *(point or (None, None))))
    conn.commit()
    return point


REGEX_SPECIALS = set("\\.^$|?*+()[]{}")


def _street_pattern(street: str) -> str:
    """The street name as a literal Overpass regex, written inside a QL string.
    A quote may be ' or ׳ in the map ("ז'בוטינסקי"), so any character is allowed there. Every other regex
    character is escaped: ".*" would match every street in the city, and a trailing backslash would break
    the query. In a QL string a backslash is itself escaped, so a literal "." is written \\. ."""
    return "".join("." if ch in "'\"׳״" else "\\\\" + ch if ch in REGEX_SPECIALS else ch for ch in street)


def street_lines(conn, street: str) -> list[list[Point]]:
    """All the pieces (OSM 'ways') of a street in the city, each as a list of points. Cached."""
    row = conn.execute("SELECT lines FROM streetcache WHERE query = ?", (street,)).fetchone()
    if row:
        return [[tuple(p) for p in line] for line in json.loads(row["lines"])]

    center = geocode(conn, CITY)
    pattern = _street_pattern(street)
    ways = _overpass(
        f'[out:json][timeout:60];way["highway"]["name"~"{pattern}"]'
        f"(around:{CITY_RADIUS_M},{center[0]},{center[1]});out geom;"
    ) if center else []
    # "כהנמן" may also match "סמטת כהנמן": keep only the name that has the most pieces
    names = Counter(w["tags"]["name"] for w in ways)
    best_name = min(names, key=lambda n: (-names[n], len(n))) if names else None
    lines = [[(g["lat"], g["lon"]) for g in w["geometry"]] for w in ways if w["tags"]["name"] == best_name]

    conn.execute("INSERT OR REPLACE INTO streetcache VALUES (?, ?)", (street, json.dumps(lines)))
    conn.commit()
    return lines


def similar_street(conn, street: str) -> str | None:
    """The city street whose name is closest to a misspelled one ("רביבנוב" -> "רבינוב"), or None.
    The list of all street names is downloaded once and kept in streetcache under the key '*'."""
    row = conn.execute("SELECT lines FROM streetcache WHERE query = '*'").fetchone()
    if row:
        names = json.loads(row["lines"])
    else:
        center = geocode(conn, CITY)
        ways = _overpass(f'[out:json][timeout:60];way["highway"]["name"]'
                         f"(around:{CITY_RADIUS_M},{center[0]},{center[1]});out tags;") if center else []
        names = sorted({w["tags"]["name"] for w in ways})
        conn.execute("INSERT OR REPLACE INTO streetcache VALUES ('*', ?)", (json.dumps(names, ensure_ascii=False),))
        conn.commit()
    # people often write only the end of the name: "רבינוב" for "הרב רבינוב", so compare to every ending
    endings = {}
    for name in names:
        words = name.split()
        for k in range(len(words)):
            endings.setdefault(" ".join(words[k:]), name)
    close = difflib.get_close_matches(street, list(endings), n=1, cutoff=0.75)
    return endings[close[0]] if close else None


def _food_shops(around: str) -> list[dict]:
    """Overpass query for food shops; 'around' is one or more (around:...) filters."""
    shop_filter = 'nwr["shop"~"^(' + "|".join(FOOD_SHOP_TYPES) + ')$"]'
    shops = []
    for el in _overpass(f"[out:json][timeout:90];({around.replace('SHOP', shop_filter)});out center tags;"):
        tags = el.get("tags", {})
        center = el.get("center", el)  # ways/relations have "center", nodes have lat/lon directly
        shops.append({
            "shop_id": f"{el['type']}/{el['id']}",
            "name": tags.get("name", ""),
            "type": tags.get("shop"),
            "address": " ".join(filter(None, [tags.get("addr:street"), tags.get("addr:housenumber")])),
            "point": (center["lat"], center["lon"]),
        })
    return list({s["shop_id"]: s for s in shops}.values())  # the same shop can be near several points


def shops_near(points: list[Point], radius_m: int) -> list[dict]:
    """Food shops within radius_m of any of the points. Uses the saved city shops if there are any
    (python -m data.geo shops), so a normal run needs no map server; otherwise asks Overpass."""
    rows = db.connect().execute("SELECT * FROM osm_shops").fetchall()
    if not rows:
        return _food_shops("".join(f"SHOP(around:{radius_m},{lat},{lon});" for lat, lon in points))
    shops = [dict(r) | {"point": (r["lat"], r["lon"])} for r in rows]
    return [s for s in shops if any(distance_m(s["point"], p) <= radius_m for p in points)]


def download_shops() -> None:
    """Save every food shop within CITY_RADIUS_M of the city center. Run once, again when the map changes."""
    conn = db.connect()
    center = geocode(conn, CITY)
    if not center:
        sys.exit(f"Could not find {CITY} on the map.")
    print(f"Downloading food shops around {CITY} (may take a minute)...")
    shops = _food_shops(f"SHOP(around:{CITY_RADIUS_M},{center[0]},{center[1]});")
    conn.execute("DELETE FROM osm_shops WHERE shop_id NOT LIKE 'manual/%'")  # keep shops added by hand
    conn.executemany(
        "INSERT INTO osm_shops VALUES (?, ?, ?, ?, ?, ?)",
        [(s["shop_id"], s["name"], s["type"], s["address"], *s["point"]) for s in shops],
    )
    conn.commit()
    print(f"Saved {len(shops)} food shops ({sum(1 for s in shops if s['name'])} with a name).")


def distance_m(a: Point, b: Point) -> float:
    """Straight-line distance in meters (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _address_variants(address: str) -> list[str]:
    """Ways to write a chain's store address that OpenStreetMap may know. Empty when the address
    is not a place (a web site, "unknown", or only the city name, which would put the store in the city center)."""
    text = address.strip()
    if not text or "http" in text or "unknown" in text.lower():
        return []
    text = re.sub(r"^(רחוב|רח'|רח|שד'|שדרות)\s+", "", text)
    if not re.sub(r"[\s\-,]|" + r"\s*".join(CITY.split()), "", text.replace("-", " ")):
        return []  # nothing left but the city name
    variants = [text]
    first, _, rest = text.partition(" ")
    if first.startswith("ה") and len(first) > 2:
        variants.append(f"{first[1:]} {rest}")          # הלח"י 28 -> לח"י 28
    spelled = re.sub(r"(?<=[א-ת])(?<!י)יה\b", "ייה", text)  # העליה השניה -> העלייה השנייה
    if spelled != text:
        variants.append(spelled)
    return variants


def geocode_stores() -> None:
    conn = db.connect()
    rows = conn.execute("SELECT chain, store_id, name, address FROM stores WHERE lat IS NULL").fetchall()
    print(f"Locating {len(rows)} stores (about 1 second each)...")
    for r in rows:
        point = None
        for variant in _address_variants(r["address"] or ""):
            point = geocode(conn, variant)
            if point:
                break
        if point:
            conn.execute("UPDATE stores SET lat=?, lon=? WHERE chain=? AND store_id=?", (*point, r["chain"], r["store_id"]))
        print(f"  {'✓' if point else '✗'} {r['chain']} {r['name']} | {r['address']}")
    conn.commit()


if __name__ == "__main__":
    commands = {"stores": geocode_stores, "shops": download_shops}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()

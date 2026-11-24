"""Nearby shops that have no price data, with a careful note on their price reputation from the web."""
import json
from datetime import date, timedelta
from typing import Literal, get_args

from pydantic import BaseModel

from agent import route
from agent.llm import ask, search
from agent.matcher import ShopType
from agent.prompts import REPUTATION_SEARCH, REPUTATION_VERDICT
from config import CHAIN_NAMES_HE, CITY, CORRIDOR_WIDTH_M, EXTRA_STORES_TO_RESEARCH, REPUTATION_MAX_AGE_DAYS
from data import db, geo


class Reputation(BaseModel):
    verdict: Literal["cheap", "average", "expensive", "unknown"]
    summary: str
    sources: list[str]


WORDS = {"cheap": "זולים יחסית", "average": "בינוניים", "expensive": "גבוהים יחסית"}


# A specialty shop is shown only if the list has something it sells; any other kind of shop always.
# Which specialty shops sell each item is decided by the model when it parses the list (ListItem.shop_types).
SPECIALTY_SHOPS = set(get_args(ShopType))


def sells_something(shop_type: str | None, wanted: set[str]) -> bool:
    return shop_type not in SPECIALTY_SHOPS or shop_type in wanted


def other_shops(corridor: dict, wanted: set[str]) -> list[dict]:
    """Named food shops on the user's way that are not one of the chains we have prices for,
    and that may sell something from the list (wanted = the specialty shop types of the items)."""
    radius = max([CORRIDOR_WIDTH_M] + [s["radius"] for s in corridor["spots"]])
    # a branch we already have prices for can be on the map under its own name ("גן ורשא" of מעיין 2000)
    priced = [r["name"] for r in db.connect().execute("SELECT name FROM stores")]
    shops = []
    for shop in geo.shops_near(route.sample_points(corridor), radius):
        if not shop["name"] or any(chain in shop["name"] for chain in CHAIN_NAMES_HE):
            continue
        if len(shop["name"]) >= 4 and any(shop["name"] in name for name in priced):
            continue
        if not sells_something(shop["type"], wanted):
            continue
        where = route.near(corridor, shop["point"])
        if where["included"]:
            shops.append(shop | where)
    return sorted(shops, key=lambda s: s["distance"])


def reputation(conn, shop: dict) -> dict:
    """Web search on the shop's prices. Saved in the database, so each shop is searched at most once a month."""
    row = conn.execute("SELECT * FROM reputation WHERE shop_id = ?", (shop["shop_id"],)).fetchone()
    if row and (row["verdict"] == "local"  # local knowledge (data/manual_shops.py) is kept, not searched again
                or date.fromisoformat(row["checked"]) > date.today() - timedelta(days=REPUTATION_MAX_AGE_DAYS)):
        return {"verdict": row["verdict"], "summary": row["summary"], "sources": json.loads(row["sources"])}

    where = f"{shop['name']}, {shop['address']} {CITY}".replace(" ,", ",")
    text, urls = search(REPUTATION_SEARCH, f"החנות: {where}")
    if urls:
        result = ask(REPUTATION_VERDICT, f"הסיכום:\n{text}\n\nהקישורים:\n" + "\n".join(urls), Reputation)
        # Grounding check in code: keep only sources the search really returned
        result.sources = [u for u in result.sources if u in urls]
        if not result.sources:
            result.verdict = "unknown"
    else:
        result = Reputation(verdict="unknown", summary="", sources=[])

    conn.execute(
        "INSERT OR REPLACE INTO reputation VALUES (?, ?, ?, ?, ?)",
        (shop["shop_id"], result.verdict, result.summary, json.dumps(result.sources), date.today().isoformat()),
    )
    conn.commit()
    return result.model_dump()


def describe(rep: dict) -> str:
    """The cautious sentence the user sees."""
    if rep["verdict"] == "local":
        return f"אין נתוני מחירים. לפי מידע מקומי: {rep['summary']} כדאי לבדוק במקום."
    if rep["verdict"] == "unknown":
        return "אין נתוני מחירים, וגם לא מצאתי ברשת מידע על רמת המחירים."
    return (
        f"אין נתוני מחירים. מהתרשמות ממה שנכתב ברשת, נראה שהמחירים שם {WORDS[rep['verdict']]} "
        f"({rep['summary']}). זה רושם כללי בלבד, לא השוואת מחירים, ולכן כדאי לבדוק במקום. "
        f"מקור: {rep['sources'][0]}"
    )


def research(conn, shops: list[dict]) -> list[dict]:
    """Adds a 'note' to the closest few shops."""
    return [s | {"note": describe(reputation(conn, s))} for s in shops[:EXTRA_STORES_TO_RESEARCH]]

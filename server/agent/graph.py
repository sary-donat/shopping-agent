"""The LangGraph agent.

 parse -> locate -> match -> price -> nearby -> answer
           ^  |      ^  |      |
           |  v      |  v      +--> choose_store ⏸ (cheap or near?) --+
       ask_places ⏸  ask_user ⏸  +--> confirm_split ⏸ (two stores?) --+--> nearby
"""
from typing import TypedDict

import requests
from langgraph.checkpoint.memory import InMemorySaver
from langsmith.utils import ContextThreadPoolExecutor
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from agent import nearby as nearby_shops
from agent import route
from agent.basket import best_split, comparable_prices, size_of, store_options, store_totals
from agent.matcher import ListItem, Place, match_item, parse_request
from config import (CHEAP_VS_NEAR_MIN_SAVING, MAX_ITEMS, MAX_PARALLEL_CALLS, MAX_QUESTION_ROUNDS, MAX_REQUEST_CHARS,
                    PROMPT_VERSION, SPLIT_MIN_SAVING)
from data import db

NEEDS_USER = ("ambiguous", "not_found")
MAX_PLACE_ROUNDS = 2


class State(TypedDict, total=False):
    request: str          # the user's message
    places: list[dict]    # Place objects (as dicts), in the order the user passes them
    corridor: dict | None # the area "on the way", see agent/route.py; None = whole city
    issues: list[dict]    # places the map didn't find, as questions for the user
    place_rounds: int
    items: list[dict]     # parsed ListItem objects (as dicts)
    too_many: int         # items beyond MAX_ITEMS, not checked
    matches: list[dict]   # one result per item, see matcher.match_item
    rounds: int
    on_the_way: bool      # False = no store with prices on the way, showing the closest instead
    best: dict            # cheapest store on the way
    nearest: dict         # closest store
    others: list[dict]
    split: dict | None
    elsewhere: list[dict] # products no store on the way sells: the closest store that does
    ask_cheap_vs_near: bool
    chosen: str           # "cheap" or "near"
    use_split: bool
    other_shops: list[dict]
    answer: str


# ---------- 1. understand the request and find the way on the map ----------

def parse(state: State) -> State:
    parsed = parse_request(state["request"][:MAX_REQUEST_CHARS], PROMPT_VERSION)
    return {"items": [i.model_dump() for i in parsed.items[:MAX_ITEMS]], "matches": [], "rounds": 0,
            "too_many": max(0, len(parsed.items) - MAX_ITEMS),  # each item is a model call: cap the cost
            "places": [p.model_dump() for p in parsed.places], "place_rounds": 0}


def locate(state: State) -> State:
    places = [Place(**p) for p in state["places"]]
    if not places:
        return {"corridor": None, "issues": [{"kind": "none", "question": "לא כתבת איפה אתה גר או איפה תהיה היום."}]}
    try:
        corridor, issues = route.build(db.connect(), places)
    except requests.RequestException:
        return {"corridor": None, "issues": [{"kind": "network",
                                              "question": "שרת המפה (OpenStreetMap) לא ענה כרגע. בדרך כלל זה זמני."}]}
    return {"corridor": None if route.is_empty(corridor) else corridor, "issues": issues}


def ask_places(state: State) -> State:
    """Human-in-the-loop: a place the map didn't find, streets that don't meet, or no place at all."""
    questions = [{"issue": k, "question": issue["question"], "kind": issue["kind"]}
                 for k, issue in enumerate(state["issues"])]
    answers = interrupt({"type": "fix_places", "questions": questions})  # [{"issue": k, "text": ...}], [] = skip
    if not answers:
        return {"place_rounds": MAX_PLACE_ROUNDS}
    if state["issues"][0]["kind"] == "network":  # nothing to fix in the places: just ask the map again
        return {"place_rounds": state["place_rounds"] + 1}

    if state["issues"][0]["kind"] == "none":  # the user now wrote where they are: understand it like the request
        places = parse_request(answers[0]["text"][:MAX_REQUEST_CHARS], PROMPT_VERSION).places
    else:
        for a in answers:  # a free answer ("it's near X, on Y street") is understood like the request, not sent as is
            issue = state["issues"][a["issue"]]
            if issue["kind"] in ("point", "street") and not route.confirms_guess(issue, a["text"]):
                parsed = parse_request(a["text"][:MAX_REQUEST_CHARS], PROMPT_VERSION).places
                a["place"] = parsed[0] if parsed else None
        places = route.apply_fixes([Place(**p) for p in state["places"]], state["issues"], answers)
    return {"places": [p.model_dump() for p in places], "place_rounds": state["place_rounds"] + 1}


# ---------- 2. find the products ----------

def match(state: State) -> State:
    """All items at once: one model call per item, in parallel (10 items took 36 s one after the other).
    Each thread opens its own database connection (SQLite connections can't be shared between threads),
    and LangSmith's executor keeps every call under this step in the trace."""
    matches = list(state["matches"]) or [{"status": "retry"} for _ in state["items"]]
    todo = [i for i, m in enumerate(matches) if m["status"] == "retry"]
    one = lambda i: match_item(db.connect(), ListItem(**state["items"][i]), PROMPT_VERSION)  # noqa: E731
    with ContextThreadPoolExecutor(max_workers=MAX_PARALLEL_CALLS) as pool:
        for i, result in zip(todo, pool.map(one, todo)):
            matches[i] = result
    return {"matches": matches}


def ask_user(state: State) -> State:
    """Human-in-the-loop: one interrupt with all the unclear items together."""
    questions = [
        {"index": i, "text": m["text"], "status": m["status"], "reason": m["reason"], "options": m["options"]}
        for i, m in enumerate(state["matches"]) if m["status"] in NEEDS_USER
    ]
    # a list, one per question: {"index", "pick": item_code, "remember": bool} | {"index", "retype": text} | {"index", "skip": True}
    answers = interrupt({"type": "choose_products", "questions": questions})

    conn = db.connect()
    items, matches = list(state["items"]), [dict(m) for m in state["matches"]]
    for answer in answers:
        i = answer["index"]
        if "pick" in answer:
            picked = next((o for o in matches[i].get("options", []) if o["item_code"] == answer["pick"]), {})
            matches[i] |= {"status": "match", "item_code": answer["pick"], "cheapest": False,
                           "alternatives": picked.get("alternatives", []), "reason": "נבחר על ידי המשתמש"}
            if answer.get("remember"):
                db.save_preference(conn, matches[i]["text"], answer["pick"])
        elif "retype" in answer:
            # short search terms from the model, as for the request ("מסטיק של עלית לא משנה טעם" -> "מסטיק עלית");
            # the full text stays, so the product choice still sees "any flavor is fine"
            parsed = parse_request(answer["retype"][:MAX_REQUEST_CHARS], PROMPT_VERSION).items
            terms = [t for p in parsed for t in p.search_terms] or [answer["retype"]]
            shop_types = list({t for p in parsed for t in p.shop_types}) or items[i].get("shop_types", [])
            items[i] = {"text": answer["retype"], "quantity": items[i]["quantity"], "search_terms": terms,
                        "shop_types": shop_types}
            matches[i] = {"status": "retry"}
        else:
            matches[i] |= {"status": "skipped", "reason": "המשתמש דילג"}
    return {"items": items, "matches": matches, "rounds": state["rounds"] + 1}


# ---------- 3. prices on the way ----------

def price(state: State) -> State:
    conn = db.connect()
    matched = [m for m in state["matches"] if m["status"] == "match"]
    wanted = {m["item_code"]: m["quantity"] for m in matched}
    if not wanted:
        return {"best": {}, "others": [], "split": None, "ask_cheap_vs_near": False}

    # a store that lacks the chosen product may sell an equally good one (closest in size, then cheapest)
    options = {m["item_code"]: [m["item_code"], *m.get("alternatives", [])] for m in matched}
    codes = [c for group in options.values() for c in group]
    sizes = {c: size_of(i["quantity"], i["unit"], i["name"]) for c in codes if (i := db.get_item(conn, c))}
    cheapest = {m["item_code"] for m in matched if m.get("cheapest")}  # "הכי זול": lowest price per kilo
    prices, used = store_options(db.prices_by_store(conn, codes), options, sizes, cheapest)
    where, on_the_way = {}, True
    if state.get("corridor"):  # keep stores on the way; if there are none, the 5 closest
        locations = db.store_locations(conn)
        where = {s: route.near(state["corridor"], locations[s]) for s in prices if s in locations}
        keep = [s for s, w in where.items() if w["included"]]
        on_the_way = bool(keep)
        keep = keep or sorted(where, key=lambda s: where[s]["distance"])[:5]
        prices = {s: prices[s] for s in keep}

    # stores are compared for the same amounts ("compare"); "total" stays what is paid at the register
    compare = comparable_prices(prices, used, sizes)
    totals = store_totals(prices, wanted, compare)
    if where:  # same price: the nearer store first
        totals.sort(key=lambda t: (len(t["missing"]), t["compare"], where[t["store"]]["distance"]))
    best = totals[0]
    complete_enough = [t for t in totals if len(t["missing"]) <= len(best["missing"])]
    nearest = min(complete_enough, key=lambda t: where[t["store"]]["distance"]) if where else best
    ask_cheap_vs_near = nearest is not best and nearest["compare"] - best["compare"] >= CHEAP_VS_NEAR_MIN_SAVING

    split = best_split(prices, wanted, compare)
    worth_split = split and not ask_cheap_vs_near and (
        best["missing"] or best["compare"] - split["compare"] >= SPLIT_MIN_SAVING)

    label = lambda t: _labeled(conn, t, where, used)  # noqa: E731
    return {
        "elsewhere": _elsewhere(conn, list(dict.fromkeys(best["missing"] + nearest["missing"])), options,
                                {m["item_code"]: m["text"] for m in matched}, state.get("corridor")),
        "on_the_way": on_the_way,
        "best": label(best),
        "nearest": label(nearest),
        "others": [label(t) for t in totals[1:4]],
        "ask_cheap_vs_near": ask_cheap_vs_near,
        "split": _labeled_split(conn, split, best["compare"], used) if worth_split else None,
    }


def choose_store(state: State) -> State:
    """Human-in-the-loop: only the user knows if walking further is worth the saving."""
    return {"chosen": interrupt({"type": "cheap_vs_near", "best": state["best"], "nearest": state["nearest"]})}


def confirm_split(state: State) -> State:
    """Human-in-the-loop: only the user knows if saving X ₪ is worth a second stop."""
    return {"use_split": interrupt({"type": "confirm_split", "split": state["split"], "best": state["best"]})}


# ---------- 4. other shops on the way, and the answer ----------

def nearby(state: State) -> State:
    if not state.get("corridor"):
        return {"other_shops": []}
    try:
        wanted = {t for item, m in zip(state["items"], state["matches"]) if m["status"] != "skipped"
                  for t in item.get("shop_types", [])}
        shops = nearby_shops.other_shops(state["corridor"], wanted)
    except requests.RequestException:
        return {"other_shops": [{"name": "", "note": "לא הצלחתי לבדוק במפה חנויות נוספות (שגיאת רשת)."}]}
    return {"other_shops": nearby_shops.research(db.connect(), shops)}


def answer(state: State) -> State:
    conn = db.connect()
    corridor = state.get("corridor")
    lines = [f"📍 {corridor['description']}" if corridor else "📍 בלי מיקום: השוואה בין כל הסניפים בעיר"]
    lines += [f"  ⚠️ {w}" for w in route.warnings(state["places"], state.get("issues", []))]
    if state.get("too_many"):
        lines.append(f"  ⚠️ {too_many_note(state['too_many'])}")

    best = state.get("best")
    store = (state["nearest"] if state.get("chosen") == "near" else best) if best else {}
    for m in state["matches"]:
        if m["status"] == "match":
            item = db.get_item(conn, store.get("used", {}).get(m["item_code"], m["item_code"]))  # what this store sells
            mark = "✗" if m["item_code"] in store.get("missing", []) else "✓"  # ✗ = not sold in the recommended store
            lines.append(f"  {mark} {m['text']} → {item['name']} ({item['manufacturer']}) × {m['quantity']}")
        else:
            lines.append(f"  ✗ {m['text']} - לא נכלל ({m['reason']})")

    if best:
        if not state["on_the_way"]:
            lines += ["", "אין על הדרך סניפים עם נתוני מחירים. אלה הקרובים ביותר:"]
        lines += ["", f"🛒 ההמלצה: {_store_line(store)}"]
        if store["missing"]:
            lines.append(f"  (חסרים שם {len(store['missing'])} מוצרים)")
        for e in [] if state.get("use_split") else [e for e in state.get("elsewhere", []) if e["code"] in store["missing"]]:
            where_else = ("לא נמכר באף סניף עם נתוני מחירים" if not e["store"] else
                          f"אפשר לקנות ב-{e['store']}" + (" (על הדרך)" if e["distance"] == 0 else f", {e['distance']} מ' מהדרך"))
            lines.append(f"  ⚠️ חסר: {e['text']}. {where_else}")
        lines += [f"  השוואה: {_store_line(o)}" for o in [best, *state["others"]] if o["label"] != store["label"]]
        if state.get("use_split"):
            lines += ["", f"קנייה בשתי חנויות - {state['split']['total']:.2f} ₪:"]
            for part in state["split"]["parts"]:
                names = [db.get_item(conn, code)["name"] for code in part["items"]]
                lines.append(f"  {part['store']}: {', '.join(names)}")
    else:
        lines += ["", "לא נמצאו מוצרים לחישוב סל."]

    if state.get("other_shops"):
        lines += ["", "🏪 חנויות נוספות על הדרך, בלי נתוני מחירים:"]
        for s in state["other_shops"]:
            if s["name"]:
                address = f", {s['address']}" if s["address"] else ""
                lines.append(f"  • {s['name']}{address} - {s['distance']} מ' מ{s['from']}")
            lines.append(f"    {s['note']}")
    return {"answer": "\n".join(lines)}


# ---------- helpers ----------

def too_many_note(n: int) -> str:
    return f"הרשימה ארוכה: נבדקו {MAX_ITEMS} הפריטים הראשונים, ו-{n} נוספים לא נבדקו. אפשר לשלוח אותם בבקשה נפרדת."


def _labeled(conn, total: dict, where: dict, used: dict) -> dict:
    """used: {chosen code: the code bought in this store}, when the store sells an alternative instead."""
    w = where.get(total["store"], {})
    return {"label": db.store_label(conn, total["store"]), "total": total["total"], "compare": total["compare"],
            "missing": total["missing"],
            "distance": w.get("distance"), "from": w.get("from"), "key": list(total["store"]),
            "used": used.get(total["store"], {})}


def _store_line(t: dict) -> str:
    where = f" | {t['distance']} מ' מ{t['from']}" if t["distance"] is not None else ""
    same_amount = f" (לאותה כמות: {t['compare']:.2f} ₪)" if abs(t["compare"] - t["total"]) >= 0.01 else ""
    return f"{t['label']} - {t['total']:.2f} ₪{same_amount}{where}"


def _elsewhere(conn, missing: list[str], options: dict, texts: dict, corridor: dict | None) -> list[dict]:
    """For each product missing in the recommended store (no store or pair of stores on the way has everything):
    the closest store in the city that sells it or an alternative. Without a route, there is no "closest"."""
    result = []
    locations = db.store_locations(conn) if corridor else {}
    for chosen in missing:
        sellers = [s for s in db.prices_by_store(conn, options[chosen]) if s in locations]
        if sellers:
            store = min(sellers, key=lambda s: route.near(corridor, locations[s])["distance"])
            near = route.near(corridor, locations[store])  # may be on the way: another store than the recommended
            result.append({"code": chosen, "text": texts[chosen], "store": db.store_label(conn, store),
                           "distance": 0 if near["included"] else near["distance"]})
        else:
            result.append({"code": chosen, "text": texts[chosen], "store": None, "distance": None})
    return result


def _labeled_split(conn, split: dict, best_compare: float, used: dict) -> dict:
    parts = [{"store": db.store_label(conn, store), "items": [used[store].get(c, c) for c in codes]}
             for store, codes in split["plan"].items()]
    return {"total": split["total"], "saving": round(best_compare - split["compare"], 2), "parts": parts}


# ---------- routing ----------

def after_locate(state: State) -> str:
    return "ask_places" if state["issues"] and state["place_rounds"] < MAX_PLACE_ROUNDS else "match"


def after_match(state: State) -> str:
    unclear = any(m["status"] in NEEDS_USER for m in state["matches"])
    return "ask_user" if unclear and state["rounds"] < MAX_QUESTION_ROUNDS else "price"


def after_ask(state: State) -> str:
    return "match" if any(m["status"] == "retry" for m in state["matches"]) else "price"


def after_price(state: State) -> str:
    if state.get("ask_cheap_vs_near"):
        return "choose_store"
    return "confirm_split" if state.get("split") else "nearby"


def build_graph():
    g = StateGraph(State)
    for node in (parse, locate, ask_places, match, ask_user, price, choose_store, confirm_split, nearby, answer):
        g.add_node(node.__name__, node)
    g.add_edge(START, "parse")
    g.add_edge("parse", "locate")
    g.add_conditional_edges("locate", after_locate, ["ask_places", "match"])
    g.add_edge("ask_places", "locate")
    g.add_conditional_edges("match", after_match, ["ask_user", "price"])
    g.add_conditional_edges("ask_user", after_ask, ["match", "price"])
    g.add_conditional_edges("price", after_price, ["choose_store", "confirm_split", "nearby"])
    g.add_edge("choose_store", "nearby")
    g.add_edge("confirm_split", "nearby")
    g.add_edge("nearby", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=InMemorySaver())

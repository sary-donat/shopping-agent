"""From free text to a concrete product: parse the list, search candidates, let the model decide.
Used both by the graph (agent/graph.py) and by the evaluation (eval/run_eval.py)."""
from typing import Literal

from pydantic import BaseModel, Field

from agent.llm import ask
from agent.prompts import MATCH, PARSE
from config import MAX_CANDIDATES
from data import db


ShopType = Literal["bakery", "greengrocer", "butcher", "deli", "dairy", "cheese"]  # OpenStreetMap shop=* values


class ListItem(BaseModel):
    text: str
    quantity: int = 1
    search_terms: list[str]
    shop_types: list[ShopType] = Field(default_factory=list)  # specialty shops that sell it (see agent/nearby.py)


class Anchor(BaseModel):
    kind: Literal["number", "cross_street", "landmark"]
    value: str


class Place(BaseModel):
    kind: Literal["point", "street"]
    text: str                      # point: the full address / place; street: the street name
    start: Anchor | None = None    # street only: where along the street the user starts...
    end: Anchor | None = None      # ...and ends


class ParsedList(BaseModel):
    items: list[ListItem]
    places: list[Place] = Field(default_factory=list)


class OptionAlternatives(BaseModel):
    item_code: str                 # one of the options
    alternatives: list[str]        # products just as good as it, if the user picks it


class Decision(BaseModel):
    status: Literal["match", "ambiguous", "not_found"]
    item_code: str | None = None
    options: list[str] = Field(default_factory=list)
    option_alternatives: list[OptionAlternatives] = Field(default_factory=list)  # ambiguous: per option
    alternatives: list[str] = Field(default_factory=list)  # match: other products from the list that are just as good
    cheapest: bool = False  # the user asked for the cheapest ("הכי זול"): each store takes the lowest price per kilo
    reason: str


def parse_request(text: str, version: str) -> ParsedList:
    """The user's message -> shopping items + the places they mentioned."""
    return ask(PARSE[version], text, ParsedList)


def parse_list(text: str, version: str) -> list[ListItem]:
    return parse_request(text, version).items


def _describe(candidates: list[dict]) -> str:
    return "\n".join(
        f"{c['item_code']} | {c['name']} | {c['manufacturer']} | {c['quantity']} {c['unit']} "
        f"| מ-{c['min_price']:.2f} ₪ | n_stores={c['n_stores']}"
        for c in candidates
    )


def match_item(conn, item: ListItem, version: str, use_memory: bool = True) -> dict:
    """Returns {"text", "quantity", "status", "item_code", "alternatives": [codes], "options": [product dicts], "reason"}.
    alternatives: products just as good as item_code, so a store that lacks item_code can sell one of them instead."""
    result = {"text": item.text, "quantity": item.quantity, "item_code": None, "alternatives": [], "cheapest": False,
              "options": []}

    remembered = db.get_preference(conn, item.text) if use_memory else None
    if remembered:
        return result | {"status": "match", "item_code": remembered, "reason": "בחירה שמורה מפעם קודמת"}

    candidates = db.search_items(conn, item.search_terms, MAX_CANDIDATES)
    if not candidates:
        return result | {"status": "not_found", "reason": "לא נמצאו מוצרים בחיפוש"}

    decision = ask(
        MATCH[version],
        f"הפריט ברשימה: {item.text}\n\nמועמדים (item_code | שם | יצרן | כמות | מחיר | n_stores):\n{_describe(candidates)}",
        Decision,
    )

    # Grounding check in code: the model may only return products that were in the candidate list.
    by_code = {c["item_code"]: c for c in candidates}
    if decision.status == "match" and decision.item_code not in by_code:
        return result | {"status": "not_found", "reason": "המודל החזיר מוצר שלא היה ברשימה"}
    # each option carries its own alternatives, so a product the user picks can also be replaced in a store
    # that lacks it (same grounding check: only products from the candidate list)
    option_alts = {g.item_code: [c for c in g.alternatives if c in by_code and c != g.item_code]
                   for g in decision.option_alternatives}
    options = [by_code[c] | {"alternatives": option_alts.get(c, [])} for c in decision.options if c in by_code][:3]
    if decision.status == "ambiguous" and not options:
        options = [c | {"alternatives": []} for c in candidates[:3]]

    is_match = decision.status == "match"
    return result | {
        "status": decision.status,
        "item_code": decision.item_code if is_match else None,
        # the same grounding check: only products from the candidate list
        "alternatives": [c for c in decision.alternatives if c in by_code and c != decision.item_code] if is_match else [],
        "cheapest": is_match and decision.cheapest,
        "options": options if decision.status == "ambiguous" else [],
        "reason": decision.reason,
    }

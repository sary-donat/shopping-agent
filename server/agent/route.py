"""From the places the user wrote to a "corridor" on the map: the area where a store counts as "on the way".

A corridor has two kinds of pieces:
  parts - street pieces the user walks along (a store counts if it is up to CORRIDOR_WIDTH_M from them)
  spots - single points: an exact address (POINT_RADIUS_M around it), or the ends of a street piece

All the geometry is plain code. The model only turned the user's words into Place objects (agent/matcher.py).
"""
import math
import re

from agent.matcher import Anchor, Place
from config import (CITY, CORRIDOR_WIDTH_M, JUNCTION_MAX_GAP_M, LANDMARK_MAX_FROM_STREET_M,
                    POINT_RADIUS_M, TURN_REACH_M)
from data import geo
from data.geo import Point

Segment = tuple[Point, Point]


# ---------- geometry ----------

def _xy(p: Point, lat0: float) -> tuple[float, float]:
    """(lat, lon) -> meters on a flat local map. Accurate enough inside a city."""
    return p[1] * 111_320 * math.cos(math.radians(lat0)), p[0] * 110_540


def _t_and_distance(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Where p falls along a->b (0 = at a, 1 = at b), and its distance in meters from the segment."""
    (px, py), (ax, ay), (bx, by) = (_xy(q, p[0]) for q in (p, a, b))
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = ((px - ax) * dx + (py - ay) * dy) / length2 if length2 else 0.0
    tc = max(0.0, min(1.0, t))
    return t, math.hypot(px - ax - tc * dx, py - ay - tc * dy)


def _segments(lines: list[list[Point]]) -> list[Segment]:
    return [(line[k], line[k + 1]) for line in lines for k in range(len(line) - 1)]


def distance_to_segments(p: Point, segments: list[Segment]) -> float:
    return min((_t_and_distance(p, a, b)[1] for a, b in segments), default=math.inf)


def junction(lines_a: list[list[Point]], lines_b: list[list[Point]]) -> Point | None:
    """The point where two streets meet, or None if they don't come close enough."""
    segments_b = _segments(lines_b)
    gap, point = min(((distance_to_segments(p, segments_b), p) for line in lines_a for p in line),
                     default=(math.inf, None))
    return point if gap <= JUNCTION_MAX_GAP_M else None


def cut(lines: list[list[Point]], start: Point | None, end: Point | None) -> list[Segment]:
    """The part of the street the user walks: between start and end, or around the one end that is known."""
    mid = lambda s: ((s[0][0] + s[1][0]) / 2, (s[0][1] + s[1][1]) / 2)  # noqa: E731
    segments = _segments(lines)
    if start and end:
        return [s for s in segments if -0.02 <= _t_and_distance(mid(s), start, end)[0] <= 1.02]
    if start or end:
        return [s for s in segments if geo.distance_m(mid(s), start or end) <= TURN_REACH_M]
    return segments


def _length_km(segments: list[Segment]) -> float:
    return sum(geo.distance_m(a, b) for a, b in segments) / 1000


# ---------- from places to a corridor ----------

def _resolve_anchor(conn, street: str, lines, anchor: Anchor) -> Point | None:
    if anchor.kind == "number":
        return geo.geocode(conn, f"{street} {anchor.value}")
    if anchor.kind == "cross_street":
        return junction(lines, geo.street_lines(conn, anchor.value))
    for query in (f"{anchor.value}, {street}", anchor.value):  # landmark: first try it on this street
        point = geo.geocode(conn, query)
        if point and distance_to_segments(point, _segments(lines)) <= LANDMARK_MAX_FROM_STREET_M:
            return point
    return None


def _locate_point(conn, text: str) -> Point | None:
    """An address or place; "X פינת Y" is the junction of the two streets (the map can't search a corner).
    If the streets don't meet, a point on the first street is better than nothing."""
    text = re.sub(r"^(רחוב|רח'|רח)\s+", "", text.strip())  # the map finds "הרב הירש", not "רחוב הרב הירש"
    if " פינת " not in text:
        return geo.geocode(conn, text)
    a, b = (s.strip() for s in text.split(" פינת ", 1))
    return junction(geo.street_lines(conn, a), geo.street_lines(conn, b)) or geo.geocode(conn, a)


def build(conn, places: list[Place]) -> tuple[dict, list[dict]]:
    """Returns (corridor, issues). Each issue is a question for the user about a place the map didn't find."""
    parts, spots, description, issues = [], [], [], []

    lines, points, street_points = {}, {}, {}
    for i, place in enumerate(places):
        if place.kind == "point":
            point = _locate_point(conn, place.text)
            if point:
                points[i] = point
                spots.append({"label": place.text, "point": point, "radius": POINT_RADIUS_M})
                description.append(place.text)
            else:
                issues.append({"place": i, "kind": "point",
                               "question": f"לא מצאתי במפה את '{place.text}' ב{CITY} (הסוכן עובד רק ב{CITY}). "
                                           "תוכל לכתוב רחוב ומספר בית?"})
        else:
            lines[i] = geo.street_lines(conn, place.text)
            if not lines[i]:
                guess = geo.similar_street(conn, place.text)
                question = (f"לא מצאתי במפה את רחוב '{place.text}'. התכוונת ל'{guess}'? כתוב 'כן', או את השם הנכון."
                            if guess else f"לא מצאתי במפה את רחוב '{place.text}' ב{CITY} (הסוכן עובד רק ב{CITY}). "
                                          "איך הוא נקרא?")
                issues.append({"place": i, "kind": "street", "question": question, "guess": guess})

    for i, place in enumerate(places):
        if not lines.get(i):
            continue
        ends = {}  # "start"/"end" -> (point, label)
        for which, neighbor in (("start", i - 1), ("end", i + 1)):
            anchor = getattr(place, which)
            if anchor:
                point = _resolve_anchor(conn, place.text, lines[i], anchor)
                if point:
                    ends[which] = (point, f"מספר {anchor.value}" if anchor.kind == "number" else anchor.value)
                else:
                    issues.append({"place": i, "kind": "anchor", "which": which,
                                   "question": f"לא מצאתי את '{anchor.value}' ברחוב {place.text}. בערך באיזה מספר בית?"})
            elif lines.get(neighbor):  # a turn: the end of this street is where it meets the next one
                point = junction(lines[i], lines[neighbor])
                if point:
                    ends[which] = (point, f"הצומת עם {places[neighbor].text}")
                elif which == "end":
                    issues.append({"place": i, "kind": "junction",
                                   "question": f"לא מצאתי צומת בין {place.text} ל{places[neighbor].text}. "
                                               f"דרך איזה רחוב אתה עובר ביניהם?"})

        segments = cut(lines[i], ends["start"][0] if "start" in ends else None,
                       ends["end"][0] if "end" in ends else None)
        parts.append({"label": place.text, "segments": segments})
        street_points[i] = [p for s in segments for p in s]
        spots += [{"label": label, "point": point, "radius": CORRIDOR_WIDTH_M} for point, label in ends.values()]
        span = "".join([f" מ{ends['start'][1]}" if "start" in ends else "",
                        f" עד {ends['end'][1]}" if "end" in ends else ""])
        description.append(f"{place.text}{span} (כ-{_length_km(segments):.1f} ק\"מ)")

    # Walking from a point to the next place (or from a street to a point): the stores along the straight
    # line between them are on the way too. Two streets one after the other already meet at their junction.
    for i in range(len(places) - 1):
        a, b = points.get(i), points.get(i + 1)
        if not (a or b):
            continue
        if not a and street_points.get(i):  # from the street's point that is closest to where the user goes next
            a = min(street_points[i], key=lambda p: geo.distance_m(p, b))
        if not b and street_points.get(i + 1):
            b = min(street_points[i + 1], key=lambda p: geo.distance_m(p, a))
        if a and b:
            parts.append({"label": f"הדרך מ{places[i].text} ל{places[i + 1].text}", "segments": [(a, b)]})

    return {"parts": parts, "spots": spots, "description": " → ".join(description)}, issues


def is_empty(corridor: dict) -> bool:
    return not corridor["spots"] and not any(p["segments"] for p in corridor["parts"])


def near(corridor: dict, point: Point) -> dict:
    """How far a store is from the corridor, from which piece, and whether it counts as 'on the way'."""
    options = [(distance_to_segments(point, p["segments"]), p["label"], CORRIDOR_WIDTH_M) for p in corridor["parts"]]
    options += [(geo.distance_m(point, s["point"]), s["label"], s["radius"]) for s in corridor["spots"]]
    inside = [o for o in options if o[0] <= o[2]]
    distance, label, _ = min(inside or options)
    return {"distance": round(distance), "from": label, "included": bool(inside)}


def sample_points(corridor: dict, step_m: float = 150) -> list[Point]:
    """Points along the corridor, at least step_m apart - for searching shops on the map around them."""
    points = [s["point"] for s in corridor["spots"]]
    for part in corridor["parts"]:
        for a, b in part["segments"]:
            for p in (a, b):
                if all(geo.distance_m(p, q) >= step_m for q in points):
                    points.append(p)
    return points


def warnings(places: list[dict], issues: list[dict]) -> list[str]:
    """What the answer tells the user about questions that stayed open. Only a place the map didn't find
    is left out; a missing junction or anchor still keeps the streets, only less precisely."""
    missed, notes = [], []
    for issue in issues:
        if "place" not in issue:
            continue
        place = places[issue["place"]]
        if issue["kind"] in ("point", "street"):
            missed.append(place["text"])
        elif issue["kind"] == "junction":
            notes.append(f"לא מצאתי צומת בין {place['text']} ל{places[issue['place'] + 1]['text']}, "
                         "ולכן כל אחד מהם נבדק לכל אורכו, בלי הדרך שביניהם.")
        elif issue["kind"] == "anchor":
            notes.append(f"לא מצאתי את '{place[issue['which']]['value']}' ברחוב {place['text']}, "
                         "ולכן החלק הזה של הדרך מחושב בקירוב.")
    if missed:
        notes.insert(0, f"לא נמצא במפה ולכן לא נכלל: {', '.join(dict.fromkeys(missed))}")
    return list(dict.fromkeys(notes))


def confirms_guess(issue: dict, text: str) -> bool:
    """The user answered "yes" to "did you mean <street>?"."""
    return bool(issue.get("guess")) and text.strip() in ("כן", "כ", "נכון", "yes")


def apply_fixes(places: list[Place], issues: list[dict], answers: list[dict]) -> list[Place]:
    """Apply the user's answers ({"issue": index, "text": ...}) to the places."""
    places = [p.model_copy() for p in places]
    inserts = []
    for answer in answers:
        issue, text = issues[answer["issue"]], answer["text"].strip()
        place = places[issue["place"]]
        if confirms_guess(issue, text):
            place.text = issue["guess"]
        elif issue["kind"] in ("point", "street"):
            if answer.get("place"):  # the answer as the model understood it (may turn a point into a street)
                places[issue["place"]] = answer["place"]
            else:
                place.text = text
        elif issue["kind"] == "anchor":
            setattr(place, issue["which"], Anchor(kind="number" if text.isdigit() else "landmark", value=text))
        elif issue["kind"] == "junction":  # the user named the street that connects the two
            inserts.append((issue["place"] + 1, Place(kind="street", text=text)))
    for position, place in sorted(inserts, key=lambda x: x[0], reverse=True):
        places.insert(position, place)
    return places

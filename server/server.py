"""A small web UI for the agent: the same graph as app.py, with a page in the browser instead of the terminal.
The page itself is the client: ../client/index.html.

    cd server
    ..\\.venv\\Scripts\\python.exe server.py      # then open http://127.0.0.1:8765

Only the standard library: one HTML page and two JSON endpoints that drive the LangGraph agent.
  POST /api/start   {"request": "..."}                 -> a step
  POST /api/resume  {"thread_id": "...", "resume": ...} -> a step
A step is {"type": "question", "payload": <the interrupt>} or {"type": "done", "result": {...}}.
"""
import json
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # the server folder: agent/, data/, config.py
sys.path.insert(0, str(ROOT))

from langgraph.types import Command  # noqa: E402

from agent import route  # noqa: E402
from agent.graph import build_graph, too_many_note  # noqa: E402
from config import MAX_BODY_BYTES, MAX_REQUEST_CHARS  # noqa: E402
from data import db  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
PAGE = ROOT.parent / "client" / "index.html"
GRAPH = build_graph()  # one graph; each conversation has its own thread_id in the checkpointer


# ---------- the agent ----------

def run(thread_id: str, graph_input) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    out = GRAPH.invoke(graph_input, config)
    if "__interrupt__" in out:
        return {"type": "question", "thread_id": thread_id, "payload": out["__interrupt__"][0].value}
    return {"type": "done", "thread_id": thread_id, "result": result(GRAPH.get_state(config).values)}


def result(state: dict) -> dict:
    """Everything the page shows, with map points and the price of each product in the chosen store."""
    conn = db.connect()
    locations = db.store_locations(conn)
    point = lambda key: locations.get(tuple(key)) if key else None  # noqa: E731

    best = state.get("best") or {}
    chosen = state["nearest"] if best and state.get("chosen") == "near" else best
    chosen_prices = {}
    if chosen.get("key"):
        chain, store_id = chosen["key"]
        chosen_prices = {r["item_code"]: r["price"] for r in conn.execute(
            "SELECT item_code, price FROM prices WHERE chain = ? AND store_id = ?", (chain, store_id))}

    items = []
    for m in state.get("matches", []):
        row = {"text": m["text"], "quantity": m.get("quantity", 1), "status": m["status"], "reason": m.get("reason", "")}
        if m["status"] == "match":
            code = chosen.get("used", {}).get(m["item_code"], m["item_code"])  # the alternative this store sells
            product = db.get_item(conn, code) or {}
            price = chosen_prices.get(code)
            row |= {"name": product.get("name"), "manufacturer": product.get("manufacturer"),
                    "price": price, "line_total": round(price * row["quantity"], 2) if price else None}
        items.append(row)

    stores = []
    for role, s in [("chosen", chosen)] + [("other", o) for o in ([best] if chosen is not best else []) + state.get("others", [])]:
        if s and s.get("label") and s["label"] not in [x["label"] for x in stores]:
            stores.append(s | {"role": role, "point": point(s.get("key"))})

    split = None
    if state.get("use_split") and state.get("split"):
        split = {"total": state["split"]["total"], "saving": state["split"]["saving"],
                 "parts": [{"store": p["store"], "items": [(db.get_item(conn, c) or {}).get("name", c) for c in p["items"]]}
                           for p in state["split"]["parts"]]}

    corridor = state.get("corridor")
    return {
        "description": corridor["description"] if corridor else None,
        "route_warnings": route.warnings(state.get("places", []), state.get("issues", [])),
        "too_many": too_many_note(state["too_many"]) if state.get("too_many") else None,
        "on_the_way": state.get("on_the_way", True),
        "items": items,
        "stores": stores,
        "split": split,
        # products the recommended store lacks: the closest store that sells them (not needed after a split)
        "elsewhere": [] if split else [e for e in state.get("elsewhere", []) if e["code"] in chosen.get("missing", [])],
        "other_shops": [{k: s.get(k) for k in ("name", "address", "type", "distance", "from", "note", "point")}
                        for s in state.get("other_shops", []) if s.get("name")],
        "map": {"parts": [p["segments"] for p in corridor["parts"]] if corridor else [],
                "spots": [{"label": s["label"], "point": s["point"], "radius": s["radius"]}
                          for s in corridor["spots"]] if corridor else []},
        "answer_text": state.get("answer", ""),
    }


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > MAX_BODY_BYTES:
                return self._json(413, {"error": "הבקשה גדולה מדי."})
            body = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/api/start":
                text = (body.get("request") or "").strip()
                if not text:
                    return self._json(400, {"error": "כתוב איפה אתה ומה צריך לקנות."})
                if len(text) > MAX_REQUEST_CHARS:
                    return self._json(400, {"error": f"ההודעה ארוכה מדי (עד {MAX_REQUEST_CHARS} תווים)."})
                return self._json(200, run(str(uuid.uuid4()), {"request": text}))
            if self.path == "/api/resume":
                return self._json(200, run(body["thread_id"], Command(resume=body["resume"])))
            self._json(404, {"error": "not found"})
        except Exception:  # noqa: BLE001 - a message on the page instead of a dead request
            traceback.print_exc()  # the details stay in the server's terminal, not in the browser
            self._json(500, {"error": "שגיאה פנימית. הפרטים נכתבו בטרמינל של השרת."})

    def _json(self, code: int, data: dict):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, kind: str):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet: only the API calls
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write(f"  {args[0]} -> {args[1]}\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"הממשק פועל: {url}   (Ctrl+C לעצירה)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nנעצר.")

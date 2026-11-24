"""Build the evaluation dataset by hand: for each query, you mark which products are correct
and whether the agent should ask the user. Then the dataset is uploaded to LangSmith.

    python -m eval.label           # label the queries in eval/queries.txt -> eval/dataset.json
    python -m eval.label upload    # upload eval/dataset.json to LangSmith
"""
import json
import re
import sys
from pathlib import Path

from config import DATASET_NAME
from data import db

HERE = Path(__file__).parent
DATASET_FILE = HERE / "dataset.json"

HELP = """   מספרים מופרדים בפסיק = המוצרים שנכונים לבקשה (למשל 1,3)
   a = עמום: הסוכן צריך לשאול (אחרי a אפשר להוסיף את המספרים הסבירים, למשל a 1,2,4)
   s <מילים> = חפש שוב במילים אחרות   |   x = דלג על השאילתה"""


def label() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = db.connect()
    done = json.loads(DATASET_FILE.read_text(encoding="utf-8")) if DATASET_FILE.exists() else []
    labeled = {ex["inputs"]["text"] for ex in done}
    print(HELP)

    for query in (HERE / "queries.txt").read_text(encoding="utf-8").splitlines():
        if not query.strip() or query in labeled:
            continue
        terms = [query]
        while True:
            candidates = db.search_items(conn, terms, 20)
            print(f"\n=== {query}")
            for n, c in enumerate(candidates, 1):
                print(f"   {n:2}. {c['name']} | {c['manufacturer']} | מ-{c['min_price']:.2f} ₪ | {c['n_stores']} חנויות")
            reply = input("   > ").strip()
            if reply.startswith("s "):
                terms = [reply[2:]]
                continue
            break
        if reply == "x":
            continue
        should_ask = reply.startswith("a")
        numbers = [int(n) for n in re.findall(r"\d+", reply)]
        acceptable = [candidates[n - 1]["item_code"] for n in numbers if 1 <= n <= len(candidates)]
        done.append({"inputs": {"text": query}, "outputs": {"acceptable": acceptable, "should_ask": should_ask}})
        DATASET_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSaved {len(done)} examples to {DATASET_FILE}")


def upload() -> None:
    from langsmith import Client

    client = Client()
    examples = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    if client.has_dataset(dataset_name=DATASET_NAME):
        sys.exit(f"Dataset '{DATASET_NAME}' already exists in LangSmith. Delete it there or rename DATASET_NAME.")
    client.create_dataset(DATASET_NAME, description="Shopping list item -> correct product / should ask")
    client.create_examples(dataset_name=DATASET_NAME, examples=examples)
    print(f"Uploaded {len(examples)} examples to LangSmith dataset '{DATASET_NAME}'")


if __name__ == "__main__":
    upload() if sys.argv[1:] == ["upload"] else label()

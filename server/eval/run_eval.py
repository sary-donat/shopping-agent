"""Run a LangSmith experiment on the matcher with one prompt version.

    python -m eval.run_eval v1    # before the improvement
    python -m eval.run_eval v2    # after the improvement

Then open the dataset in LangSmith and compare the two experiments side by side.
"""
import sys

from langsmith import Client

from agent.matcher import match_item, parse_list
from config import DATASET_NAME, MODEL
from data import db


def make_target(version: str):
    def target(inputs: dict) -> dict:
        conn = db.connect()
        item = parse_list(inputs["text"], version)[0]
        result = match_item(conn, item, version, use_memory=False)  # memory would hide mistakes
        return {"status": result["status"], "item_code": result["item_code"]}
    return target


# ----- evaluators: each one compares the agent's output to the hand-made label -----

def correct_decision(outputs: dict, reference_outputs: dict) -> bool:
    """Asked when it should ask, or picked a correct product when it should not."""
    if reference_outputs["should_ask"]:
        return outputs["status"] == "ambiguous"
    return outputs["status"] == "match" and outputs["item_code"] in reference_outputs["acceptable"]


def silent_wrong_pick(outputs: dict, reference_outputs: dict) -> bool:
    """The dangerous mistake: picked a product on its own, and it is not an acceptable one. Lower is better."""
    return outputs["status"] == "match" and outputs["item_code"] not in reference_outputs["acceptable"]


def unneeded_question(outputs: dict, reference_outputs: dict) -> bool:
    """The annoying mistake: asked although the answer was clear. Lower is better."""
    return outputs["status"] == "ambiguous" and not reference_outputs["should_ask"]


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "v2"
    Client().evaluate(
        make_target(version),
        data=DATASET_NAME,
        evaluators=[correct_decision, silent_wrong_pick, unneeded_question],
        experiment_prefix=f"matcher-{version}",
        metadata={"prompt_version": version, "model": MODEL},
        max_concurrency=4,
    )

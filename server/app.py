"""Run the agent in the terminal: where you live or your route + a shopping list; then answer its questions.

    python app.py
"""
import sys
import uuid

from langgraph.types import Command

from agent.graph import build_graph

sys.stdout.reconfigure(encoding="utf-8")


YES = {"y", "yes", "ט", "כ", "כן"}  # "ט" is the y key on a Hebrew keyboard
NO = {"n", "no", "מ", "ל", "לא"}    # "מ" is the n key on a Hebrew keyboard


def yes_no(prompt: str) -> bool:
    """y/n question that also accepts the same keys on a Hebrew keyboard, and asks again on anything else."""
    while True:
        reply = input(prompt).strip().lower()
        if reply in YES or reply in NO:
            return reply in YES
        print("   נא להקליד y (כן) או n (לא).")


def ask_products(questions: list[dict]) -> list[dict]:
    answers = []
    for q in questions:
        print(f"\n❓ {q['text']} - {q['reason']}")
        for n, opt in enumerate(q["options"], 1):
            print(f"   {n}. {opt['name']} | {opt['manufacturer']} | מ-{opt['min_price']:.2f} ₪")
        prompt = ("   מספר לבחירה / 0 לדלג / או הקלד ניסוח אחר: " if q["options"]
                  else "   הקלד ניסוח אחר, או Enter לדלג: ")
        while True:
            reply = input(prompt).strip()
            if not reply.isdigit() or reply == "0" or 1 <= int(reply) <= len(q["options"]):
                break
            print(f"   אין אפשרות {reply}. " + (f"בחר מספר בין 1 ל-{len(q['options'])}." if q["options"] else "אין כאן רשימה לבחירה."))
        if reply.isdigit() and 1 <= int(reply) <= len(q["options"]):
            remember = yes_no("   לזכור את הבחירה לפעם הבאה? (y/n): ")
            answers.append({"index": q["index"], "pick": q["options"][int(reply) - 1]["item_code"], "remember": remember})
        elif reply in ("", "0"):
            answers.append({"index": q["index"], "skip": True})
        else:
            answers.append({"index": q["index"], "retype": reply})
    return answers


def ask_split(payload: dict) -> bool:
    split = payload["split"]
    print(f"\n🛒 בחנות אחת: {payload['best']['label']} - {payload['best']['total']:.2f} ₪")
    print(f"   בשתי חנויות: {split['total']:.2f} ₪ (חיסכון {split['saving']:.2f} ₪)")
    for part in split["parts"]:
        print(f"   • {part['store']}: {len(part['items'])} מוצרים")
    return yes_no("   שווה לך עצירה נוספת? (y/n): ")


def ask_places(questions: list[dict]) -> list[dict]:
    answers = []
    for q in questions:
        print(f"\n📍 {q['question']}")
        reply = input("   תשובה (או Enter לדלג): ").strip()
        if reply:
            answers.append({"issue": q["issue"], "text": reply})
    if not answers:
        print("   ממשיך בלי מיקום מדויק.")
    return answers


def ask_cheap_vs_near(payload: dict) -> str:
    best, near = payload["best"], payload["nearest"]
    print(f"\n💰 הזול: {best['label']} - {best['total']:.2f} ₪, {best['distance']} מ' מ{best['from']}")
    print(f"🚶 הקרוב: {near['label']} - {near['total']:.2f} ₪, {near['distance']} מ' מ{near['from']}")
    return "cheap" if yes_no(f"   לחסוך {near['compare'] - best['compare']:.2f} ₪ וללכת לזול? (y = הזול / n = הקרוב): ") else "near"


HANDLERS = {
    "fix_places": lambda p: ask_places(p["questions"]),
    "choose_products": lambda p: ask_products(p["questions"]),
    "cheap_vs_near": ask_cheap_vs_near,
    "confirm_split": ask_split,
}


def main() -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    print("כתוב איפה אתה גר, או איפה אתה עובר היום, ומה צריך לקנות. לדוגמה:")
    print("  אני גר ברבי עקיבא 20. צריך חלב, גבינה לבנה, 2 לחם וביצים")
    print("  הולך בכהנמן מאזור מכבי עד ז'בוטינסקי. צריך חלב וקוטג'\n")
    request = input("> ")

    result = graph.invoke({"request": request}, config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        result = graph.invoke(Command(resume=HANDLERS[payload["type"]](payload)), config)

    print("\n" + result["answer"])


if __name__ == "__main__":
    main()

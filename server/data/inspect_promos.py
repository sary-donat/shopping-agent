"""Look inside the chains' promotion files before writing the code that uses them.
Downloads one PromoFull file per chain (a store in CITY if the database has one) and writes a short report.

    python -m data.inspect_promos

Then send promo_report.txt (it has no personal data, only the chains' public promotions).
"""
import asyncio
import re
import xml.etree.ElementTree as ET
from collections import Counter

from config import ROOT
from data import db
from data.prices import _fetch, _files, _local, _read_xml, chains

REPORT = ROOT / "promo_report.txt"
SAMPLES_PER_CHAIN = 2


def _flatten(el, prefix="") -> dict[str, str]:
    """All the text inside a promotion as {"path/to/field": value}; repeated item lists keep their first 3."""
    fields, seen = {}, Counter()
    for child in el:
        name = _local(child.tag)
        seen[name] += 1
        if seen[name] > 3:
            continue
        path = f"{prefix}{name}" + (f"[{seen[name]}]" if seen[name] > 1 else "")
        if len(child):
            fields |= _flatten(child, path + "/")
        elif (child.text or "").strip():
            fields[path] = child.text.strip()[:80]
    return fields


def _promotions(path) -> list[dict]:
    root = ET.fromstring(_read_xml(path))
    return [_flatten(el) for el in root.iter() if any(_local(c.tag) == "promotionid" for c in el)]


def inspect_chain(conn, chain: str, out: list[str]) -> None:
    row = conn.execute("SELECT store_id FROM stores WHERE chain = ? LIMIT 1", (chain,)).fetchone()
    store_id = row["store_id"] if row else None
    out.append(f"\n{'=' * 70}\n{chain} (store {store_id or 'any'})")
    try:
        asyncio.run(_fetch(chain, "PROMO_FULL_FILE", store_id))
    except Exception as e:  # noqa: BLE001 - one broken chain must not stop the others
        out.append(f"  download failed: {e}")
        return
    files = _files(chain, "promofull")
    if not files:
        out.append("  no promotion file was downloaded")
        return

    path = max(files, key=lambda p: p.stat().st_mtime)
    promos = _promotions(path)
    out.append(f"  file: {path.name} | promotions: {len(promos)}")
    if not promos:
        return

    # which fields exist, how often, and an example value
    plain = lambda field: re.sub(r"\[\d+\]", "", field)  # noqa: E731 - "item[2]/itemcode" -> "item/itemcode"
    counts = Counter(field for p in promos for field in {plain(f) for f in p})  # each field once per promotion
    examples = {}
    for p in promos:
        for field, value in p.items():
            examples.setdefault(plain(field), value)
    out.append("  fields (how many promotions have it | example):")
    for field, n in counts.most_common():
        out.append(f"    {field:<45} {100 * n // len(promos):>3}% | {examples[field]}")

    out.append("  sample promotions:")
    for p in promos[:SAMPLES_PER_CHAIN]:
        out.append("    ---")
        out += [f"    {field}: {value}" for field, value in p.items()]


def main() -> None:
    conn = db.connect()
    out = ["Promotion files report"]
    for chain in chains():
        print(f"### {chain}")
        inspect_chain(conn, chain, out)
    REPORT.write_text("\n".join(out), encoding="utf-8")
    print(f"\nReport saved to {REPORT}")


if __name__ == "__main__":
    main()

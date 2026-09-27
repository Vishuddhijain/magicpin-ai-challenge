#!/usr/bin/env python3
"""
Generate submission.jsonl (challenge-brief.md §7.2) from the 30 canonical
test pairs in expanded/test_pairs.json, calling compose() directly (no HTTP
needed for this — the judge's harness exercises the HTTP endpoints
separately via /v1/tick, but the JSONL deliverable is produced the same way
either path: same compose() function underneath).

Usage:
    python scripts/generate_submission.py --data expanded --out submission.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.compose import compose


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="expanded")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    data_dir = Path(args.data)
    pairs = load_json(data_dir / "test_pairs.json")["pairs"]

    categories = {f.stem: load_json(f) for f in (data_dir / "categories").glob("*.json")}
    merchants = {load_json(f)["merchant_id"]: load_json(f) for f in (data_dir / "merchants").glob("*.json")}
    triggers = {load_json(f)["id"]: load_json(f) for f in (data_dir / "triggers").glob("*.json")}
    customers = {load_json(f)["customer_id"]: load_json(f) for f in (data_dir / "customers").glob("*.json")}

    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            test_id = pair["test_id"]
            merchant = merchants[pair["merchant_id"]]
            trigger = triggers[pair["trigger_id"]]
            customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
            category = categories[merchant["category_slug"]]

            result = compose(category, merchant, trigger, customer)
            line = {"test_id": test_id, **result}
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(pairs)} lines to {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Push the full expanded dataset (categories, merchants, customers) into a
running bot instance via POST /v1/context — mirrors the judge's Phase-1
warmup exactly (testing-brief.md §4, Phase 1).

Usage:
    python scripts/push_context.py --bot-url http://localhost:8080 --data expanded
"""
import argparse
import json
import sys
from pathlib import Path
from urllib import request as urlrequest


def post(bot_url: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(bot_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-url", default="http://localhost:8080")
    ap.add_argument("--data", default="expanded")
    args = ap.parse_args()

    data_dir = Path(args.data)
    delivered_at = "2026-04-26T10:00:00Z"

    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}

    for f in sorted((data_dir / "categories").glob("*.json")):
        payload = json.loads(f.read_text())
        r = post(args.bot_url, "/v1/context", {
            "scope": "category", "context_id": payload["slug"], "version": 1,
            "payload": payload, "delivered_at": delivered_at,
        })
        assert r.get("accepted"), r
        counts["category"] += 1

    for f in sorted((data_dir / "merchants").glob("*.json")):
        payload = json.loads(f.read_text())
        r = post(args.bot_url, "/v1/context", {
            "scope": "merchant", "context_id": payload["merchant_id"], "version": 1,
            "payload": payload, "delivered_at": delivered_at,
        })
        assert r.get("accepted"), r
        counts["merchant"] += 1

    for f in sorted((data_dir / "customers").glob("*.json")):
        payload = json.loads(f.read_text())
        r = post(args.bot_url, "/v1/context", {
            "scope": "customer", "context_id": payload["customer_id"], "version": 1,
            "payload": payload, "delivered_at": delivered_at,
        })
        assert r.get("accepted"), r
        counts["customer"] += 1

    print(f"Pushed: {counts}")


if __name__ == "__main__":
    main()

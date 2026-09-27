"""
Minimal smoke tests for the composer + reply handler.
Run with: python -m pytest tests/ -q   (or: python tests/test_compose.py)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.compose import compose, handle_reply

DATA = Path(__file__).resolve().parent.parent / "expanded"


def _load(scope, name):
    return json.loads((DATA / scope / f"{name}.json").read_text())


def test_determinism():
    category = _load("categories", "dentists")
    merchant = _load("merchants", "m_001_drmeera_dentist_delhi")
    triggers = json.loads((DATA.parent / "dataset" / "triggers_seed.json").read_text())["triggers"]
    trigger = next(t for t in triggers if t["id"] == "trg_001_research_digest_dentists")
    r1 = compose(category, merchant, trigger, None)
    r2 = compose(category, merchant, trigger, None)
    assert r1 == r2


def test_required_keys_present():
    category = _load("categories", "dentists")
    merchant = _load("merchants", "m_001_drmeera_dentist_delhi")
    triggers = json.loads((DATA.parent / "dataset" / "triggers_seed.json").read_text())["triggers"]
    trigger = next(t for t in triggers if t["id"] == "trg_001_research_digest_dentists")
    r = compose(category, merchant, trigger, None)
    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        assert key in r and r[key]


def test_customer_facing_send_as():
    category = _load("categories", "dentists")
    merchant = _load("merchants", "m_001_drmeera_dentist_delhi")
    customer = _load("customers", "c_001_priya_for_m001")
    triggers = json.loads((DATA.parent / "dataset" / "triggers_seed.json").read_text())["triggers"]
    trigger = next(t for t in triggers if t["kind"] == "recall_due")
    r = compose(category, merchant, trigger, customer)
    assert r["send_as"] == "merchant_on_behalf"


def test_auto_reply_detection_then_exit():
    history = [{"from": "vera", "msg": "hello"}]
    canned = "Thank you for contacting us, our team will get back to you"
    out1 = handle_reply(history, canned)
    assert out1["action"] == "send"  # first canned reply: try once
    history.append({"from": "merchant", "msg": canned})
    history.append({"from": "vera", "msg": out1["body"]})
    out2 = handle_reply(history, canned)
    assert out2["action"] == "end"  # second canned reply: exit gracefully


def test_intent_transition_routes_to_action():
    out = handle_reply([], "ok let's do it, go ahead")
    assert out["action"] == "send"
    assert "?" not in out["body"]  # should not be another qualifying question


def test_not_interested_ends():
    out = handle_reply([], "not interested, please stop")
    assert out["action"] == "end"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
    print("\nAll smoke tests passed.")

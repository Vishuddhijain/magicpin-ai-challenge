"""
Tests for fixes #7-#10 (auto-reply fuzzy detection, explicit-intent action mode
without false completion claims, anti-repetition single-CTA guarantee, and the
/v1/context contract) plus coverage across every trigger-kind family in the
dataset.

Run with: python -m pytest tests/test_fixes.py -v
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compose import compose, handle_reply, looks_like_canned_reply
import app.bot as botmod
from app.bot import app

DATA = Path(__file__).resolve().parent.parent / "expanded"
client = TestClient(app)


def _load(scope, name):
    return json.loads((DATA / scope / f"{name}.json").read_text())


def _reset_bot_state():
    """Wipe bot.py's module-level in-memory state between tests that hit the
    HTTP layer, so tests don't leak conversation/context state into each other."""
    r = client.post("/v1/teardown")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Fix #7 — auto-reply detection beyond exact string equality
# ---------------------------------------------------------------------------

def test_two_different_canned_messages_both_recognized_and_end_conversation():
    """A different-worded canned/autoresponder message the SECOND time around
    must still be recognized as a repeat auto-reply, not treated as a fresh
    human reply just because the exact wording differs."""
    history = [{"from": "vera", "msg": "hello"}]
    state = {}  # a single shared, mutated-in-place state, exactly as bot.py does
    canned_1 = "Thank you for contacting us, our team will get back to you"
    out1 = handle_reply(history, canned_1, state=state)
    assert out1["action"] == "send"  # first canned reply: try once

    history.append({"from": "merchant", "msg": canned_1})
    history.append({"from": "vera", "msg": out1["body"]})

    canned_2 = "Hi, we've received your query and will respond within 24 hours"
    out2 = handle_reply(history, canned_2, state=state)
    assert out2["action"] == "end", "a second, differently-worded canned reply must still end the conversation"


def test_canned_detection_not_reliant_on_exact_equality():
    """Regression guard: two canned texts that are NOT byte-identical must
    both be classified as canned via looks_like_canned_reply."""
    a = "Shukriya! Aapka message mil gaya hai, team jald sampark karegi."
    b = "Dhanyavad, humein aapka message mila hai. Hamari team jald hi sampark karenge."
    assert a != b
    assert looks_like_canned_reply(a, [])
    assert looks_like_canned_reply(b, [a])


def test_canned_marker_scoring_catches_untemplated_boilerplate():
    """A canned-sounding message that matches NONE of the fixed
    AUTO_REPLY_PATTERNS substrings verbatim should still be caught by the
    generic marker-scoring path."""
    text = "Your ticket has been logged, reference number assigned, our support team will respond shortly."
    assert looks_like_canned_reply(text, [])


def test_genuine_human_reply_not_flagged_as_canned():
    text = "Can you tell me more about the pricing for this offer?"
    assert not looks_like_canned_reply(text, [])


def test_single_canned_reply_does_not_end_immediately():
    out = handle_reply([], "Thank you for contacting us, our team will get back to you")
    assert out["action"] == "send"


# ---------------------------------------------------------------------------
# Fix #8 — explicit intent -> immediate action mode, no false completion claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["yes", "Yes!", "go ahead", "let's do it", "haan chalo", "haan", "ok karo"])
def test_explicit_intent_enters_action_mode_immediately(message):
    out = handle_reply([], message)
    assert out["action"] == "send"
    assert "?" not in out["body"], "action mode must not ask another qualifying question"


FORBIDDEN_COMPLETION_CLAIMS = ["done —", "done -", "i've done", "already done", "has been sent", "is now live", "it's live"]


@pytest.mark.parametrize("message", ["yes", "go ahead", "let's do it", "haan chalo"])
def test_intent_response_never_claims_action_already_happened(message):
    out = handle_reply([], message)
    body_lower = out["body"].lower()
    for phrase in FORBIDDEN_COMPLETION_CLAIMS:
        assert phrase not in body_lower, f"body falsely implies completion: {out['body']!r}"


def test_intent_state_recorded_as_confirmed():
    state = {}
    handle_reply([], "let's do it", state=state)
    assert state["intent_state"] == "confirmed"


# ---------------------------------------------------------------------------
# Fix #9 — anti-repetition fallback never creates a second CTA
# ---------------------------------------------------------------------------

def test_anti_repetition_no_second_cta_when_original_has_one():
    _reset_bot_state()
    conv_id = "conv_anti_rep_test_1"
    body = {
        "conversation_id": conv_id, "merchant_id": "m_001", "customer_id": None,
        "from_role": "merchant", "message": "not sure, tell me more",
        "received_at": "2026-04-26T10:00:00Z", "turn_number": 1,
    }
    r1 = client.post("/v1/reply", json=body)
    assert r1.status_code == 200
    first_body = r1.json()["body"]
    assert first_body.count("?") <= 1

    # Send the exact same merchant message again -> forces the anti-repetition
    # fallback path (the default "no signal detected" response is otherwise
    # identical every time for the same conversation_meta/anchor state).
    body["turn_number"] = 2
    r2 = client.post("/v1/reply", json=body)
    second_body = r2.json()["body"]
    assert second_body.count("?") <= 1, f"fallback introduced a second CTA: {second_body!r}"


def test_anti_repetition_appends_single_closer_when_original_has_no_cta():
    _reset_bot_state()
    conv_id = "conv_anti_rep_test_2"
    # First message: bare "yes" -> action-mode body with cta=none, no "?".
    body = {
        "conversation_id": conv_id, "merchant_id": "m_002", "customer_id": None,
        "from_role": "merchant", "message": "not sure yet",
        "received_at": "2026-04-26T10:00:00Z", "turn_number": 1,
    }
    r1 = client.post("/v1/reply", json=body)
    first_body = r1.json()["body"]
    body["turn_number"] = 2
    r2 = client.post("/v1/reply", json=body)
    second_body = r2.json()["body"]
    assert second_body != first_body
    assert second_body.count("?") <= 1


# ---------------------------------------------------------------------------
# Fix #10 — /v1/context matches the testing brief exactly
# ---------------------------------------------------------------------------

def test_context_same_version_is_noop_200():
    _reset_bot_state()
    push = {"scope": "merchant", "context_id": "m_test_ctx", "version": 3,
            "payload": {"a": 1}, "delivered_at": "2026-04-26T10:00:00Z"}
    r1 = client.post("/v1/context", json=push)
    assert r1.status_code == 200
    r2 = client.post("/v1/context", json=push)
    assert r2.status_code == 200
    assert r2.json()["accepted"] is True


def test_context_lower_version_returns_409():
    _reset_bot_state()
    base = {"scope": "merchant", "context_id": "m_test_ctx2",
            "payload": {"a": 1}, "delivered_at": "2026-04-26T10:00:00Z"}
    client.post("/v1/context", json={**base, "version": 5})
    r = client.post("/v1/context", json={**base, "version": 2})
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "stale_version"
    assert body["current_version"] == 5


def test_context_higher_version_replaces_payload():
    _reset_bot_state()
    base = {"scope": "merchant", "context_id": "m_test_ctx3", "delivered_at": "2026-04-26T10:00:00Z"}
    client.post("/v1/context", json={**base, "version": 1, "payload": {"name": "old"}})
    r = client.post("/v1/context", json={**base, "version": 2, "payload": {"name": "new"}})
    assert r.status_code == 200
    assert botmod.contexts[("merchant", "m_test_ctx3")]["payload"]["name"] == "new"


def test_context_invalid_scope_returns_400():
    _reset_bot_state()
    r = client.post("/v1/context", json={
        "scope": "not_a_real_scope", "context_id": "x", "version": 1,
        "payload": {}, "delivered_at": "2026-04-26T10:00:00Z",
    })
    assert r.status_code == 400
    assert r.json()["reason"] == "invalid_scope"


def test_context_missing_field_returns_400():
    _reset_bot_state()
    r = client.post("/v1/context", json={
        "scope": "merchant", "context_id": "x", "payload": {},
        "delivered_at": "2026-04-26T10:00:00Z",
        # "version" missing entirely
    })
    assert r.status_code == 400


def test_context_malformed_payload_type_returns_400():
    _reset_bot_state()
    r = client.post("/v1/context", json={
        "scope": "merchant", "context_id": "x", "version": 1,
        "payload": "not-an-object", "delivered_at": "2026-04-26T10:00:00Z",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Coverage across every trigger-kind family in the dataset
# ---------------------------------------------------------------------------

KIND_FIXTURES = {
    "active_planning_intent":   ("trg_013_corporate_thali_planning", "m_006_southindiancafe_restaurant_bangalore", None),
    "appointment_tomorrow":     ("trg_076_appointment_tomorrow_m_019_karim_salon_lu", "m_019_karim_salon_lucknow", "c_075_aditya_for_m_019_karim_salon_lucknow"),
    "category_seasonal":        ("trg_020_summer_demand_shift", "m_009_apollo_pharmacy_jaipur", None),
    "cde_opportunity":          ("trg_022_cde_webinar_dentists", "m_001_drmeera_dentist_delhi", None),
    "chronic_refill_due":       ("trg_019_chronic_refill_grandfather", "m_009_apollo_pharmacy_jaipur", "c_013_grandfather_for_m009"),
    "competitor_opened":        ("trg_023_competitor_opened_dentist", "m_001_drmeera_dentist_delhi", None),
    "curious_ask_due":          ("trg_008_curious_ask_studio11", "m_003_studio11_salon_hyderabad", None),
    "customer_lapsed_hard":     ("trg_015_winback_rashmi", "m_007_powerhouse_gym_bangalore", "c_010_rashmi_for_m007"),
    "customer_lapsed_soft":     ("trg_071_customer_lapsed_soft_m_014_dr_asha_dentis", "m_014_dr_asha_dentist_chandigarh", "c_055_reyansh_for_m_014_dr_asha_dentist_chandigarh"),
    "dormant_with_vera":        ("trg_025_dormancy_glamour", "m_004_glamour_salon_pune", None),
    "festival_upcoming":        ("trg_006_festival_diwali", "m_003_studio11_salon_hyderabad", None),
    "gbp_unverified":           ("trg_021_unverified_gbp_sunrise", "m_010_sunrisepharm_pharmacy_lucknow", None),
    "ipl_match_today":          ("trg_010_ipl_match_delhi", "m_005_pizzajunction_restaurant_delhi", None),
    "milestone_reached":        ("trg_012_milestone_mylari", "m_006_southindiancafe_restaurant_bangalore", None),
    "perf_dip":                 ("trg_004_perf_dip_bharat", "m_002_bharat_dentist_mumbai", None),
    "perf_spike":               ("trg_024_perf_spike_zen", "m_008_zenyoga_gym_chennai", None),
    "recall_due":               ("trg_003_recall_due_priya", "m_001_drmeera_dentist_delhi", "c_001_priya_for_m001"),
    "regulation_change":        ("trg_002_compliance_dci_radiograph", "m_001_drmeera_dentist_delhi", None),
    "renewal_due":              ("trg_005_renewal_due_bharat", "m_002_bharat_dentist_mumbai", None),
    "research_digest":          ("trg_001_research_digest_dentists", "m_001_drmeera_dentist_delhi", None),
    "review_theme_emerged":     ("trg_011_review_theme_late_delivery", "m_005_pizzajunction_restaurant_delhi", None),
    "seasonal_perf_dip":        ("trg_014_seasonal_acquisition_dip_powerhouse", "m_007_powerhouse_gym_bangalore", None),
    "supply_alert":             ("trg_018_supply_atorvastatin_recall", "m_009_apollo_pharmacy_jaipur", None),
    "trial_followup":           ("trg_017_kids_yoga_trial_followup_karthik", "m_008_zenyoga_gym_chennai", "c_012_karthik_jr_for_m008"),
    "wedding_package_followup": ("trg_007_bridal_followup_kavya", "m_003_studio11_salon_hyderabad", "c_005_kavya_for_m003"),
    "winback_eligible":         ("trg_009_winback_glamour", "m_004_glamour_salon_pune", None),
}

CATEGORY_SLUG_BY_MERCHANT = {}
for _f in (DATA / "merchants").glob("*.json"):
    _m = json.loads(_f.read_text())
    CATEGORY_SLUG_BY_MERCHANT[_m["merchant_id"]] = _m["category_slug"]


@pytest.mark.parametrize("kind", sorted(KIND_FIXTURES.keys()))
def test_compose_works_and_is_deterministic_for_every_trigger_kind(kind):
    trigger_id, merchant_id, customer_id = KIND_FIXTURES[kind]
    trigger = _load("triggers", trigger_id)
    merchant = _load("merchants", merchant_id)
    category = _load("categories", CATEGORY_SLUG_BY_MERCHANT[merchant_id])
    customer = _load("customers", customer_id) if customer_id else None

    r1 = compose(category, merchant, trigger, customer)
    r2 = compose(category, merchant, trigger, customer)
    assert r1 == r2, f"compose() not deterministic for kind={kind}"

    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        assert key in r1 and r1[key], f"missing/empty '{key}' for kind={kind}"
    assert len(r1["body"]) > 10, f"suspiciously short body for kind={kind}: {r1['body']!r}"


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"])

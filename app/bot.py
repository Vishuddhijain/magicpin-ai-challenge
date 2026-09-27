"""
bot.py — HTTP server for the magicpin AI Challenge.

Implements the 5 endpoints from challenge-testing-brief.md §2:
    POST /v1/context
    POST /v1/tick
    POST /v1/reply
    GET  /v1/healthz
    GET  /v1/metadata
    POST /v1/teardown   (optional, per §11 of the testing brief)

Run locally:
    uvicorn app.bot:app --host 0.0.0.0 --port 8080 --reload

State is in-memory (fine per the brief — "storing in memory is fine, just
don't restart between calls"). Everything the judge pushes via /v1/context
is kept in `contexts`, keyed by (scope, context_id) -> {version, payload}.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.compose import compose, handle_reply, evaluate_trigger_candidate, rank_trigger_candidates, active_offer

VALID_CONTEXT_SCOPES = {"category", "merchant", "customer", "trigger"}

app = FastAPI(title="Vera Challenge Bot")
START_TIME = time.time()

# Soft anti-spam cap: at most this many proactive sends to one merchant per
# tick, even if more of their triggers rank highly. Restraint is rewarded
# (testing-brief §14 FAQ); flooding one merchant with 5 messages in the same
# 5-minute tick is not the win a high rank score might suggest.
PER_MERCHANT_TICK_CAP = 3

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
contexts: dict[tuple[str, str], dict] = {}          # (scope, context_id) -> {version, payload}
conversations: dict[str, list[dict]] = {}           # conversation_id -> [{"from","msg"}]
conversation_meta: dict[str, dict] = {}             # conversation_id -> {"merchant_id","customer_id"}
sent_suppression_keys: dict[str, set[str]] = {}     # merchant_id -> set of suppression_keys already sent
sent_bodies: dict[str, set[str]] = {}               # conversation_id -> set of bodies already sent (anti-repetition)


def _get(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vishuddhi Jain",
        "team_members": ["Vishuddhi Jain"],
        "model": "rule-based-deterministic-composer-v1 (no external LLM call required)",
        "approach": (
            "Deterministic template composer: extracts the single most specific, verifiable "
            "fact available across category/merchant/trigger/customer contexts, frames it per "
            "trigger-kind angle, matches category voice + language (hi-en code-mix when merchant/"
            "customer prefers it), and attaches one binary or open-ended CTA. /v1/tick ranks all "
            "eligible triggers (urgency, payload groundedness, merchant-signal strength, customer "
            "relevance, actionability, category fit) rather than sending in input order, after "
            "hard-gating on expiry, customer consent scope, dedup, and perf-direction contradiction "
            "checks. Conversation replies are handled with pattern-based auto-reply detection, "
            "intent-transition routing, graceful exit on opt-out, and remembered per-conversation "
            "state (original trigger/offer/category) so follow-ups stay context-specific."
        ),
        "contact_email": "vishuddhi0303.jain@gmail.com",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

@app.post("/v1/context")
async def push_context(request: Request):
    # Fix #10 — match challenge-testing-brief.md §2.1 exactly:
    #   same version           -> 200 accepted / no-op
    #   lower version          -> 409 {"accepted": false, "reason": "stale_version", ...}
    #   malformed scope/request -> 400 {"accepted": false, "reason": "invalid_scope"/"malformed_request", ...}
    # We parse the body manually (rather than relying on FastAPI's default
    # 422 for a Pydantic-model mismatch) so malformed input reaches the
    # judge as the 400 the brief specifies, not a 422.
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "body is not valid JSON"})

    if not isinstance(raw, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "body must be a JSON object"})

    scope = raw.get("scope")
    context_id = raw.get("context_id")
    version = raw.get("version")
    payload = raw.get("payload")
    delivered_at = raw.get("delivered_at")

    if scope not in VALID_CONTEXT_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope",
            "details": f"scope must be one of {sorted(VALID_CONTEXT_SCOPES)}, got {scope!r}",
        })
    if not isinstance(context_id, str) or not context_id:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "context_id must be a non-empty string"})
    if not isinstance(version, int) or isinstance(version, bool):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "version must be an integer"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "payload must be an object"})
    if not isinstance(delivered_at, str) or not delivered_at:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": "delivered_at must be a non-empty string"})

    key = (scope, context_id)
    cur = contexts.get(key)
    if cur and cur["version"] > version:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]})
    if cur and cur["version"] == version:
        # Re-posting the same version is a no-op per the brief.
        return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": _now_iso()}
    contexts[key] = {"version": version, "payload": payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{context_id}_v{version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    # -- Pass 1: gather + hard-gate every available trigger, don't just take
    #    the first 20 in whatever order the judge listed them (Fix #1). --
    candidates: list[dict] = []
    for trg_id in body.available_triggers:
        trigger = _get("trigger", trg_id)
        if not trigger:
            continue
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        merchant = _get("merchant", merchant_id)
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug"))
        if not category:
            continue
        customer = _get("customer", customer_id) if customer_id else None

        supp_seen = sent_suppression_keys.setdefault(merchant_id, set())
        evaluation = evaluate_trigger_candidate(trigger, merchant, category, customer, body.now, supp_seen)
        if not evaluation["eligible"]:
            # Rejected here covers: expired (Fix #2), consent doesn't cover
            # the outreach or missing customer context for a customer-scope
            # trigger (Fix #3), already-sent suppression_key, and a
            # perf_spike/perf_dip/seasonal_perf_dip whose direction is
            # contradicted by the merchant's actual measured metric (Fix #5).
            continue

        candidates.append({
            "trigger_id": trg_id,
            "trigger": trigger,
            "merchant": merchant,
            "category": category,
            "customer": customer,
            "eval": evaluation,
        })

    # -- Pass 2: rank by score (urgency, payload groundedness, merchant
    #    signal strength, customer relevance, actionability, category fit)
    #    and only then compose + send, capped at 20 actions and a per-
    #    merchant anti-spam ceiling. --
    ranked = rank_trigger_candidates(candidates)

    actions = []
    per_merchant_sent: dict[str, int] = {}
    for cand in ranked:
        if len(actions) >= 20:
            break

        trigger, merchant, category, customer = cand["trigger"], cand["merchant"], cand["category"], cand["customer"]
        trg_id = cand["trigger_id"]
        merchant_id = merchant.get("merchant_id") or trigger.get("merchant_id")
        customer_id = customer.get("customer_id") if customer else trigger.get("customer_id")

        if per_merchant_sent.get(merchant_id, 0) >= PER_MERCHANT_TICK_CAP:
            continue

        result = compose(category, merchant, trigger, customer)

        # Final dedup check: compose() may derive its own suppression_key
        # (fallback path) that differs from the trigger's own, so re-check
        # against what's actually about to be marked as sent.
        supp_key_seen = sent_suppression_keys.setdefault(merchant_id, set())
        if result["suppression_key"] in supp_key_seen:
            continue
        supp_key_seen.add(result["suppression_key"])

        conversation_id = f"conv_{merchant_id}_{trg_id}"
        conversations[conversation_id] = [{"from": result["send_as"], "msg": result["body"]}]
        # Fix #6 — remember everything a context-specific reply will need:
        # the original trigger, the offer compose() had available, category,
        # and the conversation's evolving intent/auto-reply/opt-out state.
        conversation_meta[conversation_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "trigger": trigger,
            "category_slug": merchant.get("category_slug"),
            "original_body": result["body"],
            "selected_offer": active_offer(merchant),
            "auto_reply_count": 0,
            "intent_state": "none",
            "opt_out": False,
        }
        sent_bodies.setdefault(conversation_id, set()).add(result["body"])
        per_merchant_sent[merchant_id] = per_merchant_sent.get(merchant_id, 0) + 1

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", "")],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": (
                result["rationale"] + " [tick ranking: score="
                f"{cand['eval']['score']:.2f} among {len(ranked)} eligible candidate(s) this tick]"
            ),
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    history = conversations.setdefault(body.conversation_id, [])
    # Fix #6 — pull (or lazily create) this conversation's remembered state.
    # conversation_meta already holds it if this conversation started from a
    # /v1/tick action; if the judge references a conversation_id we don't
    # recognize, fall back to a minimal state built from what the reply
    # itself tells us, rather than failing.
    meta = conversation_meta.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id,
        "customer_id": body.customer_id,
        "trigger_id": None,
        "trigger": {},
        "category_slug": None,
        "original_body": None,
        "selected_offer": None,
        "auto_reply_count": 0,
        "intent_state": "none",
        "opt_out": False,
    })

    result = handle_reply(history, body.message, meta)

    # record incoming turn
    history.append({"from": body.from_role, "msg": body.message})

    if result["action"] == "send":
        seen = sent_bodies.setdefault(body.conversation_id, set())
        candidate = result["body"]
        if candidate in seen:
            # Anti-repetition: never resend a verbatim body in the same
            # conversation. Fix #9 — the fallback must not add a SECOND CTA
            # on top of one the original body already carries (violates the
            # "one clear CTA per send" rule). If the body already ends in a
            # question (its CTA), only vary the lead-in, don't append another
            # question. Only bodies with no question at all (a pure
            # statement, e.g. the action-mode "no more questions" message)
            # get a single new closing question appended.
            stripped = candidate.rstrip()
            if stripped.endswith("?"):
                candidate = "Just checking back in — " + stripped[0].lower() + stripped[1:]
            else:
                candidate = stripped.rstrip(".") + " — let me know if you'd like anything changed."
        seen.add(candidate)
        history.append({"from": "vera", "msg": candidate})
        return {
            "action": "send",
            "body": candidate,
            "cta": result.get("cta", "open_ended"),
            "rationale": result["rationale"],
        }
    if result["action"] == "wait":
        return {
            "action": "wait",
            "wait_seconds": result.get("wait_seconds", 1800),
            "rationale": result["rationale"],
        }
    # "end"
    return {"action": "end", "rationale": result["rationale"]}


# ---------------------------------------------------------------------------
# POST /v1/teardown (optional, testing-brief §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    conversation_meta.clear()
    sent_suppression_keys.clear()
    sent_bodies.clear()
    return {"status": "wiped"}

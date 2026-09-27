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

from fastapi import FastAPI
from pydantic import BaseModel

from app.compose import compose, handle_reply

app = FastAPI(title="Vera Challenge Bot")
START_TIME = time.time()

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
            "customer prefers it), and attaches one binary or open-ended CTA. Conversation replies "
            "are handled with pattern-based auto-reply detection, intent-transition routing, and "
            "graceful exit on opt-out."
        ),
        "contact_email": "vishuddhi0303.jain@gmail.com",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] > body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    if cur and cur["version"] == body.version:
        # Re-posting the same version is a no-op per the brief.
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
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
    actions = []
    for trg_id in body.available_triggers[:20]:  # respect the 20-actions/tick cap
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

        supp_key_seen = sent_suppression_keys.setdefault(merchant_id, set())
        result = compose(category, merchant, trigger, customer)

        # dedup: don't resend the same suppression_key to the same merchant twice
        if result["suppression_key"] in supp_key_seen:
            continue
        supp_key_seen.add(result["suppression_key"])

        conversation_id = f"conv_{merchant_id}_{trg_id}"
        conversations[conversation_id] = [{"from": result["send_as"], "msg": result["body"]}]
        conversation_meta[conversation_id] = {"merchant_id": merchant_id, "customer_id": customer_id}
        sent_bodies.setdefault(conversation_id, set()).add(result["body"])

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
            "rationale": result["rationale"],
        })

        if len(actions) >= 20:
            break

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
    result = handle_reply(history, body.message)

    # record incoming turn
    history.append({"from": body.from_role, "msg": body.message})

    if result["action"] == "send":
        seen = sent_bodies.setdefault(body.conversation_id, set())
        candidate = result["body"]
        if candidate in seen:
            # anti-repetition: never resend a verbatim body in the same conversation
            candidate = candidate.rstrip(".") + " — anything else I can help clarify?"
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

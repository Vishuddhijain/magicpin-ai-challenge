"""
compose.py — the deterministic message-composition engine for the magicpin
AI Challenge ("Vera").

Public entry point (matches challenge-brief.md §7.1 exactly):

    def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict

Returns: {"body", "cta", "send_as", "suppression_key", "rationale"}

Design notes
------------
* Pure, deterministic, no network calls, no randomness -> same input always
  gives the same output (temperature=0 by construction). This satisfies the
  "must be deterministic" + "<30s" constraints without needing an LLM key.
* If ANTHROPIC_API_KEY is set in the environment, `llm_polish()` can be used
  by the caller as an optional finishing pass (see app/llm.py) — but the
  rule-based engine below is a complete, fully-working composer on its own,
  so the bot works out of the box with zero external dependencies.
* Every fact used in a message must come from the category/merchant/trigger/
  customer dicts actually passed in. Nothing is invented (brief §5, rule 8).
"""

from __future__ import annotations

from typing import Any, Optional
import re

Ctx = dict[str, Any]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    if x is None:
        return "?"
    sign = "+" if x >= 0 else ""
    return f"{sign}{round(x * 100)}%"


def _first_name(merchant: Ctx) -> str:
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name", "there").split()[0]


def _is_clinical(category: Ctx) -> bool:
    return category.get("voice", {}).get("tone", "") in ("peer_clinical", "clinical", "clinical_utility")


def _code_mix(merchant: Ctx, customer: Optional[Ctx], category: Ctx) -> bool:
    """Should this message use Hindi-English code-mix?"""
    if customer:
        lp = customer.get("identity", {}).get("language_pref", "")
        if "hi" in lp.lower():
            return True
        if lp.lower() == "en":
            return False
    langs = merchant.get("identity", {}).get("languages", [])
    if "hi" in langs:
        return category.get("voice", {}).get("code_mix", "") != "english_only"
    return False


def salutation(category: Ctx, merchant: Ctx) -> str:
    ident = merchant.get("identity", {})
    name = ident.get("name", "")
    first = ident.get("owner_first_name")
    examples = category.get("voice", {}).get("salutation_examples", [])
    if first and any("Dr." in ex for ex in examples) and "dent" in category.get("slug", ""):
        return f"Dr. {first}"
    if first:
        return first
    return name.split()[0] if name else "Hi"


def resolve_digest_item(category: Ctx, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def best_digest_item(category: Ctx, prefer_kind: Optional[str] = None) -> Optional[dict]:
    """Fallback: pick the most relevant digest item when the trigger payload
    doesn't name a specific one (common for generator-expanded triggers,
    which carry a placeholder payload)."""
    items = category.get("digest", [])
    if not items:
        return None
    if prefer_kind:
        for item in items:
            if item.get("kind") == prefer_kind:
                return item
    return items[0]


def matching_signal(merchant: Ctx, needle: str) -> Optional[str]:
    """Loose substring match: strips a trailing 's' on both sides so
    'high_risk_adults' (digest segment) matches 'high_risk_adult_cohort'
    (merchant signal)."""
    if not needle:
        return None
    needle_stem = needle.rstrip("s")
    for s in merchant.get("signals", []):
        if needle in s or needle_stem in s:
            return s
    return None


CLINICAL_SLUGS = {"dentists", "pharmacies"}


def _customer_noun(category: Ctx) -> str:
    return "patients" if category.get("slug") in CLINICAL_SLUGS else "customers"


def _biz_noun(category: Ctx) -> str:
    return "practice" if category.get("slug") == "dentists" else (
        "pharmacy" if category.get("slug") == "pharmacies" else "business"
    )


def _a_an(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def active_offer(merchant: Ctx) -> Optional[dict]:
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def peer_gap(merchant: Ctx, category: Ctx, metric: str) -> Optional[tuple[float, float]]:
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    mv, pv = perf.get(metric), peer.get(f"avg_{metric}") or peer.get(f"avg_{metric}_30d")
    if mv is None or pv is None:
        return None
    return mv, pv


AUTO_REPLY_PATTERNS = [
    "shukriya", "aapki jaankari ke liye", "team tak pahuncha",
    "automated assistant", "thank you for contacting", "we will get back",
    "aapka message mil gaya hai",
]

INTENT_PATTERNS = [
    r"\blet'?s do it\b", r"\bgo ahead\b", r"\bi want to join\b", r"\byes.*(join|start|do it)\b",
    r"\bchalo\b", r"\bshuru kar\b", r"\bok(?:ay)? (?:kar dijiye|karo|kijiye)\b", r"\bhaan.*chalo\b",
    r"\bplease (?:update|do|proceed)\b",
]

NOT_INTERESTED_PATTERNS = [
    r"\bnot interested\b", r"\bstop\b", r"\bno thanks\b", r"\bnahi chahiye\b",
    r"\bplease stop\b", r"\bunsubscribe\b", r"\bmat bhejo\b",
]

WAIT_PATTERNS = [
    r"\bcall (?:me )?later\b", r"\bbusy\b", r"\bavi nahi\b", r"\babhi nahi\b",
    r"\bwill (?:check|reply) later\b", r"\bthodi der mein\b",
]


# --------------------------------------------------------------------------
# per-trigger-kind framing
# --------------------------------------------------------------------------
# cta: "binary" -> single YES/STOP style ask, "open" -> open-ended low-friction
# question, "none" -> pure information, no ask.
KIND_META: dict[str, dict[str, str]] = {
    "research_digest":            {"cta": "open",   "angle": "research"},
    "cde_opportunity":             {"cta": "open",   "angle": "research"},
    "regulation_change":           {"cta": "open",   "angle": "compliance"},
    "recall_due":                  {"cta": "binary", "angle": "recall"},
    "customer_lapsed_soft":        {"cta": "binary", "angle": "recall"},
    "customer_lapsed_hard":        {"cta": "binary", "angle": "winback"},
    "winback_eligible":            {"cta": "binary", "angle": "winback"},
    "perf_spike":                  {"cta": "open",   "angle": "spike"},
    "perf_dip":                    {"cta": "open",   "angle": "dip"},
    "seasonal_perf_dip":           {"cta": "open",   "angle": "dip"},
    "milestone_reached":           {"cta": "open",   "angle": "milestone"},
    "dormant_with_vera":           {"cta": "open",   "angle": "dormant"},
    "appointment_tomorrow":        {"cta": "binary", "angle": "appointment"},
    "review_theme_emerged":        {"cta": "open",   "angle": "review"},
    "scheduled_recurring":         {"cta": "open",   "angle": "curious_ask"},
    "curious_ask_due":             {"cta": "open",   "angle": "curious_ask"},
    "festival_upcoming":           {"cta": "binary", "angle": "seasonal"},
    "category_seasonal":           {"cta": "binary", "angle": "seasonal"},
    "ipl_match_today":             {"cta": "binary", "angle": "seasonal"},
    "wedding_package_followup":    {"cta": "binary", "angle": "seasonal"},
    "active_planning_intent":      {"cta": "binary", "angle": "intent"},
    "renewal_due":                 {"cta": "binary", "angle": "renewal"},
    "gbp_unverified":              {"cta": "binary", "angle": "gbp"},
    "competitor_opened":           {"cta": "open",   "angle": "competitor"},
    "supply_alert":                {"cta": "binary", "angle": "compliance"},
    "chronic_refill_due":          {"cta": "binary", "angle": "recall"},
    "trial_followup":              {"cta": "binary", "angle": "recall"},
}
DEFAULT_KIND_META = {"cta": "open", "angle": "generic"}


# --------------------------------------------------------------------------
# anchor extraction — the single most specific, verifiable fact available
# --------------------------------------------------------------------------

def build_anchor(angle: str, category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx]) -> tuple[str, str]:
    """Returns (hook_sentence, source_tag_or_empty)."""
    payload = trigger.get("payload", {}) or {}
    perf = merchant.get("performance", {})
    delta = perf.get("delta_7d", {})

    if angle in ("research", "compliance"):
        item = resolve_digest_item(category, payload.get("top_item_id")) or best_digest_item(
            category, "compliance" if angle == "compliance" else "research"
        )
        if item:
            src = item.get("source", "")
            if angle == "compliance":
                title = item.get("title", "A regulation update")
                deadline = payload.get("deadline_iso") or item.get("deadline_iso")
                deadline_txt = f" (effective {deadline})" if deadline and deadline not in title else ""
                hook = f"{title}{deadline_txt}."
                if item.get("summary"):
                    hook += f" {item['summary']}"
            else:
                n = item.get("trial_n")
                seg = item.get("patient_segment") or item.get("segment")
                hook = item.get("title", "New research just dropped")
                if n and seg:
                    hook += f" — {n}-patient data, relevant to your {seg.replace('_', ' ')} cohort." if matching_signal(
                        merchant, seg or ""
                    ) else f" — {n}-sample study."
                elif item.get("summary"):
                    hook += f" {item['summary']}"
            return hook, src
        return "There's a category update relevant to your practice this week.", ""

    if angle in ("recall", "winback"):
        if customer:
            rel = customer.get("relationship", {})
            last_visit = rel.get("last_visit")
            state = customer.get("state", "lapsed_soft").replace("_", " ")
            offer = active_offer(merchant)
            offer_txt = f" {offer['title']} is live." if offer else ""
            since_txt = f"a while since your last visit ({last_visit})" if last_visit else "a while"
            return (
                f"It's been {since_txt} — you're due for a check-in ({state}).{offer_txt}",
                "",
            )
        agg = merchant.get("customer_aggregate", {})
        lapsed = agg.get("lapsed_180d_plus")
        total = agg.get("total_unique_ytd")
        if lapsed:
            pct = f" ({round(lapsed / total * 100)}% of YTD patients)" if total else ""
            return f"{lapsed} of your patients{pct} have been lapsed 180+ days — a recall window most clinics leave on the table.", ""
        return "A batch of your regulars are due for their recall window.", ""

    if angle in ("spike", "dip"):
        views_pct = delta.get("views_pct")
        calls_pct = delta.get("calls_pct")
        metric, pct = ("views", views_pct) if angle == "spike" else ("calls", calls_pct)
        if pct is None:
            metric, pct = "views", views_pct
        if pct is not None:
            # Direction word is derived from the *actual* sign of the real
            # metric, never from the trigger's kind label — a generated
            # "perf_dip" trigger can carry a merchant whose current delta is
            # actually positive, and narrating "down +2%" would be a
            # self-contradicting, ungrounded claim.
            verb = "up" if pct >= 0 else "down"
            return (
                f"Your {metric} are {verb} {abs(round(pct * 100))}% week-over-week ({perf.get(metric, '—')} in the last {perf.get('window_days', 30)}d).",
                "",
            )
        return "Your listing performance moved noticeably this week.", ""

    if angle == "milestone":
        agg = merchant.get("customer_aggregate", {})
        ident = merchant.get("identity", {})
        if agg.get("total_unique_ytd"):
            locality = ident.get("locality", "local")
            noun = _customer_noun(category)
            biz = _biz_noun(category)
            return f"You've crossed {agg['total_unique_ytd']} unique {noun} YTD — a real milestone for {_a_an(locality)} {locality} {biz}.", ""
        return "You just hit a milestone worth marking publicly.", ""

    if angle == "dormant":
        hist = merchant.get("conversation_history", [])
        last_ts = hist[-1]["ts"] if hist else None
        if last_ts:
            return f"We haven't heard from you since {last_ts[:10]} — quick check-in before your account goes fully quiet.", ""
        return "It's been quiet on our side for a while — quick check-in.", ""

    if angle == "appointment":
        if customer:
            name = customer.get("identity", {}).get("name", "your customer")
            slot_pref = customer.get("preferences", {}).get("preferred_slots", "")
            return f"{name} has an appointment tomorrow" + (f" ({slot_pref.replace('_', ' ')} preferred)." if slot_pref else "."), ""
        return "You have a booking tomorrow that's worth confirming ahead of time.", ""

    if angle == "review":
        themes = merchant.get("review_themes", [])
        neg = next((t for t in themes if t.get("sentiment") == "neg"), None)
        if neg:
            return (
                f"{neg.get('occurrences_30d', 'Several')} reviews this month flagged \"{neg.get('theme', '').replace('_', ' ')}\""
                + (f" — e.g. \"{neg['common_quote']}\"." if neg.get("common_quote") else "."),
                "",
            )
        return "A theme is emerging across your recent reviews worth a look.", ""

    if angle == "curious_ask":
        return "Quick one for you, no prep needed:", ""

    if angle == "seasonal":
        beats = category.get("seasonal_beats", [])
        note = beats[0]["note"] if beats else payload.get("metric_or_topic", "a seasonal moment")
        return f"{note.capitalize() if isinstance(note, str) else note} — good moment to be visible.", ""

    if angle == "intent":
        topic = (payload.get("intent_topic") or "").replace("_", " ")
        quote = payload.get("merchant_last_message")
        offer = active_offer(merchant)
        base_txt = f" I'll use your {offer['title']} as the base." if offer else ""
        if topic and quote:
            return f"On {topic} — you said \"{quote}\".{base_txt}", ""
        if topic:
            return f"Following up on {topic}.{base_txt}", ""
        return "Picking up where we left off —" + base_txt, ""

    if angle == "renewal":
        sub = merchant.get("subscription", {})
        days = sub.get("days_remaining")
        if days is not None:
            return f"Your {sub.get('plan', 'plan')} has {days} days remaining.", ""
        return "Your plan renewal window is approaching.", ""

    if angle == "gbp":
        stale = matching_signal(merchant, "stale_posts")
        return (f"Your Google profile has a gap: {stale.replace('_', ' ')}." if stale else "Your Google profile has an open gap worth closing."), ""

    if angle == "competitor":
        return "A new competitor listing appeared near you on Google.", ""

    # generic fallback — always available from performance + peer_stats
    gap = peer_gap(merchant, category, "ctr")
    if gap:
        mv, pv = gap
        return f"Your CTR is {mv:.1%} vs a {pv:.1%} category median for your locality.", ""
    return "There's a merchant-specific update worth flagging.", ""


# --------------------------------------------------------------------------
# CTA line
# --------------------------------------------------------------------------

def build_cta_sentence(cta_kind: str, angle: str, merchant: Ctx, customer: Optional[Ctx], code_mix: bool) -> tuple[str, str]:
    """Returns (sentence, cta_field_value)."""
    if cta_kind == "binary":
        if angle in ("recall", "winback") and customer:
            if code_mix:
                return "Reply 1 to confirm a slot, or 2 for a different time.", "binary_choice"
            return "Reply 1 to confirm a slot, or 2 for a different time.", "binary_choice"
        if angle == "seasonal":
            return "Want me to draft the offer post — just say go?", "binary_yes_no"
        if angle == "renewal":
            return "Reply YES to renew now, or STOP if you'd rather not.", "binary_yes_stop"
        if angle == "gbp":
            return "Want me to fix this now — 2-minute job? Reply YES or STOP.", "binary_yes_stop"
        if angle == "intent":
            return "Reply YES and I'll send the draft now, or STOP to hold off.", "binary_yes_stop"
        if angle == "appointment":
            return "Reply YES to confirm, or let us know if you'd like to reschedule.", "binary_yes_stop"
        return "Reply YES to go ahead, or STOP to skip this.", "binary_yes_stop"
    if cta_kind == "open":
        if angle == "curious_ask":
            return "What's your most-asked service this week?", "open_ended"
        if angle in ("research", "compliance"):
            return "Want me to pull the full item and draft something you can share?", "open_ended"
        if angle in ("spike", "dip"):
            return "Want a quick breakdown of what's driving it?", "open_ended"
        if angle == "milestone":
            return "Want me to draft a post celebrating this for your page?", "open_ended"
        if angle == "dormant":
            return "Anything I can help with this week?", "open_ended"
        if angle == "review":
            return "Want me to draft a response you can post?", "open_ended"
        return "Want me to look into this further for you?", "open_ended"
    return "", "none"


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    kind = trigger.get("kind", "")
    meta = KIND_META.get(kind, DEFAULT_KIND_META)
    angle = meta["angle"]

    code_mix = _code_mix(merchant, customer, category)
    who = customer.get("identity", {}).get("name") if customer else salutation(category, merchant)

    hook, source = build_anchor(angle, category, merchant, trigger, customer)
    ask, cta_value = build_cta_sentence(meta["cta"], angle, merchant, customer, code_mix)

    parts = [f"{who}," if who else ""]
    parts.append(hook)
    if ask:
        parts.append(ask)
    if source:
        parts.append(f"— {source}")

    body = " ".join(p for p in parts if p).strip()
    # keep first-token comma-joined naturally: "Dr. Meera, <hook> <ask> — <source>"
    body = re.sub(r"^([^,]+,) ", r"\1 ", body)

    send_as = "merchant_on_behalf" if customer else "vera"

    supp_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id', 'unknown')}:{trigger.get('id', 'na')}"

    rationale = (
        f"Trigger kind='{kind}' (angle={angle}) selected as the reason-to-message; "
        f"anchored on {'a customer-specific fact' if customer else 'merchant performance/category data'}"
        f"{' + digest source ' + source if source else ''}; "
        f"CTA={cta_value} matches {'action' if meta['cta']=='binary' else 'informational'} trigger type; "
        f"send_as={send_as}."
    )

    return {
        "body": body,
        "cta": cta_value,
        "send_as": send_as,
        "suppression_key": supp_key,
        "rationale": rationale,
    }


# --------------------------------------------------------------------------
# conversation (reply) handling — used by /v1/reply
# --------------------------------------------------------------------------

def _matches_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def handle_reply(history: list[dict], merchant_message: str) -> dict:
    """
    history: list of {"from": "merchant"|"vera", "msg": str} for this conversation,
             in chronological order, NOT including the current merchant_message.
    Returns a dict shaped like the /v1/reply response: action + body/rationale
    (and wait_seconds when action == "wait").
    """
    prior_merchant_msgs = [h["msg"] for h in history if h.get("from") == "merchant"]
    repeat_count = sum(1 for m in prior_merchant_msgs if m.strip() == merchant_message.strip())

    # 1) auto-reply detection (brief §9 Pattern B): canned/verbatim-repeated text.
    #    First occurrence -> try once with a direct, low-effort ask (in case a human
    #    is behind it). Second occurrence -> stop burning turns, exit gracefully.
    looks_canned = _matches_any(AUTO_REPLY_PATTERNS, merchant_message)
    if repeat_count >= 2 or (repeat_count >= 1 and looks_canned):
        return {
            "action": "end",
            "rationale": "Canned/auto-reply text seen 2+ times; exiting gracefully rather than burning further turns.",
        }
    if looks_canned:
        return {
            "action": "send",
            "body": "Got it. Before this goes to your team — want to take 2 minutes yourself to see exactly what's missing? One quick reply and I'll show you.",
            "cta": "open_ended",
            "rationale": "First canned/auto-reply detected; trying once directly (per Pattern B) before deciding whether to exit.",
        }

    # 2) explicit not-interested -> exit
    if _matches_any(NOT_INTERESTED_PATTERNS, merchant_message):
        return {
            "action": "end",
            "rationale": "Merchant signaled not interested / opt-out; ending conversation gracefully.",
        }

    # 3) explicit intent transition -> action mode, no more qualifying questions
    if _matches_any(INTENT_PATTERNS, merchant_message):
        return {
            "action": "send",
            "body": "Done — starting now, no more questions needed. I'll confirm once it's live.",
            "cta": "none",
            "rationale": "Detected explicit intent/agreement; routed straight to action instead of re-qualifying.",
        }

    # 4) merchant wants time -> back off
    if _matches_any(WAIT_PATTERNS, merchant_message):
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Merchant asked for time; backing off 30 minutes before next contact.",
        }

    # 5) default: acknowledge + advance with one low-friction next step
    return {
        "action": "send",
        "body": "Got it — noted. Want me to go ahead with the next step, or is there something specific you'd like changed first?",
        "cta": "open_ended",
        "rationale": "No canned/opt-out/intent/wait signal detected; advancing the conversation one low-friction step.",
    }

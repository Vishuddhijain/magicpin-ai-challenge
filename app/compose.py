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

from datetime import datetime, timezone
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


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 parser (accepts trailing 'Z'). Returns None on any
    parse failure instead of raising — callers treat that as "unknown"."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_trigger_expired(trigger: Ctx, now_iso: str) -> bool:
    """Fix #2 — reject expired triggers using trigger.expires_at vs tick.now.
    If either timestamp fails to parse, we don't have grounds to reject, so
    we treat the trigger as not-expired (fail open on unknown, never on a
    real not comparable read since that would be inventing an expiry)."""
    expires = parse_iso(trigger.get("expires_at"))
    now = parse_iso(now_iso)
    if expires is None or now is None:
        return False
    return now >= expires


# --------------------------------------------------------------------------
# consent enforcement (Fix #3) — customer trigger kinds -> consent scopes
# --------------------------------------------------------------------------
# Any ONE of the listed scopes being present in customer.consent.scope is
# sufficient to permit the outreach. Kinds not listed fall back to
# "any explicit consent at all", which is the conservative default: we only
# ever message a customer when they've said yes to *something*, and we only
# send category-appropriate content when they've said yes to *that*.
CONSENT_SCOPE_MAP: dict[str, list[str]] = {
    "recall_due":                 ["recall_reminders"],
    "customer_lapsed_soft":       ["recall_reminders", "winback_offers"],
    "customer_lapsed_hard":       ["winback_offers", "renewal_reminders"],
    "winback_eligible":           ["winback_offers", "renewal_reminders"],
    "wedding_package_followup":   ["bridal_package_followup"],
    "appointment_tomorrow":       ["appointment_reminders"],
    "chronic_refill_due":         ["refill_reminders", "recall_alerts"],
    "trial_followup":             ["program_updates", "kids_program_updates", "treatment_followup"],
}


def consent_allows(kind: str, customer: Optional[Ctx]) -> bool:
    """True only if this customer has actually consented to this kind of
    outreach. No customer context at all -> can't verify -> False."""
    if not customer:
        return False
    consent = customer.get("consent", {}) or {}
    if not consent.get("opted_in_at"):
        return False
    scope = set(consent.get("scope") or [])
    if not scope:
        return False
    required = CONSENT_SCOPE_MAP.get(kind)
    if required is None:
        # Unmapped customer-facing kind: any real consent at all is the floor.
        return True
    return bool(scope & set(required))


# --------------------------------------------------------------------------
# perf_spike / perf_dip / seasonal_perf_dip grounding (Fixes #4 + #5)
# --------------------------------------------------------------------------

def resolve_perf_signal(trigger: Ctx, merchant: Ctx) -> Optional[dict]:
    """Trigger.payload is the primary source of truth for spike/dip specifics
    (metric, delta_pct, window, vs_baseline, likely_driver). Only when the
    payload is missing real numbers (e.g. a generator placeholder payload)
    do we fall back to merchant.performance.delta_7d."""
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric")
    delta_pct = payload.get("delta_pct")
    if metric and delta_pct is not None:
        return {
            "metric": metric,
            "delta_pct": delta_pct,
            "window": payload.get("window"),
            "vs_baseline": payload.get("vs_baseline"),
            "likely_driver": payload.get("likely_driver"),
            "source": "payload",
        }
    # Fallback: merchant's own performance snapshot.
    delta7 = merchant.get("performance", {}).get("delta_7d", {}) or {}
    for candidate_metric, key in (("views", "views_pct"), ("calls", "calls_pct"), ("ctr", "ctr_pct")):
        if delta7.get(key) is not None:
            return {
                "metric": candidate_metric,
                "delta_pct": delta7[key],
                "window": "7d",
                "vs_baseline": None,
                "likely_driver": None,
                "source": "merchant_performance",
            }
    return None


def is_perf_trigger_contradicted(trigger: Ctx, merchant: Ctx) -> bool:
    """Fix #5 — a perf_spike trigger whose actually-measured delta is negative,
    or a perf_dip/seasonal_perf_dip trigger whose actually-measured delta is
    positive, is contradicted by the merchant's own real numbers. Sending it
    would be a contradictory/irrelevant message, so the caller (tick ranking)
    should suppress the trigger entirely rather than compose from it.
    When there is no measurable signal at all (neither payload nor merchant
    performance carries a number), we have no evidence *against* the trigger,
    so we do not treat that as a contradiction."""
    kind = trigger.get("kind", "")
    if kind not in ("perf_spike", "perf_dip", "seasonal_perf_dip"):
        return False
    signal = resolve_perf_signal(trigger, merchant)
    if signal is None or signal.get("delta_pct") is None:
        return False
    delta = signal["delta_pct"]
    if kind == "perf_spike":
        return delta < 0
    return delta > 0  # perf_dip / seasonal_perf_dip


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
    "aapka message mil gaya hai", "your query has been", "we have received your",
    "will respond within", "our support team", "ticket number", "ticket id",
    "case id", "reference number", "query has been logged", "query has been registered",
    "jald hi sampark", "jaldi sampark", "humein aapka message mila",
    "hamari team", "aapse sampark", "out of office", "auto-reply", "auto reply",
]

# Fix #7 — a bag of generic "this looks automated/boilerplate" marker stems
# (English + romanized Hindi). Used when a canned reply doesn't match any
# fixed AUTO_REPLY_PATTERNS substring verbatim (e.g. a *different* autoresponder
# template than the one seen earlier in the same conversation) — we score how
# many distinct canned-language markers a message hits rather than requiring
# an exact phrase or exact repeat.
CANNED_MARKER_STEMS = [
    "shukriya", "dhanyavad", "thank", "thanks", "contact", "team", "revert",
    "respond", "response", "received", "mil", "jald", "sampark", "karenge",
    "karegi", "query", "ticket", "case", "reference", "concern", "support",
    "automat", "bot", "acknowledg", "logged", "registered", "shortly", "soon",
]


def _canned_marker_hits(text: str) -> int:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    hits = 0
    for w in words:
        if any(w.startswith(stem) for stem in CANNED_MARKER_STEMS):
            hits += 1
    return hits


def _normalize_for_similarity(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def _text_similarity(a: str, b: str) -> float:
    """Token-set (Jaccard) similarity — robust to reordering/small wording
    changes between two differently-phrased canned messages, which a plain
    difflib character-ratio would under-score for short texts."""
    ta = set(_normalize_for_similarity(a).split())
    tb = set(_normalize_for_similarity(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def looks_like_canned_reply(text: str, prior_canned_texts: list[str]) -> bool:
    """Fix #7 — recognizes a canned/auto-reply even when it is NOT an exact
    string match and NOT one of the fixed AUTO_REPLY_PATTERNS substrings, as
    long as either (a) it carries enough generic canned-language markers on
    its own, or (b) it's textually similar to a message already flagged as
    canned earlier in this same conversation (a different autoresponder
    template saying essentially the same thing)."""
    if _matches_any(AUTO_REPLY_PATTERNS, text):
        return True
    if _canned_marker_hits(text) >= 3:
        return True
    for prior in prior_canned_texts:
        if _text_similarity(text, prior) >= 0.35:
            return True
    return False


INTENT_PATTERNS = [
    r"\blet'?s do it\b", r"\bgo ahead\b", r"\bi want to join\b",
    r"\byes\b", r"\bchalo\b", r"\bhaan\b", r"\bshuru kar\b",
    r"\bok(?:ay)? (?:kar dijiye|karo|kijiye)\b", r"\bdo it\b", r"\bsounds good\b",
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
    "seasonal_perf_dip":           {"cta": "open",   "angle": "seasonal_dip"},
    "milestone_reached":           {"cta": "open",   "angle": "milestone"},
    "dormant_with_vera":           {"cta": "open",   "angle": "dormant"},
    "appointment_tomorrow":        {"cta": "binary", "angle": "appointment"},
    "review_theme_emerged":        {"cta": "open",   "angle": "review"},
    "scheduled_recurring":         {"cta": "open",   "angle": "curious_ask"},
    "curious_ask_due":             {"cta": "open",   "angle": "curious_ask"},
    "festival_upcoming":           {"cta": "binary", "angle": "seasonal"},
    "category_seasonal":           {"cta": "binary", "angle": "seasonal"},
    "ipl_match_today":             {"cta": "binary", "angle": "ipl_match"},
    "wedding_package_followup":    {"cta": "binary", "angle": "wedding_followup"},
    "active_planning_intent":      {"cta": "binary", "angle": "intent"},
    "renewal_due":                 {"cta": "binary", "angle": "renewal"},
    "gbp_unverified":              {"cta": "binary", "angle": "gbp"},
    "competitor_opened":           {"cta": "open",   "angle": "competitor"},
    "supply_alert":                {"cta": "binary", "angle": "supply_alert"},
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
            if item.get("actionable"):
                hook += f" {item['actionable']}."
            return hook, src
        return "There's a category update relevant to your practice this week.", ""

    if angle == "supply_alert":
        # Fix #4 — payload is the primary (and often *only*) source for a
        # supply/recall alert: molecule + batch numbers are what makes this
        # actionable, and we never invent a batch number that isn't given.
        molecule = payload.get("molecule")
        batches = payload.get("affected_batches") or []
        manufacturer = payload.get("manufacturer")
        item = resolve_digest_item(category, payload.get("alert_id"))
        if molecule:
            hook = f"Supply alert: {molecule}"
            if batches:
                hook += f" — batches {', '.join(batches)}"
            if manufacturer:
                hook += f" ({manufacturer})"
            hook += " flagged."
            if item and item.get("summary"):
                hook += f" {item['summary']}"
            elif item and item.get("title"):
                hook += f" {item['title']}."
            if item and item.get("actionable"):
                hook += f" {item['actionable']}."
            return hook, item.get("source", "") if item else ""
        if item:
            hook = item.get("title", "A supply alert affects your stock.")
            if item.get("summary"):
                hook += f" {item['summary']}"
            return hook, item.get("source", "")
        return "A supply/compliance alert affecting your stock needs a look.", ""

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
        # Fix #4 — trigger.payload (metric/delta_pct/vs_baseline/likely_driver)
        # is the primary source; merchant.performance.delta_7d is only a
        # fallback for generator placeholder triggers that carry no real
        # numbers. Fix #5's contradiction check (is_perf_trigger_contradicted)
        # is applied by the tick-ranking layer *before* compose is ever
        # called for perf_spike/perf_dip, so by the time we get here the
        # direction implied by `angle` and the actual sign should already
        # agree — but we still derive the verb from the real sign, never
        # from the label, as a last line of defense.
        signal = resolve_perf_signal(trigger, merchant)
        if signal:
            metric = signal["metric"]
            pct = signal["delta_pct"]
            verb = "up" if pct >= 0 else "down"
            window = signal.get("window") or f"{perf.get('window_days', 30)}d"
            baseline_txt = f" vs a {signal['vs_baseline']}/day baseline" if signal.get("vs_baseline") is not None else ""
            driver_txt = f" — likely driver: {signal['likely_driver'].replace('_', ' ')}" if signal.get("likely_driver") else ""
            current = perf.get(metric, "—")
            return (
                f"Your {metric} are {verb} {abs(round(pct * 100))}% over the last {window}{baseline_txt} "
                f"({current} {metric} in the last {perf.get('window_days', 30)}d).{driver_txt}",
                "",
            )
        return "Your listing performance moved noticeably this week.", ""

    if angle == "seasonal_dip":
        # A dip that's *expected* for the season gets reframed, not alarmed:
        # pivot to what a merchant can proactively do in this low window
        # instead of implying something is wrong.
        signal = resolve_perf_signal(trigger, merchant)
        beats = category.get("seasonal_beats", [])
        season_note_raw = payload.get("season_note", "") or ""
        note = season_note_raw.replace("_", " ") if season_note_raw else None
        months = re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", season_note_raw.lower())
        beat_note = next(
            (b.get("note") for b in beats if any(mo in b.get("month_range", "").lower() for mo in months)),
            None,
        ) or (beats[0]["note"] if beats and not months else None)
        if signal and signal.get("delta_pct") is not None:
            pct_txt = f"{abs(round(signal['delta_pct'] * 100))}% below your usual — expected for this window, not a red flag."
            metric_txt = f"Your {signal['metric']} are {pct_txt}"
        else:
            metric_txt = "This is a seasonally quiet window for your category — not a red flag."
        if beat_note:
            return f"{metric_txt} {beat_note.capitalize()}.", ""
        return metric_txt, ""

    if angle == "milestone":
        # Fix #4 — the payload's own metric/value_now/milestone_value is the
        # ground truth for a milestone trigger; customer_aggregate is only a
        # fallback when the payload doesn't name a concrete metric.
        metric = payload.get("metric")
        value_now = payload.get("value_now")
        milestone_value = payload.get("milestone_value")
        if metric and value_now is not None:
            noun = metric.replace("_", " ")
            ident = merchant.get("identity", {})
            locality = ident.get("locality", "local")
            if milestone_value is not None and value_now < milestone_value:
                remaining = milestone_value - value_now
                return (
                    f"You're at {value_now} {noun}, {remaining} away from {milestone_value} — "
                    f"a milestone worth marking for {_a_an(locality)} {locality} {_biz_noun(category)}.",
                    "",
                )
            if milestone_value is not None:
                return f"You've crossed {value_now} {noun} — past the {milestone_value} milestone.", ""
            return f"You're at {value_now} {noun} — worth marking publicly.", ""
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

    if angle == "ipl_match":
        # Fix #4 — payload carries the real match/venue/time; never invent a
        # fixture the payload doesn't name.
        match = payload.get("match")
        venue = payload.get("venue")
        city = payload.get("city")
        match_time = payload.get("match_time_iso")
        if match:
            hook = f"{match} tonight"
            if venue:
                hook += f" at {venue}"
            if city:
                hook += f" ({city})"
            if match_time:
                # keep just the local time portion for readability
                time_part = match_time.split("T")[1][:5] if "T" in match_time else match_time
                hook += f", {time_part} kickoff"
            hook += " — match-night crowd looking for a place to watch."
            offer = active_offer(merchant)
            if offer:
                hook += f" Your {offer['title']} fits the moment."
            return hook, ""
        return "There's a match-night moment worth a quick promo push today.", ""

    if angle == "wedding_followup":
        # Fix #4 — days_to_wedding / trial_completed / next_step_window_open
        # from the payload are the anchor; customer name only when we
        # actually have a customer context (this trigger is customer-scoped).
        wedding_date = payload.get("wedding_date")
        trial_completed = payload.get("trial_completed")
        days_to_wedding = payload.get("days_to_wedding")
        next_step = payload.get("next_step_window_open")
        name = customer.get("identity", {}).get("name") if customer else None
        who = f"{name}'s" if name else "Their"
        parts = []
        if trial_completed:
            parts.append(f"{who} trial was completed on {trial_completed}")
        if days_to_wedding is not None and wedding_date:
            parts.append(f"the wedding is {days_to_wedding} days out ({wedding_date})")
        hook = "; ".join(parts) if parts else "the bridal trial is done and the wedding is approaching"
        hook = hook[0].upper() + hook[1:] + "."
        if next_step:
            hook += f" Next window open: {next_step.replace('_', ' ')}."
        return hook, ""

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
        # Fix #4 — payload's competitor_name/distance_km/their_offer are the
        # anchor. Never invent a competitor name or offer the payload
        # doesn't provide.
        name = payload.get("competitor_name")
        distance = payload.get("distance_km")
        their_offer = payload.get("their_offer")
        opened = payload.get("opened_date")
        if name:
            hook = f"{name} opened"
            if distance is not None:
                hook += f" {distance}km away"
            if opened:
                hook += f" on {opened}"
            hook += "."
            if their_offer:
                hook += f" They're running \"{their_offer}\"."
            my_offer = active_offer(merchant)
            if my_offer:
                hook += f" You currently have {my_offer['title']} live."
            return hook, ""
        # Item 12 fix — a placeholder competitor_opened payload (no real
        # name/distance/offer) previously fell back to a flat, no-fact line
        # ("A new competitor listing appeared near you on Google."), which is
        # exactly the "Generic — no merchant fact" failure mode the rubric
        # penalizes. Ground it in the merchant's own real, verifiable data
        # instead (CTR vs category peer median, or their own live offer)
        # rather than inventing a name/distance the payload doesn't give us.
        gap = peer_gap(merchant, category, "ctr")
        if gap:
            mv, pv = gap
            return (
                f"A new competitor listing appeared near you on Google. Your CTR is "
                f"{mv:.1%} vs a {pv:.1%} category median for your locality — worth a look "
                f"before they pull ahead on visibility."
            ), ""
        my_offer = active_offer(merchant)
        if my_offer:
            return (
                f"A new competitor listing appeared near you on Google. You currently have "
                f"{my_offer['title']} live, so it's worth checking how the two listings stack up."
            ), ""
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
        if angle == "ipl_match":
            return "Want me to push a match-night post now — just say go?", "binary_yes_no"
        if angle == "wedding_followup":
            return "Reply YES to book the next-step session, or STOP if not needed.", "binary_yes_stop"
        if angle == "supply_alert":
            return "Reply YES and I'll draft the customer notice now, or STOP to handle it yourself.", "binary_yes_stop"
        return "Reply YES to go ahead, or STOP to skip this.", "binary_yes_stop"
    if cta_kind == "open":
        if angle == "curious_ask":
            return "What's your most-asked service this week?", "open_ended"
        if angle in ("research", "compliance"):
            return "Want me to pull the full item and draft something you can share?", "open_ended"
        if angle == "spike":
            return "Want a quick breakdown of what's driving it?", "open_ended"
        if angle == "dip":
            return "Want a quick breakdown of what's driving it?", "open_ended"
        if angle == "seasonal_dip":
            return "Want me to suggest what to focus on this window instead?", "open_ended"
        if angle == "milestone":
            return "Want me to draft a post celebrating this for your page?", "open_ended"
        if angle == "dormant":
            return "Anything I can help with this week?", "open_ended"
        if angle == "review":
            return "Want me to draft a response you can post?", "open_ended"
        if angle == "competitor":
            return "Want me to check how your listing compares right now?", "open_ended"
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
# trigger ranking (Fix #1) — used by /v1/tick before it decides what to send
# --------------------------------------------------------------------------
# Each candidate is scored 0-10ish across the dimensions the fix calls out.
# The score is a ranking signal only; /v1/tick still applies hard gates
# (expiry, consent, dedup) before a candidate is ever composed/sent.

def _payload_richness(trigger: Ctx) -> float:
    """0..1 — how much real, specific data this trigger's payload carries.
    A generator placeholder payload ({"placeholder": True, ...}) scores near
    zero; a payload with several concrete fields scores high."""
    payload = trigger.get("payload", {}) or {}
    if payload.get("placeholder"):
        return 0.1
    concrete = [v for v in payload.values() if v not in (None, "", [], {})]
    if not concrete:
        return 0.1
    return min(1.0, 0.35 + 0.15 * len(concrete))


def _signal_strength(trigger: Ctx, merchant: Ctx, category: Ctx) -> float:
    """0..1 — does the merchant's own data back this trigger up? E.g. a
    review_theme_emerged trigger backed by merchant.review_themes, a
    perf trigger backed by a real measurable delta, a recall trigger backed
    by a real customer_aggregate."""
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    if kind in ("perf_spike", "perf_dip", "seasonal_perf_dip"):
        signal = resolve_perf_signal(trigger, merchant)
        return 1.0 if signal else 0.2
    if kind == "review_theme_emerged":
        return 1.0 if merchant.get("review_themes") else 0.3
    if kind in ("recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "winback_eligible"):
        agg = merchant.get("customer_aggregate", {})
        return 1.0 if agg.get("lapsed_180d_plus") else 0.5
    if kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert"):
        item = resolve_digest_item(category, payload.get("top_item_id") or payload.get("alert_id"))
        return 1.0 if item else 0.4
    if kind == "milestone_reached":
        return 1.0 if payload.get("value_now") is not None else 0.4
    if kind == "gbp_unverified":
        return 1.0 if matching_signal(merchant, "stale_posts") or not merchant.get("identity", {}).get("verified", True) else 0.4
    return 0.6  # neutral default for kinds with no specific merchant-side check


def _actionability(meta: dict) -> float:
    return 1.0 if meta.get("cta") == "binary" else (0.6 if meta.get("cta") == "open" else 0.3)


def _category_fit(trigger: Ctx, merchant: Ctx) -> float:
    """Mostly 1.0 since triggers are pre-scoped to a merchant_id whose
    category already matches; the one real check available in this dataset
    is festival_upcoming's category_relevance list."""
    payload = trigger.get("payload", {}) or {}
    relevance = payload.get("category_relevance")
    if relevance:
        return 1.0 if merchant.get("category_slug") in relevance else 0.4
    return 1.0


def _customer_relevance(trigger: Ctx, customer: Optional[Ctx]) -> float:
    if trigger.get("scope") != "customer":
        return 1.0  # merchant-facing trigger — customer relevance not applicable
    if not customer:
        return 0.0  # customer-scoped trigger with no customer context to ground it
    state = customer.get("state")
    kind = trigger.get("kind", "")
    if kind in ("customer_lapsed_soft",) and state != "lapsed_soft":
        return 0.5
    if kind in ("customer_lapsed_hard", "winback_eligible") and state not in ("lapsed_hard", "churned"):
        return 0.5
    return 1.0


def evaluate_trigger_candidate(
    trigger: Ctx,
    merchant: Ctx,
    category: Ctx,
    customer: Optional[Ctx],
    now_iso: str,
    already_sent_keys: set,
) -> dict:
    """Runs the hard gates (Fixes #2, #3, #5) and, if the candidate survives,
    computes a ranking score across the dimensions in Fix #1.

    Returns {"eligible": bool, "reason": str, "score": float}. `score` is
    only meaningful when eligible=True.
    """
    kind = trigger.get("kind", "")

    if is_trigger_expired(trigger, now_iso):
        return {"eligible": False, "reason": "expired", "score": 0.0}

    supp_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{trigger.get('id')}"
    if supp_key in already_sent_keys:
        return {"eligible": False, "reason": "duplicate_suppression_key", "score": 0.0}

    if trigger.get("scope") == "customer":
        if not customer:
            return {"eligible": False, "reason": "customer_scope_missing_customer_context", "score": 0.0}
        if not consent_allows(kind, customer):
            return {"eligible": False, "reason": "consent_does_not_cover_outreach", "score": 0.0}

    if is_perf_trigger_contradicted(trigger, merchant):
        return {"eligible": False, "reason": "perf_direction_contradicted_by_actual_metric", "score": 0.0}

    meta = KIND_META.get(kind, DEFAULT_KIND_META)
    urgency_norm = max(0.0, min(1.0, (trigger.get("urgency") or 1) / 5.0))
    relevance = _payload_richness(trigger)
    signal_strength = _signal_strength(trigger, merchant, category)
    customer_relevance = _customer_relevance(trigger, customer)
    actionability = _actionability(meta)
    category_fit = _category_fit(trigger, merchant)

    score = (
        2.0 * urgency_norm
        + 2.0 * relevance
        + 1.5 * signal_strength
        + 1.0 * customer_relevance
        + 1.0 * actionability
        + 1.0 * category_fit
    )

    return {
        "eligible": True,
        "reason": "ok",
        "score": round(score, 4),
        "breakdown": {
            "urgency": urgency_norm,
            "trigger_relevance": relevance,
            "merchant_signal_strength": signal_strength,
            "customer_relevance": customer_relevance,
            "actionability": actionability,
            "category_fit": category_fit,
        },
    }


def rank_trigger_candidates(candidates: list[dict]) -> list[dict]:
    """candidates: list of {"trigger_id", "trigger", "merchant", "category",
    "customer", "eval": <output of evaluate_trigger_candidate>}. Returns only
    the eligible ones, sorted best-first (ties broken by higher urgency,
    then trigger_id for full determinism)."""
    eligible = [c for c in candidates if c["eval"]["eligible"]]
    eligible.sort(
        key=lambda c: (-c["eval"]["score"], -(c["trigger"].get("urgency") or 0), c["trigger_id"])
    )
    return eligible


# --------------------------------------------------------------------------
# conversation (reply) handling — used by /v1/reply
# --------------------------------------------------------------------------

def _matches_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


# Fix #6 — a short label naming the concrete thing this conversation is
# actually about, pulled from the remembered original trigger/offer. Used to
# keep replies specific instead of generic "got it, noted" filler. Never
# invents facts: only uses what the stored state actually carries.
def _context_anchor_phrase(state: Optional[dict]) -> Optional[str]:
    if not state:
        return None
    trigger = state.get("trigger") or {}
    payload = trigger.get("payload") or {}
    kind = trigger.get("kind", "")
    offer = state.get("selected_offer")

    if kind == "competitor_opened" and payload.get("competitor_name"):
        return f"the {payload['competitor_name']} competitor listing"
    if kind in ("perf_dip", "seasonal_perf_dip") and payload.get("metric"):
        return f"your {payload['metric']} dip"
    if kind == "perf_spike" and payload.get("metric"):
        return f"your {payload['metric']} spike"
    if kind == "milestone_reached" and payload.get("metric"):
        return f"your {payload['metric'].replace('_', ' ')} milestone"
    if kind == "supply_alert" and payload.get("molecule"):
        return f"the {payload['molecule']} supply alert"
    if kind == "wedding_package_followup" and payload.get("next_step_window_open"):
        return f"the {payload['next_step_window_open'].replace('_', ' ')} next step"
    if kind == "ipl_match_today" and payload.get("match"):
        return f"tonight's {payload['match']} promo"
    if kind == "regulation_change":
        return "the compliance update"
    if kind == "research_digest":
        return "that research item"
    if kind == "recall_due" and payload.get("service_due"):
        return f"the {payload['service_due'].replace('_', ' ')} recall"
    if kind == "chronic_refill_due":
        return "the refill reminder"
    if kind == "active_planning_intent" and payload.get("intent_topic"):
        return payload["intent_topic"].replace("_", " ")
    if offer and offer.get("title"):
        return f"your {offer['title']}"
    return None


def _ensure_state(state: Optional[dict]) -> dict:
    """Normalizes/defaults the conversation state dict (Fix #6 fields) so the
    rest of this function can read/update it uniformly whether the caller
    passed a fully-populated state (from bot.py's conversation_meta) or None
    (e.g. direct unit-test calls with no state tracking)."""
    s = state if state is not None else {}
    s.setdefault("trigger", {})
    s.setdefault("selected_offer", None)
    s.setdefault("auto_reply_count", 0)
    s.setdefault("intent_state", "none")   # none | qualifying | confirmed
    s.setdefault("opt_out", False)
    s.setdefault("canned_texts", [])       # Fix #7 — canned messages seen so far, for fuzzy matching
    return s


def handle_reply(history: list[dict], merchant_message: str, state: Optional[dict] = None) -> dict:
    """
    history: list of {"from": "merchant"|"vera", "msg": str} for this conversation,
             in chronological order, NOT including the current merchant_message.
    state:   optional conversation-state dict (Fix #6) remembering the
             original trigger, merchant/customer ids, original outbound
             body, selected offer, category, auto_reply_count, intent_state,
             and opt_out. When bot.py passes this in, it is mutated in place
             (auto_reply_count/intent_state/opt_out are updated to reflect
             this turn) so the caller can persist it back into its own
             conversation store. Optional and defaulted for backward
             compatibility with direct calls that don't track state.
    Returns a dict shaped like the /v1/reply response: action + body/rationale
    (and wait_seconds when action == "wait").
    """
    s = _ensure_state(state)
    anchor = _context_anchor_phrase(s)

    prior_merchant_msgs = [h["msg"] for h in history if h.get("from") == "merchant"]
    repeat_count = sum(1 for m in prior_merchant_msgs if m.strip() == merchant_message.strip())

    # 1) auto-reply detection (brief §9 Pattern B): canned text, whether it's a
    #    verbatim repeat OR a *different*-worded canned/autoresponder message
    #    (Fix #7 — looks_like_canned_reply checks fixed patterns, generic
    #    canned-language markers, and fuzzy similarity to canned texts already
    #    seen this conversation, not just exact string equality).
    #    First occurrence -> try once with a direct, low-effort ask (in case a
    #    human is behind it). Second occurrence (even worded differently) ->
    #    stop burning turns, exit gracefully.
    looks_canned = looks_like_canned_reply(merchant_message, s["canned_texts"])
    if looks_canned:
        s["auto_reply_count"] = s.get("auto_reply_count", 0) + 1
        s["canned_texts"].append(merchant_message)
    if repeat_count >= 2 or (repeat_count >= 1 and looks_canned) or s["auto_reply_count"] >= 2:
        return {
            "action": "end",
            "rationale": "Canned/auto-reply text seen 2+ times (including differently-worded canned "
                         "messages recognized via marker/similarity matching, not just exact string "
                         "repeats); exiting gracefully rather than burning further turns.",
        }
    if looks_canned:
        topic_txt = f" on {anchor}" if anchor else ""
        return {
            "action": "send",
            "body": f"Got it. Before this goes to your team — want to take 2 minutes yourself{topic_txt}? One quick reply and I'll show you.",
            "cta": "open_ended",
            "rationale": "First canned/auto-reply detected; trying once directly (per Pattern B) before deciding whether to exit.",
        }

    # 2) explicit not-interested -> exit
    if _matches_any(NOT_INTERESTED_PATTERNS, merchant_message):
        s["opt_out"] = True
        return {
            "action": "end",
            "rationale": "Merchant signaled not interested / opt-out; ending conversation gracefully.",
        }

    # 3) explicit intent transition -> action mode, no more qualifying questions.
    #    Fix #8 — this must never claim an external action (posting an offer,
    #    sending a campaign, etc.) has already happened, since compose()/this
    #    bot has not actually performed one; it only stops asking further
    #    qualifying questions and states what happens next.
    if _matches_any(INTENT_PATTERNS, merchant_message):
        s["intent_state"] = "confirmed"
        what_txt = f" on {anchor}" if anchor else " on this"
        return {
            "action": "send",
            "body": f"Got it — starting{what_txt} now, no more questions from my side. I'll message you here once it's ready.",
            "cta": "none",
            "rationale": "Detected explicit intent/agreement; routed straight to action mode instead of "
                         "re-qualifying, without claiming any action has already been completed.",
        }

    # 4) merchant wants time -> back off
    if _matches_any(WAIT_PATTERNS, merchant_message):
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Merchant asked for time; backing off 30 minutes before next contact.",
        }

    # 5) default: acknowledge + advance with one low-friction next step,
    #    naming the concrete topic (Fix #6) instead of a generic "noted".
    if s["intent_state"] == "none":
        s["intent_state"] = "qualifying"
    if anchor:
        body = f"Got it — on {anchor}, want me to go ahead, or is there something specific you'd like changed first?"
    else:
        body = "Got it — noted. Want me to go ahead with the next step, or is there something specific you'd like changed first?"
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "No canned/opt-out/intent/wait signal detected; advancing the conversation one low-friction step"
                     + (f", anchored on '{anchor}' from the original trigger/offer" if anchor else "") + ".",
    }

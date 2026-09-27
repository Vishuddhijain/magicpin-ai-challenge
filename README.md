# Vera — magicpin AI Challenge Submission

**Live bot:** https://vera-bot-585x.onrender.com
**Health check:** https://vera-bot-585x.onrender.com/v1/healthz

> Note: hosted on Render's free tier, which spins down after ~15 minutes of
> inactivity. The first request after idle may take 30–50s to wake the
> instance. In-memory context does not survive a spin-down — re-run
> `scripts/push_context.py` against the live URL before any evaluation
> window if the instance may have gone idle.

---

## Approach

A **deterministic, rule-based composer** (`app/compose.py`) — no LLM call in
the hot path, so it is fast (sub-100ms, well under the 30s budget), free to
run, and provably deterministic (`tests/test_compose.py::test_determinism`).

For each `(category, merchant, trigger, customer?)` input:

1. **Pick an angle** from the trigger's `kind` (24 kinds mapped to ~15
   framing angles: research, compliance, recall, winback, spike, dip,
   milestone, dormant, appointment, review, curious_ask, seasonal, intent,
   renewal, gbp, competitor).
2. **Extract the single most specific, verifiable fact available** for that
   angle — a digest item's trial size, a real performance delta, a real
   offer price, a real customer's last-visit date, a real conversation
   quote — always read from the four context dicts, never invented. Every
   `research_digest` / `regulation_change` message that resolves a real
   digest item also carries its citation (e.g. `— JIDA Oct 2026, p.14`).
   Directional claims (`perf_spike` / `perf_dip`) are derived from the
   _actual sign_ of the real metric delta, never from the trigger's label —
   this prevents self-contradicting output on generated triggers whose kind
   and underlying data can disagree.
3. **Match category voice**: clinical/peer tone + real vocabulary for
   dentists/pharmacies, warmer tone elsewhere; Hindi-English code-mix is
   used only when the merchant's or customer's language preference calls
   for it.
4. **Attach exactly one CTA** — binary (`YES`/`STOP`, or a numbered slot
   choice) for action-shaped triggers, open-ended for informational ones.
5. Return `body`, `cta`, `send_as` (`vera` vs `merchant_on_behalf`),
   `suppression_key` (reused from the trigger when present, else derived),
   and a `rationale` naming the trigger kind, the anchor used, and why the
   CTA shape fits.

**Conversation handling** (`handle_reply`, used by `/v1/reply`) implements
the three behaviours the brief flags as production Vera's biggest gaps:

- **Auto-reply detection** — canned phrasing is met with one direct nudge
  (brief §9 Pattern B); if it repeats _within the same conversation_, the
  bot exits gracefully instead of burning turns.
- **Intent-transition routing** — explicit agreement ("let's do it", "go
  ahead", "haan chalo") routes straight to action, never another
  qualifying question (the failure mode in Pattern D).
- **Graceful exit** — explicit opt-out/not-interested ends the
  conversation; "call me later"-type replies trigger a `wait` instead of a
  `send`.

Anti-repetition and dedup are enforced structurally: `/v1/tick` tracks
`suppression_key`s already sent per merchant and skips repeats; `/v1/reply`
tracks bodies already sent per conversation and rephrases rather than
resending verbatim. `/v1/context` treats a re-post of an unchanged version
as an idempotent no-op (per brief §3) rather than rejecting it as stale —
this matters because a judge harness that re-syncs context mid-run should
not have those pushes silently fail.

## Why rule-based instead of an LLM call

- **Determinism is free** — no temperature=0 caveat, no seed-drift risk.
- **Zero external dependencies / API cost** — the bot works out of the box
  with nothing but `pip install -r requirements.txt`.
- **Latency is a non-issue** — every response is nowhere near the 30s
  budget, so there's no risk of the "return `{actions: []}` because we ran
  out of time" failure mode.
- **Trade-off**: a ceiling on linguistic creativity/naturalness compared to
  an LLM composer. The natural next step, given more time, is an optional
  LLM "polish" pass (temperature=0, strict system prompt forbidding new
  facts) layered on top of the same anchor-extraction logic — the anchor
  extraction is the part that needs to be grounded and hand-verified, so
  keeping _that_ rule-based and only asking an LLM to rephrase (never to
  source facts) would preserve the no-hallucination guarantee while
  improving fluency.

## What additional context would have helped most

- A structured `next_best_action` hint per trigger (even a category tag)
  would remove the need to infer a framing angle from `kind` string
  matching.
- Some generator-expanded triggers carry `payload: {"placeholder": true}`
  rather than real fields — real payloads (like the seed set) make a much
  bigger quality difference than any prompting change would, since the
  composer intentionally refuses to fabricate specifics it wasn't given.

## Repo layout

```
app/
  compose.py     — pure composer + reply-handler (no I/O, fully unit-testable)
  bot.py         — FastAPI server exposing the 5 required endpoints
scripts/
  push_context.py        — pushes expanded/ dataset into a running bot via /v1/context
  generate_submission.py — produces submission.jsonl from the 30 canonical test pairs
tests/
  test_compose.py — smoke tests (determinism, required keys, reply routing)
dataset/         — seed data + generator (unchanged from the challenge pack)
expanded/        — generated locally, not committed (see below)
submission.jsonl — the 30 required outputs, generated by scripts/generate_submission.py
judge_simulator.py — local LLM-judge dry-run tool (provided by the challenge; not part of the bot itself)
```

`expanded/` is a build artifact reproduced deterministically from
`dataset/generate_dataset.py` + the seed files, so it's excluded from
version control rather than committed. Regenerate it locally with:

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
```

## How to run locally

```bash
python -m venv venv
venv\Scripts\activate        # Windows PowerShell
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
uvicorn app.bot:app --host 0.0.0.0 --port 8080
```

In a second terminal:

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python scripts/push_context.py --bot-url http://localhost:8080 --data expanded
```

Verify:

```bash
curl http://localhost:8080/v1/healthz
```

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

Covers: output determinism, required response keys, `merchant_on_behalf`
send-as routing for customer-facing messages, auto-reply detection +
graceful exit, and intent-transition routing.

An optional local LLM-judge dry-run (`judge_simulator.py`, provided by the
challenge pack) scores the bot against the 5 rubric dimensions before
submission. It needs its own API key for whichever provider you choose —
set via a local `.env` file (`GROQ_API_KEY=...` or equivalent), never
committed. This tool is independent of the bot itself and isn't required
for the bot to function.

## Regenerating the deliverable

```bash
python scripts/generate_submission.py --data expanded --out submission.jsonl
```

## Deployment

Hosted on [Render](https://render.com) (free tier):

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app.bot:app --host 0.0.0.0 --port $PORT`
- **Python version:** pinned via `.python-version` (`3.11`) — Render's
  current default (3.14) has no prebuilt wheel for this project's pinned
  `pydantic-core` version, which fails to build from source in Render's
  build sandbox. Pinning to 3.11 uses a prebuilt wheel and avoids the
  issue entirely.

## Team

- **Team name:** Vishuddhi Jain
- **Members:** Vishuddhi Jain
- **Contact:** https://github.com/Vishuddhijain/

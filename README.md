# Provenance Guard

A backend attribution analysis system for creative platforms. Accepts text submissions, classifies them as human-written or AI-generated using a three-signal detection pipeline, returns a transparency label, and handles creator appeals.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GROQ_API_KEY=your_key_here
```

Run the server:

```bash
python app.py
```

The server starts on `http://localhost:5001`. The SQLite audit database (`audit.db`) is created automatically on first run.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/submit` | Submit content for attribution analysis |
| POST | `/appeal` | Contest a classification |
| GET | `/status/<content_id>` | Check classification and appeal status |
| GET | `/log` | Retrieve recent audit log entries |
| GET | `/analytics` | Detection pattern summary dashboard |
| POST | `/certificate/request` | Request a verified-human provenance certificate |
| GET | `/certificate/<certificate_id>` | Retrieve a provenance certificate |

---

## Architecture

A piece of text enters the system at `POST /submit`. The endpoint first checks the **rate limiter** — if the IP has exceeded the allowed window, the request is rejected before any detection work begins.

If the request passes, the raw text is handed to the **detection pipeline**, which runs three independent signals. Their scores are passed to the **confidence scorer**, which combines them into a single 0–1 probability using a weighted average. That score feeds the **label engine**, which maps it to one of three transparency label variants. The full decision record — signal scores, combined score, label, content ID, timestamp — is written to the **SQLite audit log** and returned to the caller.

If a creator disputes the result, `POST /appeal` looks up the original record, appends the creator's reasoning, and updates the submission's status to `"under_review"`. No automated re-classification occurs.

```
POST /submit
  → Rate Limiter
  → Detection Pipeline
      Signal 1: LLM Classification     (weight 0.50)
      Signal 2: Stylometric Heuristics (weight 0.25)
      Signal 3: Burstiness Scoring     (weight 0.25)
  → Confidence Scorer
  → Label Engine
  → Audit Log (SQLite)
  → JSON response

POST /appeal
  → Look up content_id in Audit Log
  → Append appeal record
  → Update status → "under_review"
  → JSON response
```

---

## Detection Signals

### Why these two signals?

The core design constraint was that the two signals needed to be genuinely independent — measuring different properties so they'd disagree in informative ways rather than just duplicating each other. The LLM signal asks "does this *read* like AI wrote it?" — a holistic semantic judgment. The stylometric signal asks "do the *numbers* look like AI wrote it?" — a structural, statistical judgment. A text can fool one without fooling the other, and when they disagree that disagreement is itself a signal worth capturing (the analytics dashboard tracks disagreement rate for exactly this reason).

Burstiness was added as a third signal because it catches something neither of the first two can: the *shape* of the distribution across the text, not just the averages. Two texts can have identical stylometric averages while one has near-zero variance (AI's tell) and the other swings wildly (human's tell). That flatness is invisible to stylometry but detected by burstiness.

---

### Signal 1 — LLM Classification (Groq)

**What it measures:** Holistic semantic and stylistic coherence. The text is sent to `llama-3.3-70b-versatile` with a structured prompt; the model returns an `ai_probability` float and a one-sentence reasoning string.

**Why it works:** AI models tend to overuse hedging phrases ("it's worth noting", "in conclusion"), produce unusually balanced paragraph structure, use formal register in casual contexts, and lack the micro-inconsistencies that come from real-time human composition. The model recognizes these patterns because it has seen both at scale.

**Blind spots:** Polished human writing and lightly edited AI text ("AI laundering") can both fool it. Output varies slightly across API calls. It has no memory of a creator's prior style — it treats every submission cold.

**If deploying for real:** The prompt would need iteration against a labeled dataset. The current prompt was written from first principles; production would require testing against known-human and known-AI samples and refining which tells the model is instructed to weight most heavily. I'd also cache the Groq client rather than initializing it per-request in the lazy global pattern used here. Weight: **0.50**.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Four statistical surface properties of the text:

| Feature | AI tendency | Human tendency |
|---|---|---|
| Sentence length variance | Low — uniform lengths | High — jagged, irregular |
| Type-token ratio (TTR) | Lower — repeated vocabulary | Higher — more diverse word choice |
| Punctuation density | Lower — lightly punctuated | Higher — dashes, ellipses, exclamations |
| Average word length | Slightly longer (formal) | Shorter on average |

**Why it works:** LLMs are trained to minimize perplexity, which produces statistically smooth text. Human writers are not optimizing for smoothness — they repeat words because they forgot they used them, write long sentences when excited and short ones for emphasis.

**Calibration note:** TTR is unreliable on short texts (< 150 words) because almost no words repeat in a 3-sentence passage regardless of authorship. On short inputs the TTR sub-weight drops from 0.35 to 0.10, with the freed weight shifting to average word length (0.15 → 0.40), which differentiates reliably at any length.

**If deploying for real:** The thresholds (what counts as "low" variance, what's "normal" punctuation density) were set by reasoning from first principles and spot-checking a handful of examples. A production version would calibrate these against a labeled corpus — likely producing different numbers and possibly different features entirely. Sentence length variance may be less useful than I assumed for creative writing, where long sentences are a stylistic choice for both humans and AI. Weight: **0.25**.

---

### Signal 3 — Burstiness Scoring (pure Python)

**What it measures:** The coefficient of variation (standard deviation ÷ mean) of per-sentence complexity values (word count × average word length). High CoV = bursty = human-like. Low CoV = smooth = AI-like.

**Why it works:** Human writing alternates between dense, complex sentences and short punchy ones. AI output is uniformly smooth throughout — medium-complexity sentences from start to finish. Signal 2 captures averages; Signal 3 captures the *shape* of how those values vary across the text.

**Blind spots:** Unreliable on texts with fewer than 4 sentences — returns a neutral 0.5 fallback in that case. Certain literary forms (minimalist prose, aphorisms, numbered lists) are intentionally low-burstiness and will score falsely high for AI. When unreliable, its weight is redistributed proportionally between the other two signals.

**If deploying for real:** The CoV → AI probability mapping (`1.0 - cov / 0.7`) was derived from reasoning about typical human vs. AI CoV ranges, not measured data. This is the signal I'd most want to validate empirically before deploying. Weight: **0.25**.

---

## Confidence Scoring

### Combination method

```
combined = 0.50 × llm_score + 0.25 × stylometric_score + 0.25 × burstiness_score
```

If burstiness is unreliable (text too short), its weight redistributes proportionally between LLM and stylometric.

### Why this approach?

The LLM signal gets the highest weight because it captures the most information per signal — it's reading the text holistically rather than measuring one statistical property. The two pure-Python signals split the remaining weight equally because they're measuring complementary but similarly reliable structural properties. The weights are judgment calls, not empirically derived — a deployed version would tune these against a labeled dataset.

### Label thresholds

| Combined score | Attribution | Label |
|---|---|---|
| < 0.40 | `human` | High-confidence human |
| 0.40 – 0.72 | `uncertain` | Uncertain |
| ≥ 0.73 | `ai` | High-confidence AI |

**Why a wide uncertain band?** A false positive — labeling a human's work as AI-generated — is worse than a false negative on a writing platform. The 33-point uncertain band ensures that ambiguous cases never receive an accusatory label. The 0.73 AI threshold was tuned during testing: at 0.75 the "clearly AI" test case (combined 0.79) passed but barely, leaving no margin; at 0.70 a formal-register human text (combined 0.72) was falsely accused. 0.73 cleanly separates those two cases.

### Example submissions showing meaningful score variation

**High-confidence AI** — templated paragraph with hedging phrases and formal register:
> *"Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications."*

| Signal | Score |
|---|---|
| LLM | 0.900 |
| Stylometric | 0.575 |
| Burstiness | 0.500 (unreliable — short text) |
| **Combined** | **0.792 → `ai`** |

**Lower-confidence case** — formal academic prose that a human economist might write:
> *"The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension between their mandate for price stability and the unintended consequences of prolonged low interest rates."*

| Signal | Score |
|---|---|
| LLM | 0.800 |
| Stylometric | 0.556 |
| Burstiness | 0.500 (unreliable — short text) |
| **Combined** | **0.719 → `uncertain`** |

The LLM sees formal academic register and flags it; the combined score lands in the uncertain band because the system correctly cannot distinguish "formal human prose" from "AI-generated formal prose" without more signal. This is the right behavior — an accusation requires higher confidence than a suspicion.

---

## Transparency Labels

All three label variants are shown below exactly as they appear in API responses (`label.verdict`, `label.explanation`, `label.confidence_note`).

### High-confidence human (`score < 0.40`)

> **Likely written by a human.**
>
> Our analysis found strong indicators of human authorship in this piece.
>
> *If you believe this label is incorrect, you may submit an appeal.*

### High-confidence AI (`score ≥ 0.73`)

> **Likely AI-generated.**
>
> Our analysis found strong indicators that this content was generated by an AI tool.
>
> *Creators who believe this is a mistake can submit an appeal — misclassification happens, and every appeal is reviewed.*

### Uncertain (`0.40 ≤ score < 0.73`)

> **Authorship unclear.**
>
> Our system could not confidently determine whether this content was written by a human or generated by AI. This label does not mean the content is AI-generated — it means we don't have enough signal to say either way.
>
> *If you're the creator, you can submit an appeal to have this reviewed.*

---

## Rate Limiting

Implemented with Flask-Limiter. Counters are stored in memory (reset on restart).

| Endpoint | Limit | Window | Reasoning |
|---|---|---|---|
| `POST /submit` | 10 requests | per minute per IP | A genuine creator doesn't need to submit more than 10 pieces per minute. Blocks naive flood attacks and limits Groq API cost exposure. |
| `POST /appeal` | 5 requests | per hour per IP | An appeal is a deliberate human action. More than 5 per hour indicates gaming rather than legitimate use. |
| `POST /certificate/request` | 10 requests | per hour per IP | Certificates are one-per-content; high volume suggests automated abuse. |
| `GET /log` | 30 requests | per minute per IP | Read-only and cheap, but gated to prevent enumeration of content IDs at speed. |
| `GET /analytics` | 30 requests | per minute per IP | Same reasoning as `/log`. |

**Rate limit verification** — sending 12 rapid requests to `POST /submit` (exceeds the 10/minute cap):

```
200
200
200
200
200
200
200
200
200
200
429
429
```

The 11th and 12th requests return HTTP 429 with body:

```html
<title>429 Too Many Requests</title>
<h1>Too Many Requests</h1>
<p>10 per 1 minute</p>
```

**Note:** In a multi-worker production deployment, replace `storage_uri="memory://"` with a Redis URI so limits are shared across workers.

---

## Appeals Workflow

When a creator believes their content has been misclassified:

1. Call `POST /appeal` with the `content_id`, their `creator_id`, and a written `reasoning` explaining why they believe the label is wrong.
2. The system looks up the original audit record. If not found, returns 404.
3. An appeal record is appended to the `appeals` table with its own `appeal_id` and timestamp.
4. The submission's `status` is updated from `"classified"` to `"under_review"`.
5. A human moderator would review the appeal; no automated re-classification occurs.

**Example request:**
```json
POST /appeal
{
  "content_id": "e1dec9bb-ed9d-4e1d-9d44-03d8b291215e",
  "creator_id": "user-003",
  "reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
}
```

**Example response:**
```json
{
  "appeal_id": "f9682f04-80e4-486c-aa19-9aa5f9a86d9b",
  "content_id": "e1dec9bb-ed9d-4e1d-9d44-03d8b291215e",
  "status": "under_review",
  "message": "Your appeal has been received and will be reviewed.",
  "timestamp": "2026-06-27T16:55:48.901234+00:00"
}
```

---

## Audit Log

Every attribution decision is written to a SQLite database (`audit.db`). Retrieve recent entries with:

```
GET /log?limit=20&offset=0
```

**Live entries** (from `GET /log` — one entry per attribution state, entry 3 includes a filed appeal):

```json
{
  "entries": [
    {
      "content_id": "6db2b00c-9381-4e14-9bb5-905cdfc1108e",
      "creator_id": "user-001",
      "timestamp": "2026-06-27T16:55:47.098612+00:00",
      "attribution": "ai",
      "confidence": 0.7915,
      "llm_score": 0.9,
      "stylometric_score": 0.5745,
      "burstiness_score": 0.5,
      "status": "classified",
      "appeal_id": null,
      "appeal_reasoning": null
    },
    {
      "content_id": "185c57a5-dbb2-4610-a0b7-83611958855a",
      "creator_id": "user-002",
      "timestamp": "2026-06-27T16:55:47.716609+00:00",
      "attribution": "human",
      "confidence": 0.1799,
      "llm_score": 0.1,
      "stylometric_score": 0.1847,
      "burstiness_score": 0.335,
      "status": "classified",
      "appeal_id": null,
      "appeal_reasoning": null
    },
    {
      "content_id": "e1dec9bb-ed9d-4e1d-9d44-03d8b291215e",
      "creator_id": "user-003",
      "timestamp": "2026-06-27T16:55:48.125234+00:00",
      "attribution": "uncertain",
      "confidence": 0.7187,
      "llm_score": 0.8,
      "stylometric_score": 0.556,
      "burstiness_score": 0.5,
      "status": "under_review",
      "appeal_id": "f9682f04-80e4-486c-aa19-9aa5f9a86d9b",
      "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
    }
  ]
}
```

---

## Known Limitations

### Non-native English speakers and formal human writers

The system will systematically over-flag content written by people who use formal register naturally — non-native English speakers who learned formal written English, academics writing outside their research context, or anyone who writes more "correctly" than casually. This isn't a calibration error; it's a structural problem. Both the LLM signal and the stylometric signal treat formal, consistent, polished prose as AI-like, because AI text is also formal, consistent, and polished. The signals can't distinguish between "polished because AI" and "polished because the writer is careful." The third audit log entry above is a real example: a piece of academic writing that a human economist might genuinely produce scores 0.72 — just below the AI threshold, landing in uncertain. A slightly more formal version of that same writing would cross it.

The label design mitigates this somewhat — the uncertain band is wide precisely because this case is common — but a non-native English speaker who consistently writes formally will receive "Authorship unclear" labels that feel accusatory even if they technically stop short of "Likely AI-generated." The appeals workflow exists specifically for this scenario.

### "AI laundering" (lightly edited AI output)

The lightly-edited-AI test case scored 0.268 (`human`). The system missed it entirely. The LLM signal gave it 0.20 because the editing removed enough AI tells that the model couldn't detect them. This is a known limitation of LLM-based detection: once the obvious surface markers (hedging phrases, templated transitions, overly balanced structure) are edited out, the signal degrades significantly. Stylometric features don't help here either — a well-edited piece of AI text can have natural-looking variance if the editor introduced sentence-length irregularities.

This is an honest limitation, not a calibration fix. Robust laundering detection would require signals that AI editing doesn't smooth away — something like comparing the text against known AI model output distributions, or tracking edit history — neither of which is in scope here.

### Short texts

Burstiness returns a neutral fallback for any text under 4 sentences, and TTR becomes unreliable under ~150 words. Poetry and microfiction are the primary use cases where this matters — a haiku or a 3-line prose poem will never produce a reliable burstiness score, and its TTR will be artificially high regardless of authorship. The system still runs the LLM and stylometric signals on short texts, but with effectively one fewer signal and a recalibrated stylometric scorer. A poem confidently classified as AI by the LLM and stylometric signals should still be trusted; it's the uncertain cases on short texts where the system has the least information.

---

## Stretch Features

### Ensemble Detection ✓

Three independent signals with documented weighting (LLM 0.50 / stylometry 0.25 / burstiness 0.25). Each captures a genuinely different property: semantic coherence, aggregate surface statistics, and sentence-complexity variance. When burstiness is unreliable, its weight redistributes proportionally rather than defaulting to a fixed fallback.

### Analytics Dashboard ✓

`GET /analytics` returns a live summary derived from the audit log:

```json
{
  "total_submissions": 42,
  "attribution_counts": { "human": 18, "ai": 15, "uncertain": 9 },
  "appeal_rate_pct": 7.14,
  "signal_disagreement_rate_pct": 23.8,
  "avg_confidence": 0.6134
}
```

**Metrics:**
- `attribution_counts` — detection pattern breakdown across all submissions
- `appeal_rate_pct` — percentage of submissions that received an appeal; a persistently high rate suggests the system is over-flagging
- `signal_disagreement_rate_pct` — percentage of submissions where LLM and stylometric scores diverged by more than 0.3; a diagnostic for cases where the signals are in genuine tension

### Provenance Certificate ✓

Creators of content classified as `"human"` or `"uncertain"` can request a verified-human provenance certificate via `POST /certificate/request`. The request requires a written attestation (`statement`) and accepts optional draft evidence.

On success, a certificate is issued and linked to the submission. Content classified as high-confidence AI is ineligible — the creator must first resolve the classification through appeal.

`GET /certificate/<certificate_id>` returns the full certificate including a `badge_description` that honestly represents what it does and doesn't guarantee: *"The creator of this content has attested that it is original human-authored work. This certificate does not guarantee authenticity but records the creator's formal declaration."*

---

## AI Usage

### Instance 1 — Detection pipeline implementation

**What I directed the AI to do:** Generate the three pipeline modules (`llm_signal.py`, `stylometry.py`, `burstiness.py`) and the confidence scorer (`scorer.py`), providing the Detection Signals section and Architecture diagram from `planning.md` as context.

**What it produced:** Working implementations of all four modules. The structure and logic matched the spec closely — the Groq prompt, the four stylometric features, the coefficient-of-variation formula for burstiness, and the weighted combiner were all correct on first generation.

**What I revised:** The stylometric sub-weights were flat (0.25 each across all four features) regardless of text length. During Milestone 4 testing I discovered that TTR is useless on short texts — it scores near-zero for every input under 150 words because words don't repeat in a 3-sentence passage. I manually added length-conditional weighting: on short texts, TTR drops from 0.35 to 0.10 and average word length rises from 0.15 to 0.40. I also revised the AI threshold from 0.75 → 0.73 after running the four test inputs and finding that formal human prose was crossing the original threshold. Neither change was in the generated output; both came from observing actual scores and reasoning about calibration.

### Instance 2 — Audit log and Flask app skeleton

**What I directed the AI to do:** Generate `db/audit_log.py` (SQLite schema plus CRUD helpers) and `app.py` (Flask routes for all five endpoints with Flask-Limiter), providing the API Surface and Rate Limiting sections from `planning.md`.

**What it produced:** A complete working implementation. All endpoints matched the API contract, the rate limits were wired correctly, and the SQLite schema included all required columns.

**What I revised:** The schema used `created_at` as the timestamp field name, but the project spec's sample audit entry used `timestamp`. I renamed the field throughout — schema definition, INSERT statement, SELECT queries, ORDER BY clause, and the variable name passed in from `app.py`. This was a small but necessary correction to ensure the log output matched the spec's documented format exactly. I also changed the appeal response key from `submitted_at` to `timestamp` for consistency with the submission record.

---

## Spec Reflection

### Where the spec helped

The false positive guidance in the hints — *"a false positive is worse than a false negative on a writing platform"* — directly shaped the threshold design. Without that explicit framing I would have split the threshold symmetrically around 0.5. Instead, the uncertain band runs from 0.40 to 0.72 (32 points on the AI side vs. 10 points on the human side), and the AI threshold of 0.73 requires genuine confidence before the system makes an accusation. The spec didn't dictate these specific numbers, but the asymmetry principle gave me a clear design criterion to test against when calibrating during Milestone 4.

### Where implementation diverged

The spec described the appeals workflow as requiring only a `content_id` and the creator's reasoning. The implementation added a `creator_id` field and a duplicate-appeal guard (the endpoint returns 409 if an appeal already exists for that content). Neither was in the spec. I added `creator_id` because the audit log wouldn't otherwise have a way to associate the appeal with the person who filed it — useful for any human moderator reviewing the queue. The duplicate guard was added after thinking through the abuse case: without it, a frustrated creator could spam the appeal endpoint and flood the moderation queue. Both changes were straightforward extensions of the spec's intent rather than departures from it, but they weren't specified and I made the call to add them.

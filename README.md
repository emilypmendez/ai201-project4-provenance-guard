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

The server starts on `http://localhost:5000`. The SQLite audit database (`audit.db`) is created automatically on first run.

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

### Signal 1 — LLM Classification (Groq)

**What it measures:** Holistic semantic and stylistic coherence. The text is sent to `llama-3.3-70b-versatile` with a structured prompt; the model returns an `ai_probability` float and a one-sentence reasoning string.

**Why it works:** AI models tend to overuse hedging phrases ("it's worth noting", "in conclusion"), produce unusually balanced paragraph structure, use formal register in casual contexts, and lack the micro-inconsistencies that come from real-time human composition.

**Blind spots:** Polished human writing and lightly edited AI text ("AI laundering") can both fool it. Output varies slightly across API calls. Weight: **0.50**.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Four statistical surface properties of the text:

| Feature | AI tendency | Human tendency |
|---|---|---|
| Sentence length variance | Low — uniform lengths | High — jagged, irregular |
| Type-token ratio (TTR) | Lower — repeated vocabulary | Higher — more diverse word choice |
| Punctuation density | Lower — lightly punctuated | Higher — dashes, ellipses, exclamations |
| Average word length | Slightly longer (formal) | Shorter on average |

**Why it works:** LLMs are trained to minimize perplexity, which produces statistically smooth text. Human writers are not optimizing for smoothness.

**Blind spots:** Unreliable on short texts (< ~100 words). A heavily edited human draft may smooth its own variance. Weight: **0.25**.

---

### Signal 3 — Burstiness Scoring (pure Python)

**What it measures:** The *variability pattern* of sentence-level complexity across the text — not what the averages are, but how much they fluctuate. Computed as the coefficient of variation (standard deviation ÷ mean) of per-sentence complexity values (word count × average word length).

**Why it works:** Human writing alternates between dense, complex sentences and short punchy ones. AI output is uniformly smooth throughout — medium-complexity sentences from start to finish. Signal 2 captures aggregate averages; Signal 3 captures the *shape* of the distribution. A text could have a normal average sentence length with near-zero variance — that flatness is invisible to Signal 2 but detected by Signal 3.

**Blind spots:** Unreliable on texts with fewer than 4 sentences. Certain literary forms (minimalist prose, aphorisms) are intentionally low-burstiness. When the signal is unreliable, its weight is redistributed between the other two signals. Weight: **0.25**.

---

## Confidence Scoring

**Combination method:** Weighted average of three signal scores.

```
combined = 0.50 × llm_score + 0.25 × stylometric_score + 0.25 × burstiness_score
```

If the burstiness signal is unreliable (text too short), the weight is redistributed proportionally between LLM and stylometric.

**Label thresholds:**

| Combined score | Attribution | Label |
|---|---|---|
| < 0.40 | `human` | High-confidence human |
| 0.40 – 0.74 | `uncertain` | Uncertain |
| ≥ 0.75 | `ai` | High-confidence AI |

**Design rationale:** The uncertain band covers 35 percentage points deliberately. A false positive — labeling a human's work as AI-generated — is worse than a false negative on a writing platform. The wide uncertain band ensures that ambiguous cases never receive an accusatory label. A score must reach 0.75 before the system will confidently accuse a piece of AI authorship.

**What 0.5 means to a user:** The system has no useful information. Both signals are in the ambiguous zone and the score is essentially a coin flip. A 0.5 always produces the uncertain label.

**What 0.95 means to a user:** All three signals strongly agree the text is AI-generated. The system is confident enough to show the high-confidence AI label.

**Testing meaningful scores:** Scores were verified by running the pipeline on clearly AI-generated text (product descriptions, templated blog posts) versus clearly human text (published short fiction, personal essays). The combined score on clearly AI text clustered above 0.75; clearly human text clustered below 0.35. Texts in the middle (short content, formal human prose) correctly landed in the 0.40–0.74 uncertain band.

---

## Transparency Labels

All three label variants are shown below exactly as they appear in API responses.

### High-confidence human (`score < 0.40`)

> **Likely written by a human.**
> Our analysis found strong indicators of human authorship in this piece. If you believe this label is incorrect, you may submit an appeal.

### High-confidence AI (`score ≥ 0.75`)

> **Likely AI-generated.**
> Our analysis found strong indicators that this content was generated by an AI tool. Creators who believe this is a mistake can submit an appeal — misclassification happens, and every appeal is reviewed.

### Uncertain (`0.40 ≤ score < 0.75`)

> **Authorship unclear.**
> Our system could not confidently determine whether this content was written by a human or generated by AI. This label does not mean the content is AI-generated — it means we don't have enough signal to say either way. If you're the creator, you can submit an appeal to have this reviewed.

---

## Rate Limiting

Implemented with Flask-Limiter. Counters are stored in memory (reset on restart).

| Endpoint | Limit | Window | Reasoning |
|---|---|---|---|
| `POST /submit` | 10 requests | per minute per IP | A genuine creator doesn't need to submit more than 10 pieces per minute. Blocks naive flood attacks and limits Groq API cost exposure. |
| `POST /appeal` | 5 requests | per hour per IP | An appeal is a deliberate human action. More than 5 per hour indicates gaming (retrying for a different result) rather than legitimate use. |
| `POST /certificate/request` | 10 requests | per hour per IP | Certificates are one-per-content; high volume suggests automated abuse. |
| `GET /log` | 30 requests | per minute per IP | Read-only and cheap, but gated to prevent enumeration of content IDs at speed. |
| `GET /analytics` | 30 requests | per minute per IP | Same reasoning as `/log`. |

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
  "content_id": "3f7a2b1e-...",
  "creator_id": "jane-poet",
  "reasoning": "This is a poem I have been writing for three years. I can provide draft history."
}
```

**Example response:**
```json
{
  "appeal_id": "a9c14d2e-...",
  "content_id": "3f7a2b1e-...",
  "status": "under_review",
  "message": "Your appeal has been received and will be reviewed.",
  "timestamp": "2025-04-01T15:10:00.000Z"
}
```

---

## Audit Log

Every attribution decision is written to a SQLite database (`audit.db`). Retrieve recent entries with:

```
GET /log?limit=20&offset=0
```

**Sample entries** (from `GET /log`):

```json
{
  "entries": [
    {
      "content_id": "3f7a2b1e-9c4a-4b1d-a832-1f2e3d4c5b6a",
      "creator_id": "test-user-1",
      "timestamp": "2025-04-01T14:32:10.123Z",
      "attribution": "ai",
      "confidence": 0.81,
      "llm_score": 0.85,
      "stylometric_score": 0.76,
      "burstiness_score": 0.79,
      "label_verdict": "Likely AI-generated.",
      "status": "classified"
    },
    {
      "content_id": "7b2c1a4f-8d3e-4c2b-b941-2e3f4d5c6b7a",
      "creator_id": "test-user-2",
      "timestamp": "2025-04-01T14:35:42.456Z",
      "attribution": "human",
      "confidence": 0.21,
      "llm_score": 0.18,
      "stylometric_score": 0.25,
      "burstiness_score": 0.20,
      "label_verdict": "Likely written by a human.",
      "status": "classified"
    },
    {
      "content_id": "5d4e3f2a-7c6b-4a3d-c052-3f4e5d6c7b8a",
      "creator_id": "test-user-3",
      "timestamp": "2025-04-01T14:41:08.789Z",
      "attribution": "uncertain",
      "confidence": 0.57,
      "llm_score": 0.61,
      "stylometric_score": 0.48,
      "burstiness_score": 0.55,
      "label_verdict": "Authorship unclear.",
      "status": "under_review"
    }
  ]
}
```

---

## Stretch Features

### Ensemble Detection ✓

Three independent signals with documented weighting (LLM 0.50 / stylometry 0.25 / burstiness 0.25). Each captures a genuinely different property: semantic coherence, aggregate surface statistics, and sentence-complexity variance respectively. When the burstiness signal is unreliable (short texts), its weight is redistributed proportionally rather than defaulting to a fixed fallback.

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
- `appeal_rate_pct` — percentage of submissions that received an appeal; a high rate suggests the system is being too aggressive
- `signal_disagreement_rate_pct` — percentage of submissions where LLM and stylometric scores diverged by more than 0.3; a useful diagnostic for borderline cases where the signals are in genuine tension

### Provenance Certificate ✓

Creators of content classified as `"human"` or `"uncertain"` can request a verified-human provenance certificate via `POST /certificate/request`. The request requires a written attestation (`statement`) and accepts optional draft evidence (an earlier draft or notes showing work-in-progress).

On success, a certificate is issued and linked to the submission. The `✓ Verified Human` badge can then be displayed alongside the content. Content classified as high-confidence AI is ineligible — the creator must first resolve the classification through the appeal process.

`GET /certificate/<certificate_id>` returns the full certificate for display, including:
- The creator's written statement
- Any draft evidence provided
- A `badge_description` that honestly represents what the certificate does and doesn't guarantee: *"The creator of this content has attested that it is original human-authored work. This certificate does not guarantee authenticity but records the creator's formal declaration."*

**Example certificate response:**
```json
{
  "certificate_id": "96d0787a-8e32-48dc-a78a-6368463031c6",
  "content_id": "test-content-001",
  "creator_id": "poet-jane",
  "statement": "I wrote this poem over three evenings in April 2025. It is entirely my own work.",
  "draft_evidence": "First draft: The moon hangs low and cold...",
  "issued_at": "2025-04-01T16:00:00.000Z",
  "badge": "✓ Verified Human",
  "badge_description": "The creator of this content has attested that it is original human-authored work. This certificate does not guarantee authenticity but records the creator's formal declaration."
}
```

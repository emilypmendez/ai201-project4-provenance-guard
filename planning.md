# Provenance Guard — Planning Document

## Architecture Narrative

A piece of text enters the system when a creator submits it to `POST /submit`. The endpoint first checks the **rate limiter**: if this IP has exceeded the allowed request window, the request is rejected before any detection work begins.

If the request passes rate limiting, the raw text is handed to the **detection pipeline**. The pipeline runs three independent signals in sequence:

1. **Signal 1 — LLM Classification (Groq):** The text is sent to `llama-3.3-70b-versatile` with a structured prompt asking the model to assess whether the writing reads as human or AI-generated and to return a probability estimate along with its reasoning. This captures *semantic and stylistic coherence* — things like whether the phrasing is hedged in ways AI tends to overuse, whether the narrative voice is consistent, and whether the structure feels templated.

2. **Signal 2 — Stylometric Heuristics:** Pure Python analysis over measurable statistical properties of the text. Computed features include sentence length variance, type-token ratio (vocabulary diversity), punctuation density, and average word length. Human writing tends to be more irregular across all of these; AI output is more uniform. This is entirely independent of Signal 1 — it doesn't ask "does this read right?" but "do the numbers look right?"

3. **Signal 3 — Burstiness Scoring:** Pure Python analysis of how much sentence-level complexity *varies* across the text, measured as the coefficient of variation of per-sentence word counts. AI text tends to be uniformly smooth throughout — medium-complexity sentences from start to finish. Human writing alternates between dense passages and short punchy ones. This is orthogonal to Signal 2: stylometry captures what the averages are; burstiness captures how much those values fluctuate over the course of the text.

The pipeline passes all three raw signal scores to the **confidence scorer**, which combines them into a single 0–1 probability estimate using a weighted average (LLM 0.50, stylometry 0.25, burstiness 0.25). The scorer applies a calibration bias that skews uncertain cases toward "human" to minimize false positives (a false accusation on a writing platform is worse than a missed detection).

The combined score feeds the **label engine**, which maps the score to one of three transparency label variants and formats the label text a platform would display to readers.

Every decision — signal scores, combined score, label assigned, content ID, timestamp — is written as a structured record to the **audit log** (SQLite).

The endpoint returns all of this to the caller: attribution result, confidence score, label text, signal breakdown, and a content ID the creator can use to file an appeal.

If a creator disputes the result, they call `POST /appeal` with their content ID and their reasoning. The appeal handler looks up the original audit record, appends the appeal text and timestamp, and flips the content's status to `"under_review"`. No automated re-classification occurs — a human moderator would act next.

---

## Detection Signals

### Signal 1: LLM Classification via Groq

**What it measures:** Holistic semantic and stylistic coherence. The model assesses whether the voice, phrasing patterns, structural choices, and register of the text are consistent with human authorship or with common AI generation artifacts.

**Why it differs between human and AI writing:** AI models tend to overuse hedging phrases ("it's worth noting", "in conclusion"), produce unusually balanced paragraph structure, use formal register even in casual contexts, and lack the micro-inconsistencies that come from real-time human composition. A large LLM can recognize these patterns because it has seen both human and AI text at scale.

**Blind spots:** The model can be fooled by lightly edited AI text ("AI laundering"), and it will misclassify highly polished human writing as AI if the writer happens to use formal, structured prose. It also has no access to metadata — it can't know whether the creator has a history of consistent style. Its output is a probability, not a fact, and it will vary slightly across API calls.

**Output:** A float in [0, 1] representing estimated probability the text is AI-generated, plus a short reasoning string.

---

### Signal 2: Stylometric Heuristics

**What it measures:** Statistical properties of the text at the surface level — independent of meaning.

| Feature | What it captures |
|---|---|
| Sentence length variance | AI tends to produce sentences of similar length; human writing is more jagged |
| Type-token ratio (TTR) | Vocabulary diversity; AI often repeats the same words, lowering TTR |
| Punctuation density | Ratio of punctuation marks to total tokens; AI text is often lightly punctuated |
| Average word length | AI slightly favors longer, more formal words |

**Why it differs between human and AI writing:** LLMs are trained to minimize perplexity, which produces text with statistically smooth distributions. Human writers are not optimizing for smoothness — they repeat words because they forget they used them, write long sentences when excited and short ones for emphasis, and punctuate idiosyncratically.

**Blind spots:** Short texts (< 100 words) produce unreliable statistics. A human writer who edits heavily may smooth their own variance. A verbose AI with temperature > 1 may produce higher variance. Stylometric heuristics work best as a corroborating signal, not a standalone classifier.

**Output:** A float in [0, 1] representing estimated probability the text is AI-generated, derived from a weighted combination of the four features scored against empirically-set thresholds.

---

### Signal 3: Burstiness Scoring

**What it measures:** The *variability pattern* of sentence complexity across the text — not what the average is, but how much it fluctuates from sentence to sentence.

Concretely: split the text into sentences, compute a complexity value for each sentence (e.g., word count, or word count × avg word length as a rough proxy for syntactic load), then calculate the **coefficient of variation** (standard deviation / mean) of those values across the full text. A high coefficient means the text is "bursty" — alternating between dense, complex sentences and short, punchy ones. A low coefficient means the text is smooth and uniform throughout.

**Why it differs between human and AI writing:** Human writers shift register naturally — a long winding sentence followed by "He didn't." AI models, by contrast, minimize perplexity globally, which tends to produce text with consistently medium-complexity sentences. The *distribution shape* of complexity differs even when the average doesn't. Signal 2 (stylometry) can miss this because it only records aggregate statistics; a text could have a "normal" average sentence length but zero variance — and that flatness is invisible to an aggregate measure.

**Why this is genuinely distinct from Signals 1 and 2:**
- Signal 1 asks "does this read like AI?" holistically — it can't decompose the text by sentence.
- Signal 2 measures *what the averages are*. Signal 3 measures *how much the values vary over time*. You could have identical Signal 2 scores with opposite Signal 3 scores.

**Blind spots:** Very short texts (< 8–10 sentences) produce unreliable variance estimates. Certain literary forms — list poems, aphorisms, minimalist prose — are intentionally low-burstiness and may score falsely high. Like Signal 2, this works best as a corroborating signal.

**Output:** A float in [0, 1] representing estimated probability the text is AI-generated. Low coefficient of variation → high AI probability; high coefficient → low AI probability. Score is normalized against empirically set thresholds.

---

## False Positive Analysis

**Scenario:** A poet who writes in a controlled, formal style — metered verse with consistent line length and elevated vocabulary — submits their work. The stylometric signal flags it as high-uniformity; the LLM signal notes the polished register. Combined score: 0.65 (leaning AI, but not confident).

**How the system handles it:**

- The confidence scorer applies its human-bias calibration. At 0.65 raw, the calibrated output might be 0.58 — still above 0.5 but significantly below any high-confidence threshold.
- The label engine assigns the **uncertain** label variant, not the AI label. The text shown to readers acknowledges that the system could not confidently classify the work and encourages the creator to appeal if the label feels wrong.
- The creator sees the uncertain label, reads that their content ID is `xyz`, and submits a `POST /appeal` with their explanation (e.g., "This is original verse I've been writing for 10 years").
- The appeal is logged, the status flips to `under_review`, and a moderator reviews.

**Design implication:** The boundary between "uncertain" and "high-confidence AI" should be set conservatively — err toward uncertainty rather than accusation. A score below ~0.75 should never produce a high-confidence AI label.

---

## API Surface

### `POST /submit`
**Purpose:** Accept content for attribution analysis.

**Request body:**
```json
{
  "content": "string — the text to analyze",
  "creator_id": "string — optional, platform user identifier"
}
```

**Response:**
```json
{
  "content_id": "uuid",
  "attribution": "human | ai | uncertain",
  "confidence": 0.0,
  "label": {
    "verdict": "string",
    "explanation": "string",
    "confidence_note": "string"
  },
  "signals": {
    "llm_score": 0.0,
    "stylometric_score": 0.0,
    "burstiness_score": 0.0
  },
  "status": "classified"
}
```

**Rate limited:** Yes (see Rate Limiting section below).

---

### `POST /appeal`
**Purpose:** Allow a creator to contest a classification.

**Request body:**
```json
{
  "content_id": "uuid",
  "creator_id": "string",
  "reasoning": "string — creator's explanation"
}
```

**Response:**
```json
{
  "content_id": "uuid",
  "appeal_id": "uuid",
  "status": "under_review",
  "message": "Your appeal has been received and will be reviewed."
}
```

---

### `GET /log`
**Purpose:** Retrieve audit log entries (admin/debug use).

**Query params:** `limit` (default 20), `offset` (default 0)

**Response:** Array of structured audit records.

---

### `GET /status/<content_id>`
**Purpose:** Check the current classification and appeal status of a submission.

**Response:** Single audit record with current status.

---

### `POST /certificate/request`
**Purpose:** Request a verified-human provenance certificate for a submission (stretch feature).

**Request body:**
```json
{
  "content_id": "uuid",
  "creator_id": "string",
  "statement": "string — creator's written attestation of human authorship",
  "draft_evidence": "string — optional earlier draft or notes showing work-in-progress"
}
```

**Eligibility:** Attribution must be `"human"` or `"uncertain"`. High-confidence AI content must go through the appeal process first.

**Response:**
```json
{
  "certificate_id": "uuid",
  "content_id": "uuid",
  "creator_id": "string",
  "issued_at": "ISO timestamp",
  "badge": "✓ Verified Human",
  "message": "Provenance certificate issued."
}
```

---

### `GET /certificate/<certificate_id>`
**Purpose:** Retrieve a provenance certificate for display on the platform.

**Response:** Full certificate record including statement and draft evidence.

---

### `GET /analytics`
**Purpose:** Summary view of detection patterns across all submissions (stretch feature — analytics dashboard).

**Response:**
```json
{
  "total_submissions": 0,
  "attribution_counts": {
    "human": 0,
    "ai": 0,
    "uncertain": 0
  },
  "appeal_rate_pct": 0.0,
  "signal_disagreement_rate_pct": 0.0,
  "avg_confidence": 0.0
}
```

`signal_disagreement_rate_pct` counts submissions where the LLM score and stylometric score differ by more than 0.3 — a useful diagnostic for cases where the signals are in genuine tension.

---

## Architecture

In the **submission flow**, a POST request enters the rate limiter and, if allowed through, passes the raw text sequentially to three independent detection signals (LLM classification, stylometric heuristics, burstiness scoring); their scores are combined by the confidence scorer into a single 0–1 probability, which the label engine maps to one of three transparency label variants before the full decision record is written to the SQLite audit log and returned to the caller. In the **appeal flow**, a POST request carrying a `content_id` and the creator's written reasoning is looked up against the audit log to confirm the submission exists, then a new appeal record is appended and the submission's status is atomically updated to `"under_review"` — no re-classification occurs, leaving the decision to a human moderator.

### Submission Flow

```
Creator
  │
  │  POST /submit { content, creator_id }
  ▼
┌─────────────────────────────────────────────┐
│  Rate Limiter                               │
│  (Flask-Limiter, per IP)                   │
└────────────────┬────────────────────────────┘
                 │ raw text passes through
                 ▼
┌─────────────────────────────────────────────┐
│  Detection Pipeline                         │
│                                             │
│  ┌──────────────────────┐                   │
│  │ Signal 1: LLM        │  ← raw text       │
│  │ (Groq llama-3.3-70b) │                   │
│  │ → llm_score: float   │  weight: 0.50     │
│  └──────────┬───────────┘                   │
│             │                               │
│  ┌──────────▼───────────┐                   │
│  │ Signal 2: Stylometry │  ← raw text       │
│  │ (pure Python)        │                   │
│  │ → stylo_score: float │  weight: 0.25     │
│  └──────────┬───────────┘                   │
│             │                               │
│  ┌──────────▼───────────┐                   │
│  │ Signal 3: Burstiness │  ← raw text       │
│  │ (pure Python)        │                   │
│  │ → burst_score: float │  weight: 0.25     │
│  └──────────┬───────────┘                   │
│             │ all three scores              │
└─────────────┼───────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────┐
│  Confidence Scorer                          │
│  weighted_avg(llm=0.6, stylo=0.4)          │
│  + human-bias calibration                  │
│  → combined_score: float [0,1]             │
└─────────────┬───────────────────────────────┘
              │ combined_score
              ▼
┌─────────────────────────────────────────────┐
│  Label Engine                               │
│  score < 0.40  → high-confidence human     │
│  0.40–0.74     → uncertain                 │
│  score ≥ 0.75  → high-confidence AI        │
│  → label: { verdict, explanation,          │
│             confidence_note }              │
└─────────────┬───────────────────────────────┘
              │ full decision record
              ▼
┌─────────────────────────────────────────────┐
│  Audit Log (SQLite)                         │
│  writes: content_id, timestamp, scores,    │
│  attribution, label, status                │
└─────────────┬───────────────────────────────┘
              │
              ▼
        JSON response to caller
```

### Appeal Flow

```
Creator
  │
  │  POST /appeal { content_id, creator_id, reasoning }
  ▼
┌─────────────────────────────────────────────┐
│  Appeal Handler                             │
│  1. Look up original record by content_id  │
│  2. Validate content_id exists             │
└─────────────┬───────────────────────────────┘
              │ original record + appeal data
              ▼
┌─────────────────────────────────────────────┐
│  Audit Log (SQLite)                         │
│  - appends appeal record (appeal_id,       │
│    reasoning, timestamp)                   │
│  - updates content status →               │
│    "under_review"                          │
└─────────────┬───────────────────────────────┘
              │
              ▼
        JSON response: { appeal_id, status: "under_review" }
```

---

## Rate Limiting

**Chosen limits:**

| Endpoint | Limit | Window |
|---|---|---|
| `POST /submit` | 10 requests | per minute per IP |
| `POST /appeal` | 5 requests | per hour per IP |
| `GET /log` | 30 requests | per minute per IP |

**Reasoning:**

- **10 submissions/minute:** A genuine creator submitting work doesn't need to submit more than 10 pieces per minute — that's already a high-volume session (e.g., bulk uploading). This blocks a naive flood attack while leaving headroom for legitimate batch use. It also limits Groq API cost exposure.
- **5 appeals/hour:** An appeal is a deliberate human action. More than 5 in an hour almost certainly indicates abuse (someone retrying hoping for a different result) rather than genuine appeals. Low enough to prevent gaming, high enough not to frustrate legitimate users.
- **30 log reads/minute:** Log access is read-only and cheap, but should still be gated so it can't be used to enumerate content IDs at high speed.

---

## Confidence Scoring Design

**What 0.5 means:** The system has no useful information — both signals are in the ambiguous zone, and the score is essentially a coin flip. A 0.5 should always produce the uncertain label, not a human or AI label.

**What 0.95 means:** Both signals strongly agree the text is AI-generated. The system is confident enough to show the high-confidence AI label.

**Calibration approach:** Raw signal scores are combined with a weighted average across three signals:

| Signal | Weight | Rationale |
|---|---|---|
| LLM classification | 0.50 | Highest weight — semantic assessment captures the most information holistically |
| Stylometric heuristics | 0.25 | Aggregate surface stats; reliable on longer texts, less so on short |
| Burstiness score | 0.25 | Structural variability; orthogonal to both others, equal weight to stylometry |

The combined score is then shifted slightly toward 0.5 on the human side — specifically, scores between 0.5 and 0.75 are treated as uncertain rather than weakly AI. This encodes the asymmetry: a false positive (accusing a human) is worse than a false negative (missing AI content).

**Label thresholds:**
- `score < 0.40` → **high-confidence human**
- `0.40 ≤ score < 0.73` → **uncertain**
- `score ≥ 0.73` → **high-confidence AI**

The wide uncertain band (35 percentage points) is intentional — it acknowledges that current detection is genuinely unreliable in the middle range.

---

## Transparency Label Variants

### High-Confidence Human
> **Likely written by a human.**
> Our analysis found strong indicators of human authorship in this piece. Confidence: [X]%. If you believe this label is incorrect, you may submit an appeal.

### High-Confidence AI
> **Likely AI-generated.**
> Our analysis found strong indicators that this content was generated by an AI tool. Confidence: [X]%. Creators who believe this is a mistake can submit an appeal — misclassification happens, and every appeal is reviewed.

### Uncertain
> **Authorship unclear.**
> Our system could not confidently determine whether this content was written by a human or generated by AI. This label does not mean the content is AI-generated — it means we don't have enough signal to say either way. Confidence: [X]%. If you're the creator, you can submit an appeal to have this reviewed.

---

## File Structure Plan

```
provenance-guard/
├── app.py                  # Flask app, route definitions
├── pipeline/
│   ├── __init__.py
│   ├── llm_signal.py       # Groq-based LLM classification
│   ├── stylometry.py       # Stylometric heuristics (aggregate stats)
│   ├── burstiness.py       # Burstiness / sentence-complexity variance
│   └── scorer.py           # Confidence combining + label engine
├── db/
│   ├── __init__.py
│   └── audit_log.py        # SQLite schema + read/write helpers
├── models/
│   └── schemas.py          # Request/response dataclasses or dicts
├── planning.md             # This file
├── README.md
├── requirements.txt
└── .env                    # GROQ_API_KEY (gitignored)
```

---

## Stretch Features (Candidates)

- [x] **Ensemble detection** — burstiness scoring added as Signal 3; weighting documented in Confidence Scoring section (LLM 0.50, stylometry 0.25, burstiness 0.25)
- [x] **Provenance certificate** — `POST /certificate/request` lets a creator submit a written attestation + optional draft evidence for any content not classified as high-confidence AI. The system issues a certificate record (certificate_id, issued_at, creator statement) stored in a `certificates` table and linked to the submission. The submission gains a `certificate_id` field and its label gains a `verified_human_badge: true` flag. `GET /certificate/<certificate_id>` returns the full certificate for display. Only content with attribution `"human"` or `"uncertain"` is eligible — a high-confidence AI classification must first be resolved via appeal before a certificate can be issued.
- [x] **Analytics dashboard** — `GET /analytics` returns detection pattern counts (human/ai/uncertain), appeal rate as a percentage, and signal disagreement rate (cases where LLM and stylometric signals diverge by > 0.3). All metrics derived from the audit log.

---

## AI Tool Plan

### M3 — Submission Endpoint + First Signal

**Spec sections to provide:** Detection Signals → Signal 1 (LLM Classification), the Architecture diagram (Submission Flow), and the API Surface section for `POST /submit`.

**What to ask the AI tool to generate:**
1. Flask app skeleton with `POST /submit` wired to a stub pipeline function.
2. `pipeline/llm_signal.py` — the Groq API call, the structured prompt instructing the model to return `{"ai_probability": float, "reasoning": str}`, and the JSON parsing with a safe fallback.

**How to verify the output:**
- Call `llm_signal.score()` directly in a Python REPL with three inputs: a passage of clearly formal AI-sounding text, a passage of casual human writing, and something ambiguous. Confirm the scores move in the expected direction before wiring the function into the endpoint.
- Hit `POST /submit` with the same three inputs and confirm the response shape matches the spec (all required keys present, confidence is a float in [0,1], label keys exist).

---

### M4 — Second + Third Signals + Confidence Scoring

**Spec sections to provide:** Detection Signals → Signal 2 (Stylometric Heuristics) + Signal 3 (Burstiness Scoring), the Confidence Scoring Design section (weights table + threshold table + calibration rationale), and the Architecture diagram.

**What to ask the AI tool to generate:**
1. `pipeline/stylometry.py` — the four feature functions (sentence length variance, TTR, punctuation density, avg word length) and their weighted combination into a single `ai_probability`.
2. `pipeline/burstiness.py` — the per-sentence complexity function and coefficient-of-variation scorer.
3. `pipeline/scorer.py` — the weighted combiner (`WEIGHTS` dict), threshold logic, `_label()` / `_attribution()` helpers, and the `run()` entry point that calls all three signals.

**What to check:**
- Run `scorer.run()` on clearly AI text (e.g., a GPT-generated product description) and clearly human text (e.g., a passage from a published novel). The combined score should be meaningfully different — not just 0.51 vs 0.52.
- Verify the uncertain band works: craft a short borderline passage and confirm it lands between 0.40 and 0.75.
- Confirm burstiness returns `reliable: False` on a text under 4 sentences and that the scorer redistributes its weight correctly in that case.

---

### M5 — Production Layer (Labels + Appeals + Audit Log)

**Spec sections to provide:** Transparency Label Variants (all three verbatim label texts), Appeals Workflow requirements, the Audit Log requirements, the Appeal Flow diagram, and the Rate Limiting section.

**What to ask the AI tool to generate:**
1. `db/audit_log.py` — SQLite schema for `submissions` and `appeals` tables, plus `log_submission()`, `log_appeal()`, `get_submission()`, `get_log()`, and `get_analytics()` helpers.
2. `POST /appeal` endpoint — body validation, `get_submission()` lookup with 404 handling, `log_appeal()` call, and the `"under_review"` response.
3. Flask-Limiter decorators on all rate-limited endpoints with the limits from the Rate Limiting section.

**How to verify:**
- Use the Flask test client to POST to `/submit`, then POST to `/appeal` with the returned `content_id`. Call `GET /status/<content_id>` and confirm `status` is `"under_review"`.
- Manually inspect `GET /log` output and confirm it includes at least 3 entries with all required fields: `content_id`, `attribution`, `confidence`, `llm_score`, `stylometric_score`, `burstiness_score`, `label_verdict`, `status`, `created_at`.
- Force each label variant by passing texts that should score below 0.40, between 0.40–0.74, and above 0.75. Confirm the `label.verdict` field matches the three verbatim strings from the spec.

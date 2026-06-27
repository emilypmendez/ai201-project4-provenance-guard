import re


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in parts if s.strip()]


def _words(text: str) -> list[str]:
    return re.findall(r"\b[a-zA-Z']+\b", text.lower())


def _type_token_ratio(words: list[str]) -> float:
    if not words:
        return 0.0
    return len(set(words)) / len(words)


def _sentence_length_variance(sentences: list[str]) -> float:
    if len(sentences) < 2:
        return 0.0
    lengths = [len(_words(s)) for s in sentences]
    mean = sum(lengths) / len(lengths)
    variance = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    return variance


def _punctuation_density(text: str) -> float:
    chars = [c for c in text if c.strip()]
    if not chars:
        return 0.0
    punct = [c for c in chars if c in ".,;:!?\"'()-—…"]
    return len(punct) / len(chars)


def _avg_word_length(words: list[str]) -> float:
    if not words:
        return 0.0
    return sum(len(w) for w in words) / len(words)


def score(text: str) -> dict:
    """
    Return {"ai_probability": float, "features": dict}.

    AI text tends toward: low sentence length variance, low TTR,
    low punctuation density, higher avg word length.
    Each feature is scored 0–1 for AI-likeness then combined.
    """
    sentences = _sentences(text)
    words = _words(text)

    sl_variance = _sentence_length_variance(sentences)
    ttr = _type_token_ratio(words)
    punct_density = _punctuation_density(text)
    avg_wl = _avg_word_length(words)

    # Score each feature for AI-likeness (1.0 = very AI-like)
    # Low variance → AI (threshold ~10 for typical prose)
    variance_score = max(0.0, 1.0 - (sl_variance / 40.0))

    # Low TTR → AI (human prose typically 0.60–0.80)
    ttr_score = max(0.0, 1.0 - (ttr / 0.75))

    # Low punctuation → AI (human prose ~0.04–0.08)
    punct_score = max(0.0, 1.0 - (punct_density / 0.06))

    # Higher avg word length → AI (human ~4.5, AI ~5.2)
    wl_score = min(1.0, max(0.0, (avg_wl - 4.0) / 2.5))

    # TTR is unreliable on short texts (< ~150 words) because almost no words repeat,
    # making all short texts score TTR > 0.75 regardless of authorship. On short inputs,
    # shift its weight to AWL, which reliably separates AI (formal, long words) from
    # casual human writing on any length text.
    word_count = len(words)
    if word_count < 150:
        w_variance, w_ttr, w_punct, w_awl = 0.30, 0.10, 0.20, 0.40
    else:
        w_variance, w_ttr, w_punct, w_awl = 0.35, 0.35, 0.15, 0.15

    ai_probability = (
        w_variance * variance_score
        + w_ttr * ttr_score
        + w_punct * punct_score
        + w_awl * wl_score
    )
    ai_probability = max(0.0, min(1.0, ai_probability))

    return {
        "ai_probability": ai_probability,
        "features": {
            "sentence_length_variance": round(sl_variance, 3),
            "type_token_ratio": round(ttr, 3),
            "punctuation_density": round(punct_density, 3),
            "avg_word_length": round(avg_wl, 3),
        },
    }

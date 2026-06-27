import re
import math


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in parts if s.strip()]


def _complexity(sentence: str) -> float:
    """Sentence complexity proxy: word count × average word length."""
    words = re.findall(r"\b[a-zA-Z']+\b", sentence)
    if not words:
        return 0.0
    avg_len = sum(len(w) for w in words) / len(words)
    return len(words) * avg_len


def _coefficient_of_variation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance) / mean


def score(text: str) -> dict:
    """
    Return {"ai_probability": float, "coefficient_of_variation": float}.

    High CoV → bursty → human-like → low AI probability.
    Low CoV → uniform → AI-like → high AI probability.
    Unreliable on texts with fewer than 8 sentences.
    """
    sentences = _sentences(text)
    complexities = [_complexity(s) for s in sentences if _complexity(s) > 0]

    if len(complexities) < 4:
        # Too short to measure reliably — return neutral
        return {"ai_probability": 0.5, "coefficient_of_variation": None, "reliable": False}

    cov = _coefficient_of_variation(complexities)

    # Empirically: AI text CoV tends to cluster around 0.2–0.4,
    # human text often 0.5–1.0+.
    # Map CoV → AI probability inversely, clamped at edges.
    ai_probability = max(0.0, min(1.0, 1.0 - (cov / 0.7)))

    return {
        "ai_probability": ai_probability,
        "coefficient_of_variation": round(cov, 4),
        "reliable": True,
    }

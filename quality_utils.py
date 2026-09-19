from __future__ import annotations

import math
import re
import unicodedata
import wave
from array import array
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

HARD_END_RE = re.compile(r"(?<=[.!?])\s+")
SOFT_END_RE = re.compile(r"(?<=[,;:])\s+")


def clean_text(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = text.replace("…", "...")
    text = text.replace("—", "-").replace("–", "-")
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_long_piece(piece: str, max_chars: int, max_words: int) -> list[str]:
    piece = piece.strip()
    if not piece:
        return []
    if len(piece) <= max_chars and len(piece.split()) <= max_words:
        return [piece]

    soft = [p.strip() for p in SOFT_END_RE.split(piece) if p.strip()]
    if len(soft) > 1:
        out: list[str] = []
        bucket = ""
        for part in soft:
            candidate = f"{bucket} {part}".strip() if bucket else part
            if bucket and (len(candidate) > max_chars or len(candidate.split()) > max_words):
                out.append(bucket)
                bucket = part
            else:
                bucket = candidate
        if bucket:
            out.append(bucket)
        if all(len(x) <= max_chars * 1.25 for x in out):
            return out

    words = piece.split()
    out = []
    bucket: list[str] = []
    chars = 0
    for word in words:
        projected = chars + (1 if bucket else 0) + len(word)
        if bucket and (projected > max_chars or len(bucket) >= max_words):
            out.append(" ".join(bucket))
            bucket = []
            chars = 0
        bucket.append(word)
        chars += (1 if chars else 0) + len(word)
    if bucket:
        out.append(" ".join(bucket))
    return out


def split_text_smart(text: str, max_chars: int = 140, max_words: int = 30) -> list[str]:
    """Sentence-first TTS chunking.

    The important difference from v1.3 is that the limit is character-first, not
    a 55-word bucket. OmniVoice failures are much easier to isolate when a chunk
    contains roughly one sentence / 70-140 characters instead of several clauses.
    """
    text = clean_text(text)
    if not text:
        return []
    max_chars = max(70, min(240, int(max_chars)))
    max_words = max(12, min(50, int(max_words)))

    sentences = [s.strip() for s in HARD_END_RE.split(text) if s.strip()]
    out: list[str] = []
    for sentence in sentences:
        out.extend(_split_long_piece(sentence, max_chars=max_chars, max_words=max_words))
    return [x.strip() for x in out if x.strip()]


def split_text_aggressive(text: str, max_chars: int = 82, max_words: int = 18) -> list[str]:
    """Fallback splitter used only after repeated QC failures."""
    text = clean_text(text)
    if not text:
        return []
    max_chars = max(45, min(120, int(max_chars)))
    max_words = max(8, min(24, int(max_words)))

    # Commas are intentional rescue boundaries. This turns e.g.
    # "After turning himself in, he is locked..." into two independent renders.
    pieces = re.split(r"(?<=[,;:])\s+|\s+(?=(?:and|but|while|because|then|so)\b)", text, flags=re.I)
    out: list[str] = []
    for piece in pieces:
        out.extend(_split_long_piece(piece, max_chars=max_chars, max_words=max_words))
    return [x.strip() for x in out if x.strip()]


def _norm_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(text)).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_similarity(expected: str, heard: str) -> dict[str, float]:
    a = _norm_for_match(expected)
    b = _norm_for_match(heard)
    if not a or not b:
        return {"score": 0.0, "char_ratio": 0.0, "word_f1": 0.0}

    char_ratio = SequenceMatcher(None, a, b).ratio()
    ca, cb = Counter(a.split()), Counter(b.split())
    overlap = sum((ca & cb).values())
    precision = overlap / max(1, sum(cb.values()))
    recall = overlap / max(1, sum(ca.values()))
    word_f1 = 0.0 if precision + recall == 0 else (2 * precision * recall / (precision + recall))

    # Character order catches bizarre phonetic hallucinations. Word F1 keeps the
    # score tolerant to small Whisper mistakes on names and punctuation.
    score = 0.58 * char_ratio + 0.42 * word_f1
    return {
        "score": round(float(score), 4),
        "char_ratio": round(float(char_ratio), 4),
        "word_f1": round(float(word_f1), 4),
    }



def speech_safe_variant(text: str) -> str:
    """Very conservative spoken-only fallbacks used after repeated QC failure.

    These do not touch the saved script. They only replace constructions that have
    already failed several independent generations.
    """
    original = clean_text(text)
    rules = [
        (r"\bAfter turning himself in\b", "After he turns himself in"),
        (r"\bafter turning himself in\b", "after he turns himself in"),
        (r"\bAfter turning herself in\b", "After she turns herself in"),
        (r"\bafter turning herself in\b", "after she turns herself in"),
        (r"\bAfter turning themselves in\b", "After they turn themselves in"),
        (r"\bafter turning themselves in\b", "after they turn themselves in"),
    ]
    value = original
    for pattern, replacement in rules:
        value = re.sub(pattern, replacement, value)
    return value

def wav_sanity(path: str | Path, text: str, speed: float = 1.0) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "reason": "wav_missing", "duration": 0.0, "rms": 0.0, "peak": 0.0, "clip_ratio": 0.0}
    try:
        with wave.open(str(p), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.getnframes()
            raw = wf.readframes(frames)
    except Exception as exc:
        return {"ok": False, "reason": f"wav_unreadable:{exc}", "duration": 0.0, "rms": 0.0, "peak": 0.0, "clip_ratio": 0.0}

    if width != 2 or rate <= 0 or frames <= 0:
        return {"ok": False, "reason": "wav_format", "duration": 0.0, "rms": 0.0, "peak": 0.0, "clip_ratio": 0.0}

    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        return {"ok": False, "reason": "wav_empty", "duration": 0.0, "rms": 0.0, "peak": 0.0, "clip_ratio": 0.0}
    if channels > 1:
        samples = array("h", samples[::channels])

    n = len(samples)
    peak_i = max(abs(int(x)) for x in samples)
    rms_i = math.sqrt(sum(int(x) * int(x) for x in samples) / max(1, n))
    clip_ratio = sum(1 for x in samples if abs(int(x)) >= 32600) / max(1, n)
    duration = frames / float(rate)
    peak = peak_i / 32768.0
    rms = rms_i / 32768.0

    words = max(1, len(clean_text(text).split()))
    safe_speed = max(0.65, min(1.5, float(speed or 1.0)))
    min_duration = max(0.24, words / (6.8 * safe_speed))
    max_duration = max(2.2, words / (0.95 * safe_speed) + 1.8)

    reason = ""
    ok = True
    if rms < 0.0022 or peak < 0.012:
        ok, reason = False, "near_silence"
    elif clip_ratio > 0.08:
        ok, reason = False, "heavy_clipping"
    elif duration < min_duration:
        ok, reason = False, "too_short"
    elif duration > max_duration:
        ok, reason = False, "too_long"

    return {
        "ok": ok,
        "reason": reason,
        "duration": round(duration, 4),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "clip_ratio": round(clip_ratio, 6),
        "min_duration": round(min_duration, 4),
        "max_duration": round(max_duration, 4),
    }


def qc_threshold(text: str) -> float:
    words = len(clean_text(text).split())
    if words <= 5:
        return 0.70
    if words <= 9:
        return 0.74
    return 0.78


def qc_soft_floor(text: str) -> float:
    words = len(clean_text(text).split())
    return 0.62 if words <= 6 else 0.66

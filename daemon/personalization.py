"""
personalization.py

A small, purely local word-frequency model for predictive typing. Learns which
words THIS user tends to type after a given 1-2 word context, and uses that to
rerank/correct the local LLM's first suggested word when it's confident enough.
No cloud calls, no LLM involved -- just counting.

Storage: a single JSON file of n-gram -> next-word counts. Never leaves the
machine, never sent anywhere. Bootstrapped once from the user's own past chat
messages (their own words, already stored locally), and reinforced over time
every time a suggestion is actually Tab-accepted in the predictive typing UI.
"""

import json
import re
import threading
from pathlib import Path

NGRAM_STORE_PATH = Path.home() / "Mira" / "daemon" / "personal_ngrams.json"

# below this many observations for a context, we don't trust it enough to override the LLM
MIN_CONFIDENT_COUNT = 3
MIN_CONFIDENT_RATIO = 0.5  # this word must be at least half of all observed continuations

_lock = threading.Lock()
_data = None  # lazy-loaded


def _load():
    global _data
    if _data is not None:
        return _data
    if NGRAM_STORE_PATH.exists():
        try:
            _data = json.loads(NGRAM_STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            _data = {}
    else:
        _data = {}
    _data.setdefault("bigram_next", {})   # "word1" -> {"word2": count}
    _data.setdefault("trigram_next", {})  # "word1 word2" -> {"word3": count}
    _data.setdefault("bootstrapped", False)
    return _data


def _save():
    NGRAM_STORE_PATH.write_text(json.dumps(_data, ensure_ascii=False))


def tokenize(text: str):
    return re.findall(r"[a-zA-Z0-9']+", text.lower())


def ingest_text(text: str):
    """Updates n-gram counts from a piece of the user's own writing."""
    tokens = tokenize(text)
    if len(tokens) < 2:
        return

    with _lock:
        data = _load()
        for i in range(len(tokens) - 1):
            w1, w2 = tokens[i], tokens[i + 1]
            data["bigram_next"].setdefault(w1, {})
            data["bigram_next"][w1][w2] = data["bigram_next"][w1].get(w2, 0) + 1

            if i + 2 < len(tokens):
                w3 = tokens[i + 2]
                key = f"{w1} {w2}"
                data["trigram_next"].setdefault(key, {})
                data["trigram_next"][key][w3] = data["trigram_next"][key].get(w3, 0) + 1
        _save()


def record_accepted_word(context_before: str, accepted_word: str):
    """Called when the user actually Tab-accepts a suggested word -- a strong,
    directly-relevant training signal, weighted the same as any other observation
    but specifically tied to predictive-typing usage rather than general writing."""
    tokens = tokenize(context_before)
    word = tokenize(accepted_word)
    if not word:
        return
    word = word[0]

    with _lock:
        data = _load()
        if tokens:
            w1 = tokens[-1]
            data["bigram_next"].setdefault(w1, {})
            data["bigram_next"][w1][word] = data["bigram_next"][w1].get(word, 0) + 1
        if len(tokens) >= 2:
            key = f"{tokens[-2]} {tokens[-1]}"
            data["trigram_next"].setdefault(key, {})
            data["trigram_next"][key][word] = data["trigram_next"][key].get(word, 0) + 1
        _save()


def predict_next_word(context_text: str):
    """Returns (word, confidence) for the most likely next word given the trailing
    context, or (None, 0) if there's not enough personal data to be confident.
    Trigram context (last 2 words) is preferred over bigram (last 1 word)."""
    tokens = tokenize(context_text)
    if not tokens:
        return None, 0.0

    with _lock:
        data = _load()

        if len(tokens) >= 2:
            key = f"{tokens[-2]} {tokens[-1]}"
            counts = data["trigram_next"].get(key)
            result = _best_candidate(counts)
            if result:
                return result

        key = tokens[-1]
        counts = data["bigram_next"].get(key)
        result = _best_candidate(counts)
        if result:
            return result

    return None, 0.0


def _best_candidate(counts):
    if not counts:
        return None
    total = sum(counts.values())
    best_word, best_count = max(counts.items(), key=lambda kv: kv[1])
    if best_count < MIN_CONFIDENT_COUNT:
        return None
    ratio = best_count / total
    if ratio < MIN_CONFIDENT_RATIO:
        return None
    return best_word, ratio


def bootstrap_from_chat_history(force: bool = False):
    """One-time ingestion of the user's own past chat messages so personalization
    doesn't start completely cold. Safe to call on every daemon startup -- only
    actually does the work once (tracked via the 'bootstrapped' flag) unless force=True."""
    with _lock:
        data = _load()
        if data.get("bootstrapped") and not force:
            return

    history_dir = Path.home() / "Mira" / "daemon" / "chat_history"
    if not history_dir.exists():
        return

    for f in history_dir.glob("*.json"):
        try:
            messages = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for m in messages:
            if m.get("role") == "user" and m.get("content"):
                ingest_text(m["content"])

    with _lock:
        data = _load()
        data["bootstrapped"] = True
        _save()

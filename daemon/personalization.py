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

import bisect
import json
import re
import threading
from pathlib import Path

NGRAM_STORE_PATH = Path.home() / "Mira" / "daemon" / "personal_ngrams.json"
SYSTEM_DICTIONARY_PATH = Path("/usr/share/dict/words")

# below this many observations for a context, we don't trust it enough to override the LLM
MIN_CONFIDENT_COUNT = 3
MIN_CONFIDENT_RATIO = 0.5  # this word must be at least half of all observed continuations

_lock = threading.Lock()
_data = None  # lazy-loaded

# in-word (letter-level) completion: a static sorted word list (system dictionary,
# ~236k words on macOS) enables fast prefix lookups via bisect, no trie needed.
# Built once, lazily, on first use -- not persisted, cheap to rebuild each daemon
# start. Kept separate from the n-gram store above: this is about completing the
# CURRENT word being typed, not predicting the next one.
_word_list_lock = threading.Lock()
_sorted_words = None


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
    _data.setdefault("unigram_counts", {})  # "word" -> count, used to rank in-word completions
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
        for w in tokens:
            data["unigram_counts"][w] = data["unigram_counts"].get(w, 0) + 1
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
        data["unigram_counts"][word] = data["unigram_counts"].get(word, 0) + 1
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


def record_accepted_completion(word: str):
    """Called when an in-word (letter-level) completion is Tab-accepted --
    boosts that word's personal frequency so it ranks higher next time."""
    word = tokenize(word)
    if not word:
        return
    word = word[0]
    with _lock:
        data = _load()
        data["unigram_counts"][word] = data["unigram_counts"].get(word, 0) + 1
        _save()


def _load_word_list():
    """Word list ranked by real-world usage frequency (wordfreq's corpus-based
    data), not just "is this a valid word" -- the raw macOS system dictionary
    (/usr/share/dict/words, an unabridged Webster's) was tried first and gave
    poor completions in practice: with no frequency signal, a bare shortest-
    match tie-break picked obscure words over common ones ("environ" ->
    "environs" instead of "environment", "compl" -> "comply" instead of
    "complete"). wordfreq's zipf_frequency fixes that by ranking candidates on
    genuine corpus frequency, with the user's own vocabulary folded in too so
    names/jargon they actually use (which a general corpus won't know) are
    still completable."""
    global _sorted_words
    with _word_list_lock:
        if _sorted_words is not None:
            return _sorted_words
        from wordfreq import top_n_list

        words = set(top_n_list("en", 60000))
        with _lock:
            data = _load()
            words.update(data.get("unigram_counts", {}).keys())
        _sorted_words = sorted(words)
        return _sorted_words


def complete_word(partial: str):
    """Returns the best full-word completion for a word currently being typed
    (letter-level, e.g. "environ" -> "environment"), or None if there's nothing
    good to suggest. Uses bisect for fast prefix lookup against a frequency-
    ranked English word list + the user's own vocabulary, ranked by genuine
    corpus frequency (wordfreq) with personal usage as a tie-break boost, so
    common words win over obscure ones with the same prefix."""
    from wordfreq import zipf_frequency

    partial = partial.lower()
    if len(partial) < 3:
        return None  # too short -- far too many candidates to be useful

    words = _load_word_list()
    lo = bisect.bisect_left(words, partial)
    hi = bisect.bisect_left(words, partial + "￿")
    candidates = [w for w in words[lo:hi] if w != partial]
    if not candidates:
        return None

    with _lock:
        data = _load()
        unigram_counts = data.get("unigram_counts", {})

    # if the partial is already a complete, personally-common word on its own,
    # don't force a longer completion unless something clearly outranks it
    own_count = unigram_counts.get(partial, 0)

    def rank(w):
        # personal usage is a real signal but shouldn't drown out corpus
        # frequency entirely -- treat it as a modest boost, not the whole score
        personal_boost = min(unigram_counts.get(w, 0), 5) * 0.3
        return -(zipf_frequency(w, "en") + personal_boost)

    best = min(candidates, key=rank)
    best_count = unigram_counts.get(best, 0)

    if own_count > 0 and best_count <= own_count:
        return None

    # cap how much extra a single completion suggests -- very long completions
    # for a short partial are usually more distracting than helpful
    if len(best) - len(partial) > 12:
        return None

    return best[len(partial):]  # just the remaining suffix to insert


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

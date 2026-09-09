"""Mira's long-term memory.

Two things live here, deliberately in one store:

  * what Mira knows about her owner and their work (products, org, pricing,
    people, preferences) -- the knowledge base that makes her more than a
    generic LLM wrapper.
  * what she learns from daily conversation, appended over time.

Retrieval is keyword + recency scoring over a JSON file, not embeddings. That
is a deliberate choice: this has to stay lightweight and dependency-free for an
open-source release, and at the scale one person's memory actually reaches
(hundreds to low thousands of facts) a good lexical match plus pinning beats a
vector index that costs a model load and a native dependency to query.
"""

import json
import re
import uuid
import datetime
import threading
from pathlib import Path

MEMORY_DIR = Path.home() / "Mira" / "daemon" / "memory_store"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
FACTS_PATH = MEMORY_DIR / "facts.json"

# Categories are a fixed vocabulary so the model can't invent a hundred
# near-duplicates ("preference", "preferences", "user_preference"...).
CATEGORIES = [
    "identity",     # who the owner is, role, contact style
    "business",     # their company: org, pricing, customers, deals
    "product",      # their products: specs, positioning
    "preference",   # how they want Mira to behave / communicate
    "people",       # colleagues, contacts and their context
    "project",      # ongoing work, deadlines, threads
    "fact",         # anything else worth keeping
]

MAX_FACT_CHARS = 600
_lock = threading.Lock()

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "and", "or",
    "of", "to", "in", "on", "for", "with", "at", "by", "from", "as", "it",
    "this", "that", "these", "those", "i", "me", "my", "we", "our", "you",
    "your", "he", "she", "they", "them", "his", "her", "their", "do", "does",
    "did", "have", "has", "had", "what", "which", "who", "when", "where",
    "how", "why", "can", "will", "would", "should", "about", "tell", "give",
}


def _load() -> list:
    if not FACTS_PATH.exists():
        return []
    try:
        data = json.loads(FACTS_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(facts: list):
    tmp = FACTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    tmp.replace(FACTS_PATH)  # atomic -- a crash mid-write must not lose memory


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def all_facts() -> list:
    return sorted(_load(), key=lambda f: (not f.get("pinned"), f.get("updated_at", "")), reverse=False)


def add_fact(text: str, category: str = "fact", source: str = "manual",
             pinned: bool = False) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty fact")
    if len(text) > MAX_FACT_CHARS:
        text = text[:MAX_FACT_CHARS]
    if category not in CATEGORIES:
        category = "fact"

    with _lock:
        facts = _load()

        # Near-duplicate guard. Without this, every conversation that restates
        # a standing fact ("I run X") would append another copy and slowly
        # crowd the retrieved context with the same sentence.
        norm = _normalize(text)
        incoming = _tokens(text)
        for existing in facts:
            if _normalize(existing["text"]) == norm:
                existing["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                existing["hits"] = existing.get("hits", 0) + 1
                _save(facts)
                return existing
            other = _tokens(existing["text"])
            if incoming and other:
                overlap = len(incoming & other) / max(len(incoming | other), 1)
                if overlap > 0.82:
                    # treat as a refinement of the same fact rather than a new one
                    existing["text"] = text
                    existing["category"] = category
                    existing["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                    _save(facts)
                    return existing

        fact = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "category": category,
            "source": source,
            "pinned": bool(pinned),
            "hits": 0,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        facts.append(fact)
        _save(facts)
        return fact


def update_fact(fact_id: str, text: str = None, category: str = None,
                pinned: bool = None) -> dict:
    with _lock:
        facts = _load()
        for f in facts:
            if f["id"] != fact_id:
                continue
            if text is not None:
                f["text"] = text.strip()[:MAX_FACT_CHARS]
            if category is not None and category in CATEGORIES:
                f["category"] = category
            if pinned is not None:
                f["pinned"] = bool(pinned)
            f["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save(facts)
            return f
    raise KeyError(fact_id)


def delete_fact(fact_id: str) -> bool:
    with _lock:
        facts = _load()
        remaining = [f for f in facts if f["id"] != fact_id]
        if len(remaining) == len(facts):
            return False
        _save(remaining)
        return True


def search_facts(query: str, limit: int = 12) -> list:
    """Rank facts against a query by lexical overlap, then recency."""
    facts = _load()
    if not facts:
        return []

    q = _tokens(query)
    if not q:
        return sorted(facts, key=lambda f: f.get("updated_at", ""), reverse=True)[:limit]

    scored = []
    for f in facts:
        ft = _tokens(f["text"])
        if not ft:
            continue
        overlap = len(q & ft)
        if not overlap:
            continue
        # normalise by fact length so a long rambling fact doesn't win on
        # sheer word count alone
        score = overlap / (len(ft) ** 0.5)
        if f.get("pinned"):
            score += 1.0
        scored.append((score, f.get("updated_at", ""), f))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [f for _, _, f in scored[:limit]]


def build_memory_context(query: str = "", max_facts: int = 14) -> str:
    """The memory block injected into a model's system prompt.

    Pinned facts always travel; the rest are whatever is most relevant to this
    particular message. Returns "" when there is nothing worth sending, so
    callers can skip the whole section rather than emit an empty header.
    """
    facts = _load()
    if not facts:
        return ""

    pinned = [f for f in facts if f.get("pinned")]
    relevant = [f for f in search_facts(query, limit=max_facts) if not f.get("pinned")]

    chosen = pinned + relevant
    if not chosen:
        # no lexical hit -- fall back to the most recently touched, so Mira is
        # never completely amnesiac just because the wording didn't match
        chosen = sorted(facts, key=lambda f: f.get("updated_at", ""), reverse=True)[:6]

    chosen = chosen[:max_facts + len(pinned)]

    by_cat = {}
    for f in chosen:
        by_cat.setdefault(f["category"], []).append(f["text"])

    lines = []
    for cat in CATEGORIES:
        if cat in by_cat:
            lines.append(f"{cat.upper()}:")
            lines.extend(f"  - {t}" for t in by_cat[cat])
    return "\n".join(lines)


# ---------- automatic extraction from conversation ----------

EXTRACTION_PROMPT = """From the exchange below, extract durable facts worth remembering long-term about the user, their business, products, people, or stated preferences.

Reply with ONLY a JSON array. Each item: {{"text": "...", "category": "one of: {categories}"}}

Rules:
- Only things that stay true beyond this conversation. Not questions, not
  small talk, not one-off requests, not anything Mira herself said.
- Write each fact standalone and third-person ("The user prefers ...", using
  their name if known -- not "you prefer ..."), so it still reads correctly
  months later out of context.
- If there is nothing durable, reply with exactly: []
- At most 3 facts.

USER: {user}
ASSISTANT: {assistant}
"""


def extract_facts_from_exchange(user_msg: str, assistant_msg: str, llm_call) -> list:
    """Pull durable facts out of one exchange and store them.

    `llm_call` takes a prompt and returns raw model text. Failures are
    swallowed on purpose: memory extraction is a background nicety and must
    never break or delay the reply the user is actually waiting on.
    """
    if not user_msg or len(user_msg.strip()) < 12:
        return []

    prompt = EXTRACTION_PROMPT.format(
        categories=", ".join(CATEGORIES),
        user=user_msg[:1500],
        assistant=(assistant_msg or "")[:800],
    )

    try:
        raw = llm_call(prompt)
    except Exception:
        return []

    if not raw:
        return []

    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    stored = []
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if len(text) < 8:
            continue
        try:
            stored.append(add_fact(text, item.get("category", "fact"), source="conversation"))
        except ValueError:
            pass
    return stored


# ---------- first-run seeding ----------

# Seeds a brand-new install with facts about MIRA HERSELF only -- deliberately
# nothing about a specific owner. This file ships in an open-source repo, so
# hardcoding one person's name, company and products here would hand every
# other user a memory pre-loaded with a stranger's business. Everything about
# the actual owner is learned from conversation (or typed into the Memory view)
# and lives in the gitignored store.
SEED_FACTS = [
    ("Mira is a local-first, OS-integrated AI assistant running on the user's own Mac.",
     "identity", True),
    ("Mira can act on the machine directly -- reminders, apps and music, web search, "
     "Dump Box capture, calendar and mail -- and should use those abilities rather than "
     "describing them.", "identity", True),
    ("Mira should keep answers direct and concise, without preamble or filler.",
     "preference", True),
]


def seed_if_empty() -> int:
    """Give Mira a baseline identity on first run.

    Only ever runs against a completely empty store, so it can't overwrite or
    duplicate anything the user has since taught her.
    """
    if _load():
        return 0
    count = 0
    for text, category, pinned in SEED_FACTS:
        try:
            add_fact(text, category, source="seed", pinned=pinned)
            count += 1
        except ValueError:
            pass
    return count

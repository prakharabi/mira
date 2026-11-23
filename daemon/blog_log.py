"""Recently published blog posts (KrishiVerse, NeutronX, Voltros), as reported by the n8n blog
automation through /notify/blog-published. Kept so the morning digest can
mention what went out since the last one -- the Telegram notice at publish
time is easy to miss by the next morning."""
import datetime
import json
from pathlib import Path

BLOG_LOG_PATH = Path.home() / "Mira" / "daemon" / "memory_store" / "blog_log.json"
MAX_ENTRIES = 30
# n8n sends brand: "neutronx" / "voltros"; KrishiVerse's workflow sends none.
BRANDS = {"krishiverse": ("KrishiVerse", "🌾"), "neutronx": ("NeutronX", "🧾"), "voltros": ("Voltros", "⚡")}


def brand_key(brand: str = "", url: str = "") -> str:
    """The brand's key; falls back to the post's site when a stored entry has no brand."""
    brand = (brand or "").strip().lower()
    if brand in BRANDS:
        return brand
    for key in ("neutronx", "voltros"):
        if f"{key}.in" in (url or ""):
            return key
    return "krishiverse"


def _load() -> list:
    if not BLOG_LOG_PATH.exists():
        return []
    try:
        data = json.loads(BLOG_LOG_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def record(title: str, summary: str, url: str, url_hi: str = "", hindi_error: str = "", brand: str = "") -> dict:
    entry = {
        "brand": brand_key(brand, url),
        "title": title,
        "summary": summary,
        "url": url,
        "url_hi": url_hi,
        "hindi_error": hindi_error,
        "published_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    entries = (_load() + [entry])[-MAX_ENTRIES:]
    BLOG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOG_LOG_PATH.write_text(json.dumps(entries, indent=1, ensure_ascii=False))
    return entry


def since(timestamp: str | None) -> list:
    """Entries published after `timestamp` (ISO string), oldest first. With
    no timestamp yet, only the last day's -- a first digest shouldn't dump the
    whole history."""
    entries = _load()
    if not timestamp:
        timestamp = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
    return [e for e in entries if e.get("published_at", "") > timestamp]

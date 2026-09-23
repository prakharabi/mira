"""Web search and page reading.

Deliberately provider-agnostic and key-free by default. The previous
implementation depended entirely on a Tavily API key, and when that key stopped
working Mira simply lost internet access with no fallback. Search now tries
several sources in order and returns from the first that answers, so no single
dead key or blocked endpoint takes the capability down:

  1. DuckDuckGo via `ddgs` -- no key, no quota, real web results
  2. DuckDuckGo API        -- official instant-answer endpoint, good for definitions
  3. Wikipedia             -- reliable for entities and factual lookups
  4. Tavily                -- only if the user has configured a key

Scraping DuckDuckGo's lite HTML directly was tried first and abandoned: it now
answers 202 with an anti-bot challenge instead of results. `ddgs` is a small
pure-Python package that maintains the token handshake that unblocks it, which
is not worth reimplementing (or re-fixing every time they change it) here.
"""

import html
import re
import urllib.parse

import requests

TIMEOUT = 12
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MAX_PAGE_CHARS = 6000


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _unwrap_ddg(url: str) -> str:
    """DuckDuckGo wraps outbound links in a redirect carrying the real URL."""
    if "duckduckgo.com/l/" in url or url.startswith("//duckduckgo.com/l/"):
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            return urllib.parse.unquote(m.group(1))
    if url.startswith("//"):
        return "https:" + url
    return url


def _search_ddgs(query: str, max_results: int):
    from ddgs import DDGS  # imported lazily so a missing package degrades to the other providers
    with DDGS() as ddgs:
        hits = list(ddgs.text(query, max_results=max_results))
    return [{
        "title": h.get("title", ""),
        "url": h.get("href", ""),
        "snippet": (h.get("body") or "")[:400],
    } for h in hits]


def _search_ddg_api(query: str, max_results: int):
    resp = requests.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()

    results = []
    if data.get("AbstractText"):
        results.append({
            "title": data.get("Heading") or query,
            "url": data.get("AbstractURL", ""),
            "snippet": data["AbstractText"][:400],
        })
    for topic in (data.get("RelatedTopics") or []):
        if len(results) >= max_results:
            break
        if isinstance(topic, dict) and topic.get("Text"):
            results.append({
                "title": topic["Text"][:80],
                "url": topic.get("FirstURL", ""),
                "snippet": topic["Text"][:400],
            })
    return results


def _search_wikipedia(query: str, max_results: int):
    resp = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query,
                "format": "json", "srlimit": max_results},
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    hits = resp.json().get("query", {}).get("search", []) or []
    return [{
        "title": h.get("title", ""),
        "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(h.get("title", "").replace(" ", "_")),
        "snippet": _clean(h.get("snippet", ""))[:400],
    } for h in hits]


def _search_tavily(query: str, max_results: int, api_key: str):
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    return [{
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "snippet": (r.get("content") or "")[:400],
    } for r in (resp.json().get("results") or [])]


def search(query: str, max_results: int = 5, tavily_key: str = "") -> dict:
    """Search the web. Returns {results: [...], provider: str, error: str}."""
    query = (query or "").strip()
    if not query:
        return {"results": [], "provider": "", "error": "empty query"}

    providers = [
        ("duckduckgo", lambda: _search_ddgs(query, max_results)),
        ("duckduckgo-api", lambda: _search_ddg_api(query, max_results)),
        ("wikipedia", lambda: _search_wikipedia(query, max_results)),
    ]
    # Tavily is a paid API -- it belongs at the back of the queue as a last
    # resort, only reached when every free source above came back empty, not
    # ahead of them. This used to be providers.insert(0, ...), which put it
    # FIRST whenever a key was configured -- exactly backwards from the
    # provider-agnostic, key-free-by-default intent described above, and the
    # reason a configured Tavily key was being billed on every search instead
    # of just the rare one DuckDuckGo/Wikipedia couldn't answer.
    if tavily_key:
        providers.append(("tavily", lambda: _search_tavily(query, max_results, tavily_key)))

    errors = []
    for name, fn in providers:
        try:
            results = fn()
            if results:
                return {"results": results, "provider": name, "error": ""}
        except Exception as e:
            errors.append(f"{name}: {e}")

    return {"results": [], "provider": "", "error": "; ".join(errors) or "no results"}


def fetch_page(url: str, max_chars: int = MAX_PAGE_CHARS) -> dict:
    """Read a page and return its visible text, so Mira can actually open a
    search result rather than only seeing the snippet."""
    if not re.match(r"^https?://", url or ""):
        return {"error": "url must start with http:// or https://", "text": ""}

    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return {"error": str(e), "text": ""}

    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "text": ""}

    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        return {"error": f"unsupported content type: {ctype}", "text": ""}

    body = resp.text
    body = re.sub(r"<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ",
                  body, flags=re.DOTALL | re.IGNORECASE)

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE)
    if m:
        title = _clean(m.group(1))

    text = _clean(body)
    return {"title": title, "url": url, "text": text[:max_chars], "error": ""}


def format_results_for_model(results: list) -> str:
    if not results:
        return "No results."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title','')}\n   {r.get('url','')}\n   {r.get('snippet','')}")
    return "\n".join(lines)

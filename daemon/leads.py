"""Lead lists -- businesses of one type in one area, found for outreach.

"Find dentists in Indiranagar" becomes a saved list of businesses with their
phone, email, website and socials, which later phases (outreach, proposals)
work from. Two sources, behind one interface:

  * gosom/google-maps-scraper, run natively as a one-shot child process. Free
    and the most complete, but it drives a headless Chromium -- so it is only
    ever started for the length of one search, with one browser (-c 1) at low
    CPU priority, and exits on its own. Nothing of it runs while idle. Built by
    scripts/install_scraper.sh; Docker was deliberately avoided, since its VM
    holds gigabytes of RAM on a Mac whether or not anything is scraping.
  * Google's Places API (New), when the user has saved their own key. Nothing
    runs locally at all and it's within Google's terms, but it returns no
    emails and bills per request past a monthly free allowance.

Emails and social links are found the same way for both: one plain HTTP fetch
of each business's own website (plus its contact page when the homepage has
no address). That is lighter than having the scraper open every site in its
browser, and it gives both sources identical columns.

Only one search runs at a time. Google rate-limits an IP that scrapes hard,
and a second Chromium would be exactly the load this design is avoiding.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

LEADS_DIR = Path.home() / "Mira" / "daemon" / "leads"
SCRAPER_LOG = LEADS_DIR / "scraper.log"
EXPORT_DIR = Path.home() / "Downloads"

# install_scraper.sh builds into daemon/bin/; MIRA_GMAPS_SCRAPER overrides it
# for anyone who built or downloaded the binary somewhere else.
_BUNDLED_SCRAPER = Path(__file__).resolve().parent / "bin" / "google-maps-scraper"

DEFAULT_DEPTH = 5          # how far down Maps' results list to scroll; ~5 is 60-120 places
MAX_DEPTH = 15             # past this Google starts rate-limiting a single IP
SCRAPE_TIMEOUT_SECONDS = 15 * 60
INACTIVITY_EXIT = "3m"     # scraper exits on its own once nothing new has arrived for this long

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_PAGE_SIZE = 20
PLACES_MAX_PAGES = 3       # the API stops paging at 60 results per query anyway
# Phone, website and rating are what make a place a lead, and asking for any
# of them bills the request at the Enterprise tier -- so there is no cheaper
# field mask that's still useful. Each page is one billed request.
PLACES_FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber",
    "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.primaryTypeDisplayName", "places.googleMapsUri",
    "places.businessStatus", "nextPageToken",
])

SITE_FETCH_TIMEOUT = 8
SITE_FETCH_MAX_BYTES = 400_000
SITE_WORKERS = 4
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Where a lead is in the outreach pipeline. Only "new" is set in this phase;
# the rest exist so outreach can move leads along without a data migration.
STAGES = ("new", "contacted", "replied", "proposal_sent", "won", "lost", "do_not_contact")

LEAD_COLUMNS = ["name", "phone", "emails", "website", "category", "address",
                "rating", "reviews", "instagram", "facebook", "linkedin",
                "maps_url", "stage"]

_lock = threading.Lock()          # guards the files in LEADS_DIR
_job_lock = threading.Lock()      # guards _current
_current = {"list_id": None, "proc": None, "cancelled": False}


def log(message: str):
    print(f"[leads] {message}", flush=True)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------- storage ----------

def _path(list_id: str) -> Path:
    # ids are generated here, but they also arrive in URLs -- never let one
    # walk out of the leads directory
    if not re.fullmatch(r"[a-f0-9]{8,32}", list_id or ""):
        raise ValueError("invalid list id")
    return LEADS_DIR / f"{list_id}.json"


def _save(record: dict):
    LEADS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(record["id"])
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        tmp.replace(path)  # atomic -- a crash mid-write must not lose a finished search


def _load(list_id: str):
    try:
        path = _path(list_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _all_records() -> list:
    if not LEADS_DIR.exists():
        return []
    records = []
    for p in LEADS_DIR.glob("*.json"):
        try:
            records.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records


def _summary(record: dict) -> dict:
    leads = record.get("leads", [])
    return {
        "id": record["id"],
        "name": record.get("name", ""),
        "business_type": record.get("business_type", ""),
        "area": record.get("area", ""),
        "source": record.get("source", ""),
        "status": record.get("status", ""),
        "error": record.get("error", ""),
        "created_at": record.get("created_at", ""),
        "finished_at": record.get("finished_at", ""),
        "count": len(leads),
        "with_phone": sum(1 for l in leads if l.get("phone")),
        "with_email": sum(1 for l in leads if l.get("emails")),
        "with_website": sum(1 for l in leads if l.get("website")),
    }


def list_lists() -> list:
    return [_summary(r) for r in _all_records()]


def get_list(list_id: str):
    return _load(list_id)


def find_list(ref: str = ""):
    """A list by id, or by words from its name ("the dentists one"), or the
    newest list when nothing is given -- which is what "show me the leads"
    right after a search means."""
    records = _all_records()
    if not records:
        return None
    ref = (ref or "").strip().lower()
    if not ref or ref in ("latest", "last", "newest", "recent"):
        return records[0]
    for r in records:
        if r["id"] == ref or r["id"].startswith(ref):
            return r
    words = set(re.findall(r"[a-z0-9]+", ref)) - {"the", "list", "leads", "in", "of", "for", "my"}
    best, best_hits = None, 0
    for r in records:  # newest first, so ties go to the most recent search
        hay = set(re.findall(r"[a-z0-9]+", r.get("name", "").lower()))
        hits = len(words & hay)
        if hits > best_hits:
            best, best_hits = r, hits
    return best


def delete_list(list_id: str) -> bool:
    if _current["list_id"] == list_id:
        return False
    try:
        path = _path(list_id)
    except ValueError:
        return False
    with _lock:
        if path.exists():
            path.unlink()
            return True
    return False


# ---------- sources ----------

def scraper_path():
    override = os.environ.get("MIRA_GMAPS_SCRAPER", "").strip()
    if override and os.access(override, os.X_OK):
        return override
    if os.access(_BUNDLED_SCRAPER, os.X_OK):
        return str(_BUNDLED_SCRAPER)
    return shutil.which("google-maps-scraper") or shutil.which("google_maps_scraper")


def _places_key() -> str:
    from main import load_settings  # local import: main imports this module
    return (load_settings().get("google_places_api_key") or "").strip()


def status() -> dict:
    running = _load(_current["list_id"]) if _current["list_id"] else None
    return {
        "scraper_installed": bool(scraper_path()),
        "scraper_path": scraper_path() or "",
        "places_configured": bool(_places_key()),
        "running": _summary(running) if running else None,
    }


def _pick_source(source: str) -> str:
    source = (source or "auto").strip().lower()
    if source in ("scraper", "gosom", "maps"):
        source = "scraper"
    if source in ("places", "google_places", "api"):
        source = "places"
    if source == "scraper" and not scraper_path():
        raise RuntimeError("The Maps scraper isn't installed. Run ./scripts/install_scraper.sh "
                           "from the Mira folder once, then try again.")
    if source == "places" and not _places_key():
        raise RuntimeError("No Google Places API key saved. Add one in Settings > Lead finder.")
    if source == "auto":
        if scraper_path():
            return "scraper"
        if _places_key():
            return "places"
        raise RuntimeError("No lead source is set up yet. Either run ./scripts/install_scraper.sh "
                           "once (free, runs on this Mac), or add a Google Places API key in "
                           "Settings > Lead finder.")
    if source not in ("scraper", "places"):
        raise RuntimeError(f"unknown source '{source}' -- use scraper, places or auto")
    return source


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _lead(name, phone="", website="", category="", address="", rating=None,
          reviews=None, maps_url="", place_id="", emails=None) -> dict:
    lead_id = place_id or hashlib.sha1(
        f"{name}|{phone}|{address}".lower().encode()).hexdigest()[:16]
    return {
        "id": lead_id,
        "name": (name or "").strip(),
        "phone": (phone or "").strip(),
        "emails": emails or [],
        "website": (website or "").strip(),
        "category": (category or "").strip(),
        "address": (address or "").strip(),
        "rating": rating,
        "reviews": reviews,
        "maps_url": maps_url or "",
        "instagram": "", "facebook": "", "linkedin": "",
        "stage": "new",
        "notes": "",
    }


def _run_scraper(record: dict) -> list:
    """One-shot scraper run: write the query to a file, let the binary crawl
    and exit, read its CSV. The browser lives exactly as long as this call."""
    binary = scraper_path()
    query = f"{record['business_type']} in {record['area']}"
    workdir = Path(tempfile.mkdtemp(prefix="mira-leads-"))
    input_file = workdir / "queries.txt"
    results_file = workdir / "results.csv"
    input_file.write_text(query + "\n")

    # No -geo: the area is part of the query text, which is how Maps itself
    # resolves "dentists in Indiranagar". No -email either: emails are found
    # afterwards over plain HTTP (see _enrich), not by opening every business
    # site in the scraper's browser.
    cmd = [binary, "-input", str(input_file), "-results", str(results_file),
           "-depth", str(record["depth"]), "-c", "1", "-lang", "en",
           "-exit-on-inactivity", INACTIVITY_EXIT]
    # Low CPU priority, so a scrape stays out of the way of the user's own
    # work. `nice` on the command line rather than os.nice() in a preexec_fn,
    # which isn't safe to use from a threaded server like this one.
    nice = shutil.which("nice")
    if nice:
        cmd = [nice, "-n", "10"] + cmd

    LEADS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(SCRAPER_LOG, "a") as logf:
            logf.write(f"\n=== {_now()} {query} (depth {record['depth']})\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), stdout=logf, stderr=subprocess.STDOUT,
                # own process group, so a timeout or cancel takes Chromium with it
                start_new_session=True,
            )
            with _job_lock:
                _current["proc"] = proc
            try:
                proc.wait(timeout=SCRAPE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                log("scraper hit the time limit; keeping what it found so far")
                _kill(proc)
            finally:
                with _job_lock:
                    _current["proc"] = None

        if _current["cancelled"]:
            raise RuntimeError("cancelled")
        if not results_file.exists():
            raise RuntimeError(f"the scraper produced no results (exit code {proc.returncode}); "
                               f"see {SCRAPER_LOG}")

        leads = []
        with open(results_file, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                if "permanently" in (row.get("status") or "").lower():
                    continue  # nobody to sell to
                emails = list(dict.fromkeys(  # the scraper can list one address twice
                    e.strip().lower() for e in (row.get("emails") or "").split(",") if e.strip()))
                leads.append(_lead(
                    row.get("title"), phone=row.get("phone"), website=row.get("website"),
                    category=row.get("category"), address=row.get("address"),
                    rating=_to_float(row.get("review_rating")),
                    reviews=_to_int(row.get("review_count")),
                    maps_url=row.get("link"), place_id=row.get("place_id") or row.get("cid"),
                    emails=emails,
                ))
        return leads
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _run_places(record: dict) -> list:
    key = _places_key()
    query = f"{record['business_type']} in {record['area']}"
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELD_MASK,
               "Content-Type": "application/json"}
    body = {"textQuery": query, "pageSize": PLACES_PAGE_SIZE}
    leads, token = [], None
    for page in range(PLACES_MAX_PAGES):
        if _current["cancelled"]:
            raise RuntimeError("cancelled")
        if token:
            body["pageToken"] = token
            time.sleep(1)  # a page token can take a moment to become valid
        resp = requests.post(PLACES_URL, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise RuntimeError(f"Places API error {resp.status_code}: {message}")
        data = resp.json()
        for p in data.get("places", []):
            if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                continue
            leads.append(_lead(
                (p.get("displayName") or {}).get("text", ""),
                phone=p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber", ""),
                website=p.get("websiteUri", ""),
                category=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
                address=p.get("formattedAddress", ""),
                rating=p.get("rating"), reviews=p.get("userRatingCount"),
                maps_url=p.get("googleMapsUri", ""), place_id=p.get("id", ""),
            ))
        token = data.get("nextPageToken")
        if not token:
            break
    return leads


# ---------- website enrichment (emails + socials) ----------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
# Things that look like addresses but are asset names, placeholders, or a site
# builder's own tracking address rather than the business's.
_EMAIL_JUNK = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|css|js)$|@(example|domain|email|yourdomain|test)\.|"
    r"sentry|wixpress|godaddy|@2x|u003e|no-?reply", re.I)

# Social patterns and skip-list adapted from google-maps-scraper-kit (MIT,
# github.com/Mahanaicoach/google-maps-scraper-kit).
_SOCIAL_RE = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]+)", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.\-%]+)", re.I),
}
_SOCIAL_SKIP = {"", "home", "pages", "people", "help", "about", "policies", "policy",
                "legal", "tos", "privacy", "settings", "sharer", "tr", "profile.php",
                "plugins", "dialog", "intent", "login", "share.php", "permalink.php",
                "p", "reel", "reels", "explore", "stories", "tv", "watch", "events",
                "groups", "marketplace", "gaming", "photo", "hashtag", "search"}
_CONTACT_LINK_RE = re.compile(r"""href=["']([^"'#]*(?:contact|about)[^"'#]*)["']""", re.I)


def _fetch(url: str) -> str:
    try:
        with requests.get(url, timeout=SITE_FETCH_TIMEOUT, stream=True,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html"}) as r:
            if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "html"):
                return ""
            body = r.raw.read(SITE_FETCH_MAX_BYTES, decode_content=True)
            return body.decode(r.encoding or "utf-8", "replace")
    except (requests.RequestException, OSError, ValueError):
        return ""


def _emails_in(html: str) -> list:
    found = []
    for m in re.finditer(r"mailto:([^\"'?>\s]+)", html, re.I):
        found.append(unquote(m.group(1)))
    if not found:
        found = _EMAIL_RE.findall(html)
    out = []
    for e in found:
        e = e.strip().strip(".").lower()
        if _EMAIL_RE.fullmatch(e) and not _EMAIL_JUNK.search(e) and e not in out:
            out.append(e)
    return out[:5]


def _socials_in(html: str) -> dict:
    out = {}
    for platform, rx in _SOCIAL_RE.items():
        for m in rx.finditer(html):
            handle = m.group(1).lower()
            if handle in _SOCIAL_SKIP:
                continue
            if platform == "facebook" and (handle.isdigit() or len(handle) < 3):
                continue
            out[platform] = m.group(0).rstrip("\"'/").replace("\\", "")
            break
    return out


def _enrich_one(lead: dict):
    site = lead.get("website", "")
    if not site:
        return
    if not site.startswith(("http://", "https://")):
        site = "http://" + site
    html = _fetch(site)
    if not html:
        return
    emails = _emails_in(html)
    socials = _socials_in(html)
    if not emails:
        # Small businesses often keep their address on a contact page only.
        # One extra fetch, same host, and only when the homepage had none.
        host = urlparse(site).netloc
        for href in _CONTACT_LINK_RE.findall(html)[:2]:
            url = urljoin(site, href)
            if urlparse(url).netloc != host:
                continue
            page = _fetch(url)
            emails = _emails_in(page)
            for k, v in _socials_in(page).items():
                socials.setdefault(k, v)
            if emails:
                break
    for e in emails:
        if e not in lead["emails"]:
            lead["emails"].append(e)
    for k, v in socials.items():
        if not lead.get(k):
            lead[k] = v


def _enrich(leads: list):
    todo = [l for l in leads if l.get("website")]
    pool = ThreadPoolExecutor(max_workers=SITE_WORKERS)
    try:
        for _ in pool.map(_enrich_one, todo):
            if _current["cancelled"]:
                break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# ---------- dedupe ----------

def _dedupe_key(lead: dict) -> str:
    digits = re.sub(r"\D", "", lead.get("phone", ""))
    if len(digits) >= 8:
        return "p:" + digits[-10:]  # ignore how the country code was written
    host = urlparse(lead.get("website", "") if "://" in lead.get("website", "")
                    else "http://" + lead.get("website", "")).netloc.lower().removeprefix("www.")
    if host:
        return "w:" + host
    return "n:" + re.sub(r"\W+", "", (lead.get("name", "") + lead.get("address", "")).lower())


def _dedupe(leads: list) -> list:
    seen, out = set(), []
    for l in leads:
        if not l.get("name"):
            continue
        key = _dedupe_key(l)
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
    return out


# ---------- running a search ----------

def start_search(business_type: str, area: str, source: str = "auto",
                 depth: int = DEFAULT_DEPTH, on_done=None) -> dict:
    """Kick off a search in the background and return straight away -- a
    scrape takes minutes, far longer than any chat turn should block.
    on_done(summary) is called when it finishes, succeeded or not."""
    business_type = (business_type or "").strip()
    area = (area or "").strip()
    if not business_type or not area:
        raise ValueError("need both a business type and an area, e.g. 'dentists' in 'Indiranagar, Bangalore'")
    chosen = _pick_source(source)
    depth = max(1, min(int(depth or DEFAULT_DEPTH), MAX_DEPTH))

    with _job_lock:
        if _current["list_id"]:
            running = _load(_current["list_id"]) or {}
            raise RuntimeError(f"A search is already running ({running.get('name', 'unknown')}). "
                               "Only one runs at a time so Google doesn't rate-limit this Mac.")
        record = {
            "id": uuid.uuid4().hex[:12],
            "name": f"{business_type} in {area}",
            "business_type": business_type,
            "area": area,
            "source": chosen,
            "depth": depth,
            "status": "running",
            "error": "",
            "created_at": _now(),
            "finished_at": "",
            "leads": [],
        }
        _save(record)
        _current.update(list_id=record["id"], cancelled=False, proc=None)

    threading.Thread(target=_run, args=(record, on_done), daemon=True).start()
    return _summary(record)


def _run(record: dict, on_done):
    try:
        leads = _run_scraper(record) if record["source"] == "scraper" else _run_places(record)
        leads = _dedupe(leads)
        record["status"] = "enriching"
        record["leads"] = leads
        _save(record)
        _enrich(leads)
        if _current["cancelled"]:
            raise RuntimeError("cancelled")
        record["status"] = "done"
    except Exception as e:
        record["status"] = "cancelled" if str(e) == "cancelled" else "failed"
        record["error"] = "" if record["status"] == "cancelled" else str(e)
        log(f"search '{record['name']}' {record['status']}: {e}")
    finally:
        record["finished_at"] = _now()
        _save(record)
        with _job_lock:
            _current.update(list_id=None, proc=None)
    summary = _summary(record)
    log(f"search '{record['name']}' finished: {summary['status']}, {summary['count']} leads")
    if on_done:
        try:
            on_done(summary)
        except Exception as e:
            log(f"completion callback failed: {e}")


def cancel_search() -> bool:
    with _job_lock:
        if not _current["list_id"]:
            return False
        _current["cancelled"] = True
        proc = _current["proc"]
    if proc:
        _kill(proc)
    return True


def completion_message(summary: dict) -> str:
    """The one-line "your leads are ready" notice, written for reading; the
    proactive delivery path turns it into speech when it speaks it."""
    if summary["status"] == "failed":
        return f"❌ Lead search for {summary['name']} failed: {summary['error']}"
    if summary["status"] == "cancelled":
        return f"Lead search for {summary['name']} was cancelled."
    if not summary["count"]:
        return (f"🔎 Lead search for {summary['name']} finished but found nothing. "
                "Try a broader area or a different business type.")
    return (f"🔎 Found {summary['count']} {summary['name']} — "
            f"{summary['with_phone']} with a phone number, {summary['with_email']} with an email. "
            "Ask me to show them or export them as a CSV.")


# ---------- reading results ----------

FILTERS = ("all", "with_email", "with_phone", "with_website", "no_website")


def filter_leads(leads: list, which: str = "all", min_rating: float = None) -> list:
    which = (which or "all").strip().lower()
    out = leads
    if which == "with_email":
        out = [l for l in out if l.get("emails")]
    elif which == "with_phone":
        out = [l for l in out if l.get("phone")]
    elif which == "with_website":
        out = [l for l in out if l.get("website")]
    elif which == "no_website":
        # often the best prospects for anyone selling web or marketing work
        out = [l for l in out if not l.get("website")]
    if min_rating:
        out = [l for l in out if (l.get("rating") or 0) >= float(min_rating)]
    return out


def to_csv(record: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LEAD_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for l in record.get("leads", []):
        row = dict(l)
        row["emails"] = ", ".join(l.get("emails") or [])
        w.writerow(row)
    return buf.getvalue()


def export_csv(record: dict) -> str:
    """Write a list to ~/Downloads, where the user will actually look for it."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", record.get("name", "leads")).strip("-").lower()[:60]
    path = EXPORT_DIR / f"mira-leads-{slug or 'list'}-{record['id'][:6]}.csv"
    path.write_text(to_csv(record), encoding="utf-8")
    return str(path)

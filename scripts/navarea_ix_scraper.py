#!/usr/bin/env python3
"""
scripts/navarea_ix_scraper.py — NAVAREA IX (Pakistan / NHO) scraper.

Source: the National Hydrographic Office Pakistan 'NAVAREA IX WARNINGS'
page. The page itself carries only a metadata table (S.NO / REGION /
DTG / WARNING # / DESCRIPTION / ATTACHMENT) — the full text of every
in-force warning sits in a separate plain-text 'View Document' file
published by the site's navarea-warnings plugin:

    https://hydrography.paknavy.gov.pk/wp-content/plugins/
      navarea-warnings/custom_uploaded_warnings_for_navarea/
      20261027SEC 412.txt

Each file is ALREADY the exact block format the universal parser
ingests for region 'ix' (DTG line, 'NAVAREA IX nnn/yy' header, region /
area / charts lines, numbered paragraphs — see
tests/fixtures/navarea_ix_sample.txt), so the harvest is a plain
concatenation — no synthesis step.

Document-link resolution (re-verified live 2026-10-27):
  1. Prefer the row's own link: the raw server HTML sometimes carries
     the 'View Document' href, sometimes only a data-* / onclick
     variant, and sometimes none at all (the live page now builds the
     anchors client-side — the first CI run found no href in any row
     and correctly refused to sync). Relative or unquoted values are
     resolved against the page URL.
  2. Otherwise derive the document URL from the row DTG. The filename
     is '<YYYYMMDD><SEC|EC> <NNN>.txt' where the date follows the DTG
     day, the prefix is inconsistent (412/26 -> '20261027SEC 412.txt'
     but 394/26 -> '20261021EC 394.txt' — the reason a plain 'SEC'
     guess once looked like a missing file) and the warning number is
     zero-padded to three digits. Both prefixes are probed, in order.
     Every candidate is validated against the advertised warning's
     own 'NAVAREA IX nnn/yy' header — the site's 404 page comes back
     with an HTTP-200 body, so status alone proves nothing.
     Header note (live CI failure 2026-10-28): SOME 2023-era files
     use the legacy WWNWS prosign form 'NAVAREA IX (.) nnn/yy (.)'
     (e.g. 20231009SEC 376.txt; 447/23 and 435/23 do not) — the
     optional '(.)' separator before the number is accepted, in the
     scraper validation AND the universal parser (see
     shared/navarea_universal_text.py).

Strategy (fail-closed, mirrors scripts/navarea_vii_scraper.py):
  0. Skip already-stored rows FIRST (fixes the WAF 429s at the root):
     on runs with DB access (no ALLOW_DRY_RUN, credentials present)
     the warnings already stored for source 'manual-navarea-ix' are
     queried and their documents NOT re-fetched — only new rows,
     rows inside the recheck window (recent DTGs — coordinators amend
     recent warnings in place) and rows with unparseable DTGs are
     fetched. Unknown state (dry run / no credentials / query error)
     fails OPEN: fetch everything. A partial harvest is NEVER treated
     as a complete snapshot — the sync's evidence-based cancel pass
     stays disabled (full_snapshot=False), so warnings skipped because
     they are stored can never be auto-cancelled. IX_FULL_SNAPSHOT=1
     forces a full fetch (backfill / lifecycle sweep).
     IX_KNOWN_IDS_FILE=<path> supplies the stored set from a file
     instead of the database (one canonical id per line, e.g.
     'NAVAREA IX 424/26'; blank lines and '#' comments ignored), for
     runs without database access that still need to stay inside the
     WAF's patience. A configured but unreadable file fails OPEN, like
     an unreachable database.
  1. Fetch the page (Scrapling Fetcher — stealthy headers, retries;
     requests fallback); any error -> non-zero exit, harvest untouched,
     parse and sync NEVER run (never sync a partial snapshot).
     Transient HTTP 503 answers are retried with backoff before any
     fail-closed refusal; the FIRST HTTP 429 stops the run cleanly
     (see point 2).
  2. Resolve one document per table row. A row whose document
     cannot be fetched — unreachable even after transient retries; the
     site advertises links its own WAF refuses (persistent HTTP 429 on
     20250903SEC 405.txt for the CI runner, live 2026-10-28) — or whose
     DTG-derived candidates all miss is skipped loudly (never silently).
     A row whose ADVERTISED document carries the WRONG warning header
     still fails the whole harvest (systematic link corruption). Zero
     resolved rows -> refuse (empty snapshot). The verbatim page HTML
     is archived alongside for provenance. The run STOPS CLEANLY
     (exit 0, stored rows kept) at MAX_REQUESTS_PER_RUN requests or
     on the FIRST HTTP 429 — no 20/40/60s retry ladder for 429 (the
     WAF block lasts 10-15 minutes live); a 429 before anything is
     stored is still a loud wholesale-block refusal. An end-of-run
     summary prints requests made, rows stored, rows remaining and
     the 429 count.
  3. Hand the harvest to the SAME universal parser + generic sync the
     manual paste pipeline uses — one contract, scrape or paste.
  4. Pre-2025 warnings (2023/2024) are ARCHIVED, not published:
     stamped status 'expired' after parsing — the active-only public
     portal never shows them.

Run:
    python -m scripts.navarea_ix_scraper
    python -m scripts.run_navarea_ix
"""
import html as _html
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote as _urlquote, urljoin as _urljoin

# Scrapling Fetcher: stealthy HTTP client with retries/impersonation
# (https://github.com/D4Vinci/Scrapling) — same idiom as scrapers/navarea_ii.py.
try:
    from scrapling.fetchers import Fetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

NAVAREA_IX_URL = "https://hydrography.paknavy.gov.pk/navarea-ix-warnings/"
REGION_KEY = "ix"
HARVEST_DIR = os.path.join("navareas", REGION_KEY)
RAW_HTML_PATH = os.path.join(HARVEST_DIR, "latest_raw.html")
RAW_TXT_PATH = os.path.join(HARVEST_DIR, "latest_raw.txt")
PARSED_PATH = os.path.join(HARVEST_DIR, "parsed.json")

_TIMEOUT_S = 45
_MIN_WARNINGS = 1  # a live-but-empty feed fails loudly; never sync 0.
# Politeness pause before EVERY document fetch (resolved hits and
# candidate misses alike). FIXED RANDOM 3-5 s (uniform): the site's
# WAF (Cloudflare) rate-limited the flat 0.6 s harvest burst with
# HTTP 429 after ~110 files in ~60s (live 2026-10-28), and persistent
# 429s on later full harvests burned minutes of retry backoff. Bounds
# are env-overridable (IX_DOC_PAUSE_MIN_S / IX_DOC_PAUSE_MAX_S) so a
# backfill or a CI dry run can pace itself inside the time budget.
_DOC_PAUSE_MIN_S = float(os.environ.get("IX_DOC_PAUSE_MIN_S", "3"))
_DOC_PAUSE_MAX_S = float(os.environ.get("IX_DOC_PAUSE_MAX_S", "5"))
# Transient HTTP answers retried with backoff before a fetch fails
# closed. HTTP 429 is NOT transient anymore: the FIRST 429 stops the
# whole run cleanly (see _HttpRateLimited) — the WAF block lasts
# 10-15 minutes live, so a 20/40/60s retry ladder only burns the
# time budget.
_TRANSIENT_STATUSES = frozenset({503})
_TRANSIENT_RETRIES = 3
_RETRY_AFTER_DEFAULT_S = 20.0
# Circuit breaker: N unreachable rows IN A ROW = the site is blocking
# this client wholesale (rate limit / WAF), not one dead document —
# abort the harvest loudly instead of grinding retry sleeps per
# remaining document (which wedged CI for 20 minutes, live
# 2026-10-28). A clean stop (request cap / first 429) does NOT
# count as an unreachable row.
_MAX_CONSECUTIVE_UNREACHABLE = 3
# Run request cap: the index request, every document fetch AND every
# retry counts toward it. At the cap the run stops CLEANLY (exit 0,
# stored rows kept) with a 'cap reached, N rows remaining' log —
# stopping at the cap is not an error. Rows are fetched newest-first
# (table order), as now. Env-overridable (IX_MAX_REQUESTS_PER_RUN)
# for the planned raise to 80 after clean backfill runs.
MAX_REQUESTS_PER_RUN = int(os.environ.get("IX_MAX_REQUESTS_PER_RUN", "60"))
# Pre-2025 warnings (2023/2024 backfill) are ARCHIVED, not published
# (Alfred's decision 2026-10-28: almost all are cancelled/expired):
# stored with status 'expired' after parsing — the active-only public
# portal never shows them.
IX_ARCHIVE_MAX_YY = 24

_BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "referer": "https://hydrography.paknavy.gov.pk/",
    "accept-language": "en;q=0.9",
}

# Table structure (verified live 2026-10-27): one <tr> per in-force
# warning, cells: S.NO / REGION-AREA / DTG / WARNING # / DESCRIPTION /
# ATTACHMENT ('View Document'). The WARNING # cell is the only bare
# 'nnn/yy' token; the ATTACHMENT cell carries the document link when
# the server renders one at all.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_WARNING_NUM_RE = re.compile(r"^\s*(\d{1,4})\s*/\s*(\d{2,4})\s*$")

# Link attributes: quoted, single-quoted or UNQUOTED values, plus the
# common data-* carriers and onclick URL literals. '#' placeholders and
# javascript: URLs are ignored (they are not document links).
_ATTR_URL_RES = (
    re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
               re.IGNORECASE),
    re.compile(r"""data-(?:href|file|url|src|attachment)\s*=\s*"""
               r"""(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE),
)
_ONCLICK_URL_RE = re.compile(
    r"""(?:"|')(https?://[^"'\s]+\.txt)(?:"|')""", re.IGNORECASE)

# Row DTG shapes seen on the live page: '271500 UTC OCT 2026',
# '300935 UTC SEP 26', '240800 UTC JULY 2026'.
_DTG_RE = re.compile(r"\b(\d{2})(\d{4})\s+UTC\s+([A-Za-z]+)\.?\s+(\d{2,4})\b",
                     re.IGNORECASE)
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# Plain-text document directory of the navarea-warnings plugin (stable
# across every Wayback capture of the site back to 2022).
_DOC_URL_BASE = ("https://hydrography.paknavy.gov.pk/wp-content/plugins/"
                 "navarea-warnings/custom_uploaded_warnings_for_navarea/")
_DOC_PREFIXES = ("SEC", "EC")  # both live on the site (see docstring)

# --- Stored-state row skipping -------------------------------------------
# The universal parser stamps source_id 'manual-navarea-ix' and
# canonical_id 'NAVAREA IX nnn/yy' for every warning it ingests, so a
# table row whose warning is already in the DB is identifiable from
# the row alone.
IX_SOURCE_ID = "manual-navarea-ix"
# Rows whose DTG falls inside this window are re-fetched even when
# already stored (catches amendments to recent warnings).
RECHECK_WINDOW_DAYS = 14
_STORED_KEY_RE = re.compile(r"^NAVAREA\s+IX\s+(\d{1,4})/(\d{2})$",
                            re.IGNORECASE)

# Every harvested document must open with its own 'NAVAREA IX nnn/yy'
# header (verified in every live file sampled: 412/26, 410/26, 409/26,
# 394/26, 378/26, 347/26, 463/25) — this is what the universal parser
# anchors on, and what validates DTG-derived candidate URLs.
_HEADER_FOR_RE = re.compile(r"NAV\s*AREA\s+IX\b", re.IGNORECASE)


def _doc_pause_seconds() -> float:
    """Randomized politeness pause for the next document fetch."""
    lo, hi = sorted((_DOC_PAUSE_MIN_S, _DOC_PAUSE_MAX_S))
    return random.uniform(lo, hi)


def _pause_before_doc_fetch() -> None:
    """Sleeps one randomized pause between document fetches."""
    time.sleep(_doc_pause_seconds())


def _strip_tags(fragment: str) -> str:
    """HTML fragment -> visible text (tags to spaces, entities decoded)."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _retry_after_seconds(headers) -> Optional[float]:
    """Numeric 'Retry-After' header value in seconds, when present."""
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After")
        return float(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None


def _fetch_once(url: str, headers: dict
                ) -> Tuple[Optional[str], Optional[int], Optional[float]]:
    """One fetch attempt: Scrapling Fetcher first, requests fallback.

    Returns (body_text, status, retry_after_seconds). body_text is
    None on any failure; status is reported for HTTP-level failures so
    the caller can retry the transient ones (503 only — a 429 stops
    the run, see fetch_url).
    """
    if HAS_SCRAPLING:
        try:
            page = Fetcher.get(url, headers=headers, timeout=_TIMEOUT_S,
                               retries=3, stealthy_headers=True)
            status = getattr(page, "status", None)
            body = page.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            body_text = str(body or "")
            if status == 200:
                return body_text, status, None
            if status in _TRANSIENT_STATUSES:
                return None, status, _retry_after_seconds(
                    getattr(page, "headers", None))
            snippet = body_text[:200].replace("\n", " ")
            print(f"WARNING: {url} returned HTTP {status}: {snippet}")
            return None, status, None
        except Exception as e:
            print(f"WARNING: Scrapling fetch failed for {url} ({e}); "
                  f"falling back to requests...")

    if not HAS_REQUESTS:
        print("ERROR: no HTTP client available "
              "(install scrapling or requests).")
        return None, None, None
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT_S)
    except Exception as e:
        print(f"ERROR: fetch failed for {url} ({e}); harvest untouched.")
        return None, None, None
    if resp.status_code == 200:
        # Audit finding F9 (IX 050/26 mojibake): the .txt documents are
        # UTF-8, but the server omits the charset and requests falls
        # back to ISO-8859-1 for text/plain, corrupting every non-ASCII
        # byte. Prefer a UTF-8 decode of the raw body whenever the
        # response does not declare a real charset (the Scrapling branch
        # above already decodes UTF-8). Responses without .content
        # (test fakes) keep today's resp.text behaviour.
        text = resp.text or ""
        declared = (getattr(resp, "encoding", None) or "").lower()
        raw = getattr(resp, "content", None)
        if declared in ("", "iso-8859-1", "latin-1") and raw:
            try:
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                text = resp.text or ""
        return text, resp.status_code, None
    if resp.status_code in _TRANSIENT_STATUSES:
        return None, resp.status_code, _retry_after_seconds(resp.headers)
    snippet = (resp.text or "")[:200].replace("\n", " ")
    print(f"ERROR: {url} returned HTTP {resp.status_code}: {snippet}")
    return None, resp.status_code, None


class _HttpRateLimited(Exception):
    """First HTTP 429: the run stops cleanly (see assemble_bulletin)."""


def _new_run_stats() -> dict:
    """Mutable per-run accounting for the end-of-run summary."""
    return {"requests": 0, "rows_stored": 0, "rows_skipped": 0,
            "rows_remaining": 0, "http_429": 0, "stop_reason": None}


def fetch_url(url: str, accept: str,
              stats: Optional[dict] = None) -> Optional[str]:
    """Fetches a URL as text.

    Scrapling's stealthy Fetcher when available, plain requests
    otherwise. Every attempt counts toward the run request cap
    (stats['requests'] — the index request and every retry included).
    Transient answers (HTTP 503) are retried with backoff, honouring a
    numeric Retry-After header when present. The FIRST HTTP 429 raises
    _HttpRateLimited (no retry ladder — the WAF block lasts 10-15
    minutes live); the caller decides how to stop. Returns the body
    text, or None (printed).
    """
    _headers = dict(_BROWSER_HEADERS)
    _headers["accept"] = accept

    for attempt in range(_TRANSIENT_RETRIES + 1):
        if stats is not None:
            # Tolerate a partially-initialised stats dict: the rest of
            # this module uses the same .get idiom, and a bare += raised
            # KeyError: 'requests' when a caller passed stats={}
            # (live CI failure 2026-10-28, two fetch_url tests).
            stats["requests"] = stats.get("requests", 0) + 1
        body_text, status, retry_after = _fetch_once(url, _headers)
        if body_text is not None:
            return body_text
        if status == 429:
            raise _HttpRateLimited(f"HTTP 429 from {url}")
        if status not in _TRANSIENT_STATUSES:
            return None
        if attempt < _TRANSIENT_RETRIES:
            wait_s = retry_after or (_RETRY_AFTER_DEFAULT_S * (attempt + 1))
            print(f"WARNING: {url} returned HTTP {status}; retrying in "
                  f"{wait_s:.0f}s "
                  f"(attempt {attempt + 1}/{_TRANSIENT_RETRIES})...")
            time.sleep(wait_s)
    return None


def _attr_url_values(fragment: str) -> List[str]:
    """Raw values of href / data-href / data-file / ... in a fragment."""
    values: List[str] = []
    for rx in _ATTR_URL_RES:
        for hm in rx.finditer(fragment):
            val = next((g for g in hm.groups() if g), None)
            if val:
                values.append(val)
    return values


def _row_link(cells: List[str], row_html: str) -> Optional[str]:
    """The row's document URL, or None.

    Looks in every cell first, then the whole row. Values are entity-
    decoded, resolved against the page URL and only accepted when they
    are http(s); '#'-placeholders and javascript: URLs are ignored.
    """
    for fragment in list(cells) + [row_html]:
        for raw in _attr_url_values(fragment):
            val = _html.unescape(raw).strip()
            if not val or val.startswith("#") or val.lower().startswith("javascript:"):
                continue
            url = _urljoin(NAVAREA_IX_URL, val)
            if url.lower().startswith("http"):
                return url
    hm = _ONCLICK_URL_RE.search(row_html)
    if hm:
        return hm.group(1)
    return None


def _dtg_datestamp(dtg_text: str) -> Optional[str]:
    """Row DTG text -> 'YYYYMMDD', or None when unparseable."""
    m = _DTG_RE.search(dtg_text or "")
    if not m:
        return None
    day, _hhmm, mon, yr = m.groups()
    month = _MONTHS.get(mon[:3].upper())
    if month is None or not 1 <= int(day) <= 31:
        return None
    year = int(yr)
    if year < 100:
        year += 2000
    return "%04d%02d%02d" % (year, month, int(day))


def _candidate_doc_urls(number: str, dtg_text: str) -> List[str]:
    """DTG-derived candidate document URLs for a link-less row.

    Filename anatomy (verified live 2026-10-27): '<YYYYMMDD><SEC|EC>
    <NNN>.txt' — date follows the DTG day, prefix is inconsistent
    (SEC for most rows, EC for e.g. 394/26), warning number is zero-
    padded to three digits (Wayback shows '024.txt', '086.txt').
    Candidates are probed in order and validated by header, so a wrong
    guess is a cheap miss, never a bad ingest.
    """
    datestamp = _dtg_datestamp(dtg_text)
    if not datestamp:
        return []
    padded = number.zfill(3)
    return [_DOC_URL_BASE + _urlquote("%s%s %s.txt" % (datestamp, prefix, padded))
            for prefix in _DOC_PREFIXES]


def _dtg_is_recent(dtg_text: str, today: Optional[date] = None) -> bool:
    """True when the row DTG falls inside the recheck window.

    Unparseable DTGs are ALWAYS 'recent' (fail-safe: such a row is
    re-fetched rather than silently skipped on a parsing hiccup).
    """
    datestamp = _dtg_datestamp(dtg_text)
    if not datestamp:
        return True
    if today is None:
        today = datetime.utcnow().date()
    try:
        row_date = datetime.strptime(datestamp, "%Y%m%d").date()
    except ValueError:
        return True
    return (today - row_date).days <= RECHECK_WINDOW_DAYS


def _supabase_client_or_none():
    """Supabase client for the stored-state lookup, or None (fail-open).

    None on ALLOW_DRY_RUN (CI PR runs: the pipeline must not touch the
    DB — the sync layer returns no client in dry-run mode either), on
    missing credentials (local runs; the sync layer fails loudly later
    if they are genuinely required) and on any construction error.
    None => the caller fetches EVERY row.
    """
    if os.environ.get("ALLOW_DRY_RUN", "").lower() in ("1", "true", "yes"):
        print("NOTE: ALLOW_DRY_RUN active - no stored-state lookup; "
              "fetching every row.")
        return None
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("NOTE: SUPABASE_URL/SUPABASE_KEY not set - no stored-state "
              "lookup; fetching every row.")
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        print(f"WARNING: Supabase client unavailable ({e}) - no "
              f"stored-state lookup; fetching every row.")
        return None


def _stored_ix_keys(client) -> Optional[set]:
    """(number, yy) pairs already stored for source 'manual-navarea-ix'.

    None = stored state unknown (no client / query error) => the
    caller fetches everything (fail-open). Rows whose warning number
    is not in this set are new or previously-unstored (e.g. rows whose
    document was unreachable in an earlier run) and are fetched.
    """
    if client is None:
        return None
    try:
        res = (client.table("nav_warnings")
               .select("canonical_id")
               .eq("source_id", IX_SOURCE_ID)
               .execute())
    except Exception as e:
        print(f"WARNING: stored-state query failed ({e}); fetching "
              f"every row.")
        return None
    keys = set()
    for row in (getattr(res, "data", None) or []):
        m = _STORED_KEY_RE.match(
            str((row or {}).get("canonical_id") or "").strip())
        if m:
            keys.add((int(m.group(1)), m.group(2)))
    return keys


def _known_ix_keys_from_file(path: str) -> Optional[set]:
    """(number, yy) pairs listed in an IX_KNOWN_IDS_FILE.

    Same shape as _stored_ix_keys, read from a file instead of the
    database: one canonical id per line ('NAVAREA IX 424/26'), blank
    lines and '#' comments ignored, unrecognised lines counted and
    skipped. None = the file cannot be read => the caller fetches
    everything (fail-open, as for an unreachable database).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"WARNING: IX_KNOWN_IDS_FILE unreadable ({e}); fetching "
              f"every row.")
        return None
    keys = set()
    unrecognised = 0
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        m = _STORED_KEY_RE.match(text)
        if m:
            keys.add((int(m.group(1)), m.group(2)))
        else:
            unrecognised += 1
    print(f"NOTE: IX_KNOWN_IDS_FILE lists {len(keys)} known warning(s)"
          + (f"; {unrecognised} unrecognised line(s) ignored" if unrecognised else "")
          + ".")
    return keys


def _resolve_stored_keys() -> Optional[set]:
    """The stored set for this run (strategy point 0), in precedence order:
    IX_FULL_SNAPSHOT=1 -> None (fetch every row); IX_KNOWN_IDS_FILE -> the
    file; otherwise the database lookup (None without credentials)."""
    if os.environ.get("IX_FULL_SNAPSHOT", "").lower() in ("1", "true", "yes"):
        print("NOTE: IX_FULL_SNAPSHOT=1 - fetching every row (backfill).")
        return None
    known_ids_file = os.environ.get("IX_KNOWN_IDS_FILE", "").strip()
    if known_ids_file:
        return _known_ix_keys_from_file(known_ids_file)
    return _stored_ix_keys(_supabase_client_or_none())


def _filter_stored_rows(rows: List[Tuple[str, str, Optional[str], str]],
                        stored_keys: Optional[set],
                        today: Optional[date] = None
                        ) -> Tuple[List[Tuple[str, str, Optional[str], str]], int]:
    """Splits rows into (rows_to_fetch, already_stored_count).

    A row is fetched when its warning is NOT stored, when its DTG is
    recent (recheck window), or when its DTG is unparseable
    (fail-safe). stored_keys None (unknown state) => fetch everything.
    """
    if stored_keys is None:
        return rows, 0
    to_fetch = []
    stored = 0
    for number, year, href, dtg in rows:
        if ((int(number), year[-2:]) in stored_keys
                and not _dtg_is_recent(dtg, today)):
            stored += 1
            continue
        to_fetch.append((number, year, href, dtg))
    return to_fetch, stored


def extract_rows(page_html: str) -> List[Tuple[str, str, Optional[str], str]]:
    """Extracts (number, year, href-or-None, dtg) per in-force warning row.

    A row that advertises a warning number is kept even when its
    ATTACHMENT cell carries no link — the document URL can be derived
    from the row DTG (see _candidate_doc_urls). The header row is
    ignored.
    """
    rows: List[Tuple[str, str, Optional[str], str]] = []
    for row_html in _ROW_RE.findall(page_html):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 4:
            continue  # header / layout row
        warning_cell = _strip_tags(cells[3])
        m = _WARNING_NUM_RE.match(warning_cell)
        if not m:
            if "View Document" in _strip_tags(row_html):
                print(f"WARNING: table row has a document link but no "
                      f"parseable WARNING # cell: {warning_cell!r}")
            continue
        number, year = m.group(1), m.group(2)
        dtg = _strip_tags(cells[2]) if len(cells) > 2 else ""
        href = _row_link(cells, row_html)
        rows.append((number, year, href, dtg))
    return rows


def _expected_header_re(number: str, year: str) -> "re.Pattern":
    yy = year[-2:]
    # 2023-era files open with the legacy WWNWS prosign header
    # 'NAVAREA IX (.) nnn/yy (.)' (live: 20231009SEC 376.txt) — the
    # optional '(.)' separator before the number is accepted.
    return re.compile(
        r"NAV\s*AREA\s+IX\b\s*(?:\(\.\)\s*)?\s*" + re.escape(number) +
        r"\s*/\s*" + re.escape(yy) + r"(?!\d)", re.IGNORECASE)


def assemble_bulletin(rows: List[Tuple[str, str, Optional[str], str]],
                      fetch_doc: Callable[[str], Optional[str]],
                      stats: Optional[dict] = None) -> Optional[str]:
    """Resolves every row's document and concatenates the blocks.

    Fail-closed where it matters:
      * a row whose document cannot be fetched — unreachable even
        after transient retries (the site advertises links its own WAF
        refuses for some clients; seen live 2026-10-28) — is skipped
        loudly like a candidate miss: one dead document must not abort
        the whole ~100-warning sync;
      * circuit breaker: three unreachable rows IN A ROW is a
        wholesale client block (rate limit / WAF), not one dead
        document — the scrape STOPS there instead of grinding retry
        sleeps per remaining document (that grind wedged CI for 20
        minutes, live 2026-10-28), and every row already harvested is
        still handed to the parser (Alfred 2026-10-30, live dispatch
        run #83: never waste a partial harvest);
      * a row whose ADVERTISED document carries the WRONG warning
        header stops the scrape the same way — the already-harvested
        rows survive, the suspect document is never ingested;
      * zero resolved documents overall -> None (empty snapshot).

    Clean stops (NOT errors — the stored rows are kept and the run
    ends with exit 0; neither counts toward the circuit breaker):
      * request cap: MAX_REQUESTS_PER_RUN requests reached -> stop
        with a 'cap reached, N rows remaining' log; the remaining
        rows are left for the next run;
      * first HTTP 429 (fetch_doc raising _HttpRateLimited) -> stop,
        stored rows kept. A 429 before ANY row is stored is still a
        loud wholesale-block refusal (None).
    """
    if stats is None:
        stats = {}
    blocks: List[str] = []
    consecutive_unreachable = 0
    for row_index, (number, year, href, dtg) in enumerate(rows):
        expected = _expected_header_re(number, year)
        if href:
            candidates: List[Tuple[str, bool]] = [(href, False)]
        else:
            candidates = [(u, True) for u in _candidate_doc_urls(number, dtg)]

        block: Optional[str] = None
        attempted_unreachable = False
        for url, skippable in candidates:
            if stats.get("requests", 0) >= MAX_REQUESTS_PER_RUN:
                stats["rows_stored"] = len(blocks)
                stats["rows_remaining"] = len(rows) - row_index
                stats["stop_reason"] = "request_cap"
                if not blocks:
                    print(f"ERROR: request cap reached "
                          f"({MAX_REQUESTS_PER_RUN} requests) with no "
                          f"warning document resolved; refusing to sync "
                          f"an empty snapshot.")
                    return None
                print(f"NOTE: cap reached ({MAX_REQUESTS_PER_RUN} "
                      f"requests) - stopping cleanly, "
                      f"{stats['rows_remaining']} row(s) remaining for "
                      f"the next run.")
                return "\n\n".join(blocks) + "\n"
            try:
                doc = fetch_doc(url)
            except _HttpRateLimited as rate_err:
                stats["http_429"] = stats.get("http_429", 0) + 1
                stats["rows_stored"] = len(blocks)
                stats["rows_remaining"] = len(rows) - row_index
                stats["stop_reason"] = "http_429"
                if not blocks:
                    print(f"ERROR: HTTP 429 on the first document "
                          f"request(s) - the site appears to be blocking "
                          f"this client (rate limit / WAF); nothing "
                          f"harvested ({rate_err}).")
                    return None
                print(f"NOTE: HTTP 429 received - stopping cleanly with "
                      f"{len(blocks)} stored row(s) kept; "
                      f"{stats['rows_remaining']} row(s) remaining "
                      f"({rate_err}).")
                return "\n\n".join(blocks) + "\n"
            if doc is None or not doc.strip():
                attempted_unreachable = True
                # Unreachable after transient retries (persistent 429/
                # 404 — the site advertises links its own WAF refuses
                # for some clients; seen live 2026-10-28). Skip loudly
                # and keep harvesting — one dead document must not
                # abort a ~100-warning sync; zero resolved overall
                # still refuses the empty snapshot below.
                print(f"WARNING: no content for warning {number}/{year} "
                      f"from {url} — skipping row loudly "
                      f"({'advertised link unreachable' if not skippable else 'candidate miss'})")
                continue
            doc = doc.replace("\r\n", "\n").replace("\r", "\n")
            block = "\n".join(line.rstrip() for line in doc.split("\n")).strip()
            if not expected.search(block):
                if not skippable:
                    stats["rows_stored"] = len(blocks)
                    stats["rows_remaining"] = len(rows) - row_index
                    stats["stop_reason"] = "wrong_header"
                    if not blocks:
                        print(f"ERROR: document for {number}/{year} does not "
                              f"carry its own 'NAVAREA IX {number}/{year[-2:]}' "
                              f"header; refusing it (wrong link? {url})")
                        return None
                    print(f"WARNING: document for {number}/{year} does not carry "
                          f"its own 'NAVAREA IX {number}/{year[-2:]}' header "
                          f"(wrong link? {url}); stopping the scrape and handing "
                          f"the {len(blocks)} row(s) already harvested to the "
                          f"parser ({stats['rows_remaining']} row(s) left).")
                    return "\n\n".join(blocks) + "\n"
                block = None
                continue
            break

        if block is not None:
            consecutive_unreachable = 0
            stats["rows_stored"] = stats.get("rows_stored", 0) + 1
            blocks.append(block)
            continue
        stats["rows_skipped"] = stats.get("rows_skipped", 0) + 1
        print(f"WARNING: no document found for warning {number}/{year} "
              f"(no usable link{'' if href else ' and no DTG-derived candidate hit'}) "
              f"— skipping row loudly (page layout change?)")
        if attempted_unreachable:
            # A real fetch failure (connection / HTTP), not a mere
            # candidate miss: several in a row means the site is
            # blocking this client wholesale — fail closed instead of
            # grinding retry sleeps per remaining document (that grind
            # wedged CI for 20 minutes, live 2026-10-28).
            consecutive_unreachable += 1
            if consecutive_unreachable >= _MAX_CONSECUTIVE_UNREACHABLE:
                stats["rows_stored"] = len(blocks)
                stats["rows_remaining"] = len(rows) - row_index
                stats["stop_reason"] = "circuit_breaker"
                if not blocks:
                    print(f"ERROR: {consecutive_unreachable} consecutive warning "
                          f"row(s) unreachable and nothing harvested — the site "
                          f"appears to be blocking this client (rate limit / "
                          f"WAF); refusing to sync an empty snapshot.")
                    return None
                print(f"WARNING: {consecutive_unreachable} consecutive warning "
                      f"row(s) unreachable — the site appears to be blocking "
                      f"this client (rate limit / WAF); stopping the scrape and "
                      f"handing the {len(blocks)} row(s) already harvested to "
                      f"the parser ({stats['rows_remaining']} row(s) left for "
                      f"the next run).")
                return "\n\n".join(blocks) + "\n"

    if not blocks:
        print("ERROR: no warning document resolved; refusing to sync an "
              "empty snapshot (page layout change?)")
        return None
    stats["rows_stored"] = len(blocks)
    stats["rows_remaining"] = 0
    if stats.get("stop_reason") is None:
        stats["stop_reason"] = "completed"
    return "\n\n".join(blocks) + "\n"


def _archive_old_warnings(parsed_path: str) -> int:
    """Stamps pre-2025 warning records status 'expired' (archive-only).

    The 2023/2024 backfill rows are stored for the record but never
    published: the public portal reads active-only, and almost all of
    them are cancelled or expired anyway. Archive status never feeds
    the sync's cancel guard (it only counts actives).
    """
    with open(parsed_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    archived = 0
    for rec in data.get("nav_warnings", []):
        m = _STORED_KEY_RE.match(
            str((rec or {}).get("warning_id") or "").strip())
        if m and int(m.group(2)) <= IX_ARCHIVE_MAX_YY:
            rec["status"] = "expired"
            archived += 1
    with open(parsed_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return archived


def _print_run_summary(stats: dict) -> None:
    """End-of-run summary: every run shows where the real limit is."""
    print("-" * 60)
    print(f"IX run summary: requests={stats.get('requests', 0)}, "
          f"rows stored={stats.get('rows_stored', 0)}, "
          f"rows skipped (unreachable)={stats.get('rows_skipped', 0)}, "
          f"rows remaining (next run)={stats.get('rows_remaining', 0)}, "
          f"429s={stats.get('http_429', 0)}, "
          f"stop reason={stats.get('stop_reason') or 'completed'}")
    print("-" * 60)


def main() -> bool:
    print("=" * 60)
    print("NAVAREA IX (Pakistan / NHO) Scraper")
    print(f"Endpoint: {NAVAREA_IX_URL}")
    print(f"HTTP Fetcher: {'Scrapling' if HAS_SCRAPLING else 'requests' if HAS_REQUESTS else 'none'}")
    print("=" * 60)

    run_stats = _new_run_stats()
    try:
        page_html = fetch_url(
            NAVAREA_IX_URL,
            "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", run_stats)
    except _HttpRateLimited as e:
        print(f"ERROR: HTTP 429 on the index request - the site appears "
              f"to be blocking this client (rate limit / WAF); harvest "
              f"untouched ({e}).")
        return False
    if page_html is None or not page_html.strip():
        return False
    if "View Document" not in page_html:
        # An HTML error page would otherwise be archived as the harvest.
        print(f"ERROR: page does not look like the NAVAREA IX table: "
              f"{_strip_tags(page_html)[:200]}")
        return False

    rows = extract_rows(page_html)
    if len(rows) < _MIN_WARNINGS:
        print(f"ERROR: extracted {len(rows)} warning row(s); refusing to "
              f"sync an empty snapshot (page layout change?)")
        return False

    # Skip already-stored rows FIRST (see strategy point 0): the
    # steady-state harvest fetches only new / recent / unparseable-DTG
    # rows, which keeps the run inside the WAF's patience.
    full_snapshot = True
    stored_keys = _resolve_stored_keys()
    rows_to_fetch, stored_skipped = _filter_stored_rows(rows, stored_keys)
    if stored_skipped:
        full_snapshot = False
        print(f"NOTE: {stored_skipped} of {len(rows)} row(s) already stored "
              f"and outside the {RECHECK_WINDOW_DAYS}-day recheck window - "
              f"not fetching their documents (partial snapshot; the sync's "
              f"cancel pass stays disabled).")

    if not rows_to_fetch:
        # Steady-state no-op: every row on the page is already stored
        # and not recent. Record an empty harvest (the workflow verifies
        # both output files exist); the sync layer treats an empty feed
        # as 'existing records left untouched' and the cancel pass
        # never runs on an empty feed.
        print("NOTE: nothing to fetch - every row is already stored and "
              "not recent; existing records left untouched.")
        os.makedirs(HARVEST_DIR, exist_ok=True)
        with open(RAW_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(page_html)
        with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
            f.write("")
        with open(PARSED_PATH, "w", encoding="utf-8") as f:
            json.dump({"raw_messages": [], "nav_warnings": [],
                       "spatial_features": [],
                       "memo": {"region": REGION_KEY, "navarea": "IX",
                                "source_id": IX_SOURCE_ID,
                                "in_force_as_on": None}}, f, indent=2)
        print(f"Empty harvest recorded -> {RAW_TXT_PATH} / {PARSED_PATH}")
        from scripts.navarea_generic_sync import sync_to_supabase
        return sync_to_supabase(PARSED_PATH, full_snapshot=full_snapshot)

    def _fetch_doc(url: str) -> Optional[str]:
        # Politeness pause before EVERY document fetch — resolved hits
        # and candidate misses alike (see _doc_pause_seconds: the
        # site's WAF rate-limits request bursts).
        _pause_before_doc_fetch()
        return fetch_url(url, "text/plain;q=0.9,*/*;q=0.8")

    bulletin = assemble_bulletin(rows_to_fetch, _fetch_doc, run_stats)
    if bulletin is None:
        return False
    if run_stats.get("stop_reason") not in (None, "completed"):
        # The scrape stopped early (request cap / first 429 / circuit
        # breaker / wrong header): the harvest is PARTIAL. Never treat
        # it as a complete snapshot, or the sync's evidence-based
        # cancel pass could cancel rows that are merely missing from
        # THIS run.
        if full_snapshot:
            full_snapshot = False
            print(f"NOTE: run stopped early (stop reason: "
                  f"{run_stats['stop_reason']}) - treating the harvest as "
                  f"a PARTIAL snapshot; the sync's cancel pass stays "
                  f"disabled.")
    _print_run_summary(run_stats)

    n_blocks = len(_HEADER_FOR_RE.findall(bulletin))
    skipped = len(rows_to_fetch) - n_blocks
    if skipped:
        print(f"NOTE: resolved {n_blocks} of {len(rows_to_fetch)} fetched "
              f"row(s); {skipped} skipped loudly (see warnings above).")

    os.makedirs(HARVEST_DIR, exist_ok=True)
    with open(RAW_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(page_html)
    with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(bulletin)
    print(f"Harvested {n_blocks} in-force warning(s) -> {RAW_TXT_PATH}")
    print(f"Verbatim page HTML archived -> {RAW_HTML_PATH}")

    from scripts.navarea_universal_parser import main as parse_main
    if not parse_main(RAW_TXT_PATH, PARSED_PATH):
        return False

    archived = _archive_old_warnings(PARSED_PATH)
    if archived:
        print(f"NOTE: archived {archived} pre-2025 warning(s) - stored "
              f"with status 'expired', NOT published on the portal.")

    from scripts.navarea_generic_sync import sync_to_supabase
    return sync_to_supabase(PARSED_PATH, full_snapshot=full_snapshot)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

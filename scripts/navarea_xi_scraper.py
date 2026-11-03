#!/usr/bin/env python3
"""
navarea_xi_scraper.py — Japan Coast Guard NAVAREA XI Scraper
Visits kaiho.mlit.go.jp with human-mimicking browser navigation (Bézier mouse
movements, random 30s–40s dwell time), then asks the coordinator for every
warning in force, year by year, and fetches their texts.

Why every year: the portal's "All Display" button lists ONE year's warnings,
but warnings from earlier years stay in force until cancelled. On 2026-11-03
the coordinator listed 32 in force, 16 of them issued 2012-2025, and the
portal's year tabs only reach back to 2017. A harvest of the current year
alone misses them, and on 1 January it misses every warning still in force
from the year before, which a full-snapshot sync then reads as withdrawn.
"""

import sys
import os
import re
import math
import random
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Optional browser automation via Playwright
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# HTTP fallback libraries
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    try:
        import requests as cffi_requests
        HAS_CURL_CFFI = False
    except ImportError:
        cffi_requests = None
        HAS_CURL_CFFI = False

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

URL_PORTAL = "https://www1.kaiho.mlit.go.jp/TUHO/keiho/navarea11_en.html"
URL_DISP = "https://www1.kaiho.mlit.go.jp/TUHO/keiho/cgi/disp_warnings.cgi"
URL_LIST = "https://www1.kaiho.mlit.go.jp/TUHO/keiho/cgi/warnings.cgi"
JCG_ENCODING = "cp932"
JCG_TYPE = "NAVAREA11"
JCG_LANG = "EG"

# Oldest year asked for. The oldest warning in force on 2026-11-03 was issued in
# 2012; the harvest prints a warning if FIRST_YEAR itself still lists one.
# Override with NAVAREA_XI_FIRST_YEAR.
FIRST_YEAR = 2000
# Warnings per disp_warnings.cgi request.
DISP_CHUNK = 40

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": URL_PORTAL,
    "Origin": "https://www1.kaiho.mlit.go.jp",
    "Content-Type": "application/x-www-form-urlencoded"
}


def _collect_from_text_lines(blocks):
    collected = {}
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        block = re.sub(r'<.*?>', '', block)
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if not lines:
            continue

        # Search leading lines (up to 3) for the notice identifier
        id_match = None
        date_line = lines[0]
        id_line_idx = 0
        for idx, l in enumerate(lines[:3]):
            m = re.search(r'(NO\.?\s*\d{2}-\d{4})', l, re.IGNORECASE)
            if m:
                id_match = m
                date_line = l
                id_line_idx = idx
                break

        if not id_match:
            continue

        wid = re.sub(r'\s+', '', id_match.group(1)).upper()
        header = f"NAVAREA XI {wid}"
        body_lines = [l for i, l in enumerate(lines) if i > id_line_idx]
        body = "\n".join(body_lines).strip()
        collected[header] = {"header": header, "date_line": date_line, "body": body}
    return collected


def _parse_html_warnings(html_content: str) -> dict:
    if not html_content:
        return {}
    html = re.sub(r'<br\s*/?>', '\n', html_content, flags=re.IGNORECASE)
    html = re.sub(r'</?STRONG>', '', html, flags=re.IGNORECASE)
    blocks = re.split(r'<hr\s*/?>', html, flags=re.IGNORECASE)
    return _collect_from_text_lines(blocks)


def _write_output(collected, output_filename):
    os.makedirs(os.path.dirname(output_filename), exist_ok=True)
    with open(output_filename, "w", encoding="utf-8") as f:
        for warn in collected.values():
            f.write(f"--- {warn['header']} ---\n{warn['date_line']}\n{warn['body']}\n\n")


def _first_year() -> int:
    try:
        return int(os.environ.get("NAVAREA_XI_FIRST_YEAR", FIRST_YEAR))
    except ValueError:
        return FIRST_YEAR


def _decode(content: bytes) -> str:
    """The CGI pages declare UTF-8; older pages were Shift JIS."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode(JCG_ENCODING, errors="replace")


def _parse_member_ids(xml_text: str, year: int) -> list:
    """Warning ids the coordinator lists as in force for one year.

    warnings.cgi answers <opt><Member><tana>260496</tana>...</Member></opt>,
    the call the portal makes to fill a year's table. Anything else raises
    ValueError, so a malformed answer fails the harvest instead of reading as
    "nothing in force that year".
    """
    try:
        root = ET.fromstring((xml_text or "").strip())
    except ET.ParseError as e:
        raise ValueError(f"warnings.cgi {year}: not XML ({e})") from None
    if root.tag != "opt":
        raise ValueError(f"warnings.cgi {year}: unexpected root <{root.tag}>")
    ids = []
    for member in root.findall("Member"):
        tana = (member.findtext("tana") or "").strip()
        if not re.fullmatch(r"\d{6}", tana) or tana[:2] != f"{year % 100:02d}":
            raise ValueError(f"warnings.cgi {year}: unexpected warning id {tana!r}")
        ids.append(tana)
    return ids


def _header_for(tana: str) -> str:
    """'260496' -> 'NAVAREA XI NO.26-0496', the key _collect_from_text_lines gives it."""
    return f"NAVAREA XI NO.{tana[:2]}-{tana[2:]}"


async def _harvest_in_force(post, first_year: int, last_year: int, pause=(0.5, 1.5)):
    """Every warning the coordinator lists as in force, first_year to last_year.

    post(url, data) -> (status, text) is the transport (browser or direct
    HTTP). Returns {} when no year lists a warning, and None when any request
    fails or a listed warning does not come back: a harvest missing warnings
    would read to a full-snapshot sync as their cancellation.
    """
    ids = []
    for year in range(last_year, first_year - 1, -1):
        status, text = await post(URL_LIST, {"YEAR": str(year), "TYPE": JCG_TYPE, "LANG": JCG_LANG})
        if status != 200:
            print(f"⚠️ warnings.cgi {year}: HTTP {status}; harvest incomplete.")
            return None
        try:
            year_ids = _parse_member_ids(text, year)
        except ValueError as e:
            print(f"⚠️ {e}; harvest incomplete.")
            return None
        if year == first_year and year_ids:
            print(f"⚠️ {year} still lists warnings in force; earlier years may too (lower NAVAREA_XI_FIRST_YEAR).")
        ids.extend(year_ids)
        if pause:
            await asyncio.sleep(random.uniform(*pause))

    collected = {}
    for i in range(0, len(ids), DISP_CHUNK):
        chunk = ids[i:i + DISP_CHUNK]
        data = {"TYPE": JCG_TYPE, "TANA": "".join(t + ":" for t in chunk), "LANG": JCG_LANG}
        status, text = await post(URL_DISP, data)
        if status != 200:
            print(f"⚠️ disp_warnings.cgi: HTTP {status}; harvest incomplete.")
            return None
        collected.update(_parse_html_warnings(text))
        if pause:
            await asyncio.sleep(random.uniform(*pause))

    missing = [t for t in ids if _header_for(t) not in collected]
    if missing:
        print(f"⚠️ {len(missing)} listed warning(s) did not come back ({', '.join(missing[:5])}); harvest incomplete.")
        return None
    print(f"📋 {len(ids)} warnings in force, {first_year}-{last_year}.")
    return collected


async def _human_mouse_move(page, start_x: float, start_y: float, target_x: float, target_y: float, steps: int = 25):
    """
    Simulates human-like mouse movement using a quadratic Bézier curve
    with random control points and micro-sleep jitter.
    """
    # Control point for natural curve
    cx = (start_x + target_x) / 2.0 + random.uniform(-60.0, 60.0)
    cy = (start_y + target_y) / 2.0 + random.uniform(-60.0, 60.0)

    for i in range(1, steps + 1):
        t = i / float(steps)
        # Quadratic Bézier: B(t) = (1-t)^2 * P0 + 2(1-t)t * P1 + t^2 * P2
        x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * cx + (t ** 2) * target_x
        y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * cy + (t ** 2) * target_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.008, 0.025))


async def fetch_via_browser(year=None, first_year=None):
    """
    Visits navarea11_en.html, simulates human browsing with curved cursor paths
    and a 30s-40s dwell, then runs the in-force harvest through the browser
    context (its cookies and headers). Returns the warnings, {} when none are
    in force, or None when the harvest failed or is incomplete.
    """
    if not HAS_PLAYWRIGHT:
        print("ℹ️ Playwright library not installed in environment; skipping browser automation.")
        return None

    last_year = year or datetime.now(timezone.utc).year
    first_year = first_year or _first_year()
    print(f"🌐 Launching Chromium browser (years {first_year}-{last_year})...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars"
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            print(f"👉 1. Visiting {URL_PORTAL}...")
            await page.goto(URL_PORTAL, wait_until="networkidle", timeout=45000)

            # Initial cursor placement
            curr_x = random.uniform(150, 350)
            curr_y = random.uniform(100, 250)
            await page.mouse.move(curr_x, curr_y)

            # Natural exploratory mouse wander and small scroll
            wander_x = curr_x + random.uniform(-40, 150)
            wander_y = curr_y + random.uniform(50, 180)
            await _human_mouse_move(page, curr_x, curr_y, wander_x, wander_y, steps=20)
            curr_x, curr_y = wander_x, wander_y

            # Allow user to override delay for testing via NAVAREA_XI_DELAY env var
            custom_delay = os.environ.get("NAVAREA_XI_DELAY")
            if custom_delay is not None:
                dwell_time = float(custom_delay)
            else:
                dwell_time = random.uniform(30.0, 40.0)

            print(f"⏳ 2. Simulating human reading dwell time ({dwell_time:.1f}s) between actions...")

            # Divide dwell time into chunks with realistic occasional cursor jitters
            intervals = max(1, int(dwell_time / 5))
            chunk_time = dwell_time / intervals
            for _ in range(intervals):
                await asyncio.sleep(chunk_time)
                # Small human idle micro-adjustment
                next_x = max(50, min(1200, curr_x + random.uniform(-25, 25)))
                next_y = max(50, min(650, curr_y + random.uniform(-25, 25)))
                await _human_mouse_move(page, curr_x, curr_y, next_x, next_y, steps=10)
                curr_x, curr_y = next_x, next_y

            async def browser_post(url, data):
                try:
                    r = await context.request.post(url, form=data, headers={"Referer": URL_PORTAL}, timeout=30000)
                    return r.status, _decode(await r.body())
                except Exception as e:
                    print(f"⚠️ Browser request error ({url}): {e}")
                    return 0, ""

            print("🔍 3. Harvesting every year's in-force warnings...")
            return await _harvest_in_force(browser_post, first_year, last_year)

        except Exception as e:
            print(f"⚠️ Browser automation error: {e}")
            return None
        finally:
            await browser.close()


def fetch_direct_disp(year=None, first_year=None):
    """
    HTTP fallback if browser automation or Playwright is not available: the
    same in-force harvest over direct requests. Returns the warnings, {} when
    none are in force, or None when the harvest failed or is incomplete.
    """
    last_year = year or datetime.now(timezone.utc).year
    first_year = first_year or _first_year()
    print(f"Direct HTTP fetch from official Japan Coast Guard (kaiho.mlit.go.jp) for years {first_year}-{last_year}...")
    if cffi_requests is None:
        return None

    def call(url, data):
        if HAS_CURL_CFFI:
            r = cffi_requests.post(url, data=data, headers=HTTP_HEADERS, impersonate="chrome", timeout=30, verify=True)
        else:
            r = cffi_requests.post(url, data=data, headers=HTTP_HEADERS, timeout=30, verify=True)
        return r.status_code, _decode(r.content)

    async def direct_post(url, data):
        try:
            return await asyncio.to_thread(call, url, data)
        except Exception as e:
            print(f"⚠️ Direct JCG Kaiho HTTP request error ({url}): {e}")
            return 0, ""

    return asyncio.run(_harvest_in_force(direct_post, first_year, last_year))


def scrape_xi_warnings(output_filename="navareas/navarea_xi/navarea_xi_warnings.txt", year=None, first_year=None):
    print("=" * 60)
    print("⚓ Japan Coast Guard (JCG Kaiho) NAVAREA XI Scraper")
    print("=" * 60)
    if year is None:
        year = datetime.now(timezone.utc).year
    if first_year is None:
        first_year = _first_year()
    collected = None

    # 1. Try humanized browser automation if Playwright is installed
    if HAS_PLAYWRIGHT:
        try:
            collected = asyncio.run(fetch_via_browser(year=year, first_year=first_year))
            if collected:
                print(f"✅ Successfully harvested {len(collected)} bulletins via browser automation.")
        except Exception as e:
            print(f"⚠️ Browser harvest attempt failed: {e}")

    # 2. HTTP fallback if the browser was unavailable or its harvest incomplete
    if collected is None:
        try:
            collected = fetch_direct_disp(year=year, first_year=first_year)
            if collected:
                print(f"✅ Successfully harvested {len(collected)} bulletins directly via HTTP fallback.")
        except Exception as e:
            print(f"⚠️ Direct JCG Kaiho HTTP fetch failed: {e}")

    if collected is None:
        # No complete harvest by either path: loud failure, runner aborts,
        # previous harvest left untouched.
        print("⛔ JCG Kaiho harvest failed or incomplete on both paths; aborting (harvest untouched).")
        return None

    if not collected:
        print("ℹ️ No active warnings in force at JCG Kaiho; writing valid empty harvest.")
        _write_output({}, output_filename)
        return {}

    print(f"Total warnings: {len(collected)}")
    _write_output(collected, output_filename)
    print(f"✅ Wrote to {output_filename}")
    return collected


if __name__ == "__main__":
    scrape_xi_warnings()

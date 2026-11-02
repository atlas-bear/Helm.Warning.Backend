#!/usr/bin/env python3
"""
navarea_8_scraper.py — NAVAREA VIII (NHO India) Stage-1 Scraper Wrapper
Acquires the latest 'NAVAREA VIII - Warnings In Force' bulletin text.

Acquisition tiers (in order, first success wins):
  TIER 1 (preferred): NHO portal warnings TABLE via scrapers/navarea_8_table.py
       (Playwright; auto-presses 'Next' until every paginated page is
       scraped). Emits both structured rows (navarea_8_table.json) and
       synthesized bulletin text.
  TIER 2 (authoritative PDF): NHO portal PDF via scrapers/navarea_8.py
       (requests fast-path, Playwright fallback) + pypdf text extraction.
       Used when the table cannot be scraped (e.g. portal redesign).
  TIER 3 (fallback): SeaLagom NAVAREA VIII mirror (sealagom.com/navarea/8) —
       anonymous plain-requests fetch of the same in-force bulletin text,
       community-contributed. Used only when both NHO tiers fail.
       VIII_DISABLE_MIRROR=1 removes this tier, for deployments that may
       only use the coordinator's own publication: acquisition then
       succeeds from NHO or fails (or salvages a partial) without it.

Partial-harvest contract (mirrors the IX salvage rules):
  * Tier 1 pagination that stops early (WAF block, dead 'Next', timeout)
    is REFUSED as a complete harvest — the partial rows stay on disk as a
    salvage candidate and acquisition falls through to the PDF.
  * If every tier fails but Tier 1 left a salvageable partial on disk, the
    partial is handed to the parser with stop_reason='early_stop' and
    full_snapshot=False, so the sync never treats absent rows as withdrawn
    warnings (cancel-pass hazard).
  * Steady state: rows already stored in the DB are skipped — when page 1
    of the table carries only known warnings, pagination stops there.

Writes the raw text to navareas/navarea_8/navarea_8_warnings.txt; structured
table rows are kept alongside (navarea_8_table.json) for audit/grounding.
"""

import os
import sys
import re

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

OUTPUT_DIR = os.path.join("navareas", "navarea_8")
MIRROR_URL = "https://www.sealagom.com/navarea/8/"
MIRROR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# Minimum sanity: in-force bulletins carry dozens of warnings; fewer than
# this suggests we captured an error page or a truncated document.
MIN_EXPECTED_WARNINGS = 5

SOURCE_ID = "nho-navarea-viii"


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT_DIR, path)


def _header_count(text: str) -> int:
    return len(re.findall(r"NAVAREA\s+VIII\s*[-\u2013]?\s*\d{1,4}\s*/\s*\d{2}", text, re.IGNORECASE))


def _known_viii_ids():
    """Canonical ids already stored for NAVAREA VIII (stored-row skipping).

    Returns None when credentials are unavailable (dry-run) or the lookup
    fails: skipping is then disabled and the sweep paginates fully, exactly
    as before.
    """
    try:
        from shared.sync_engine import get_supabase_client
        client = get_supabase_client()
    except Exception:
        return None
    if client is None:
        return None
    try:
        res = (client.table("nav_warnings").select("canonical_id")
               .eq("source_id", SOURCE_ID).execute())
        return {row["canonical_id"] for row in (res.data or [])
                if row.get("canonical_id")}
    except Exception as e:
        print(f"[skip-check] Stored-row lookup failed ({e}); running a full sweep.")
        return None


def _fetch_tier1_table(raw_txt: str):
    """NHO table acquisition (auto-pagination). Returns (text, stop_reason).

    Raises on an early-stopped sweep: the partial rows stay on disk as a
    salvage candidate, but Tier 1 is refused as a COMPLETE harvest and
    acquisition falls through to the authoritative PDF.
    """
    from scrapers.navarea_8_table import run_navarea_8_table_scraper

    summary = run_navarea_8_table_scraper(
        output_dir=OUTPUT_DIR, dry_run=False,
        raw_txt=raw_txt, known_ids=_known_viii_ids())
    n = summary.get("warnings") or 0
    stop_reason = summary.get("stop_reason") or "completed"
    if stop_reason == "early_stop":
        raise RuntimeError(
            f"Tier 1 (NHO table): pagination stopped early after {n} "
            f"warning(s); partial harvest kept on disk as salvage, "
            f"falling through to the PDF.")
    if n < MIN_EXPECTED_WARNINGS:
        raise RuntimeError(
            f"Tier 1 (NHO table): only {n} warning(s) collected "
            f"(threshold {MIN_EXPECTED_WARNINGS}); possible partial scrape.")

    with open(_abs(raw_txt), "r", encoding="utf-8") as f:
        return f.read(), stop_reason


def _fetch_tier2_pdf():
    """NHO PDF acquisition + pypdf extraction. Returns (extracted text, 'completed')."""
    from scrapers.navarea_8 import run_navarea_8_scraper

    summary = run_navarea_8_scraper(output_dir=OUTPUT_DIR, dry_run=False)
    pdf_path = summary.get("saved_path")
    if not pdf_path:
        raise RuntimeError("NAVAREA VIII PDF was not saved (dry-run or failure).")

    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is required for NAVAREA VIII PDF text extraction. "
                           "Install with: pip install pypdf")

    reader = PdfReader(pdf_path)
    pages = []
    for i, page in enumerate(reader.pages):
        pages.append(page.extract_text() or "")
    text = "\n".join(pages)

    if not text.strip():
        raise RuntimeError("PDF text extraction produced empty text; bulletin may be "
                            "scanned images or the layout changed.")
    return text, "completed"


def _mirror_disabled() -> bool:
    """True when VIII_DISABLE_MIRROR removes the Tier 3 mirror."""
    return os.environ.get("VIII_DISABLE_MIRROR", "").lower() in ("1", "true", "yes")


def _fetch_tier3_mirror():
    """SeaLagom mirror: anonymous requests fetch of the in-force text."""
    import requests

    print(f"[Tier 3] Falling back to SeaLagom mirror: {MIRROR_URL}")
    resp = requests.get(
        MIRROR_URL,
        headers={
            "User-Agent": MIRROR_UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    # Strip scripts/styles/tags to plain text (same approach as the proven
    # nho_fetch.py pipeline from hydrobharat-navarea-scraper).
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    return text, "completed"


def _validate(text: str, tier: str) -> None:
    n = _header_count(text)
    if n < MIN_EXPECTED_WARNINGS:
        raise RuntimeError(
            f"{tier}: only {n} warning header(s) found (threshold {MIN_EXPECTED_WARNINGS}); "
            "possible error page or truncated document."
        )
    print(f"[{tier}] Sanity check passed: {n} warning header(s) detected.")


def scrape_viii_warnings(raw_txt="navareas/navarea_8/navarea_8_warnings.txt") -> dict:
    """Runs the tiered acquisition and writes raw text to raw_txt.

    Returns {"text": str, "stop_reason": str, "full_snapshot": bool}.
    full_snapshot=False means the harvest is PARTIAL (Tier 1 pagination
    stopped early and both fallback tiers were unavailable): the parser
    still receives every row collected, but the sync must not treat
    absent rows as withdrawn warnings.
    """
    raw_txt = _abs(raw_txt)

    text = None
    stop_reason = None
    errors = []
    tiers = [("Tier 1 (NHO table)", lambda: _fetch_tier1_table(raw_txt)),
             ("Tier 2 (NHO PDF)", _fetch_tier2_pdf)]
    if _mirror_disabled():
        print("[Tier 3] SeaLagom mirror disabled (VIII_DISABLE_MIRROR=1).")
    else:
        tiers.append(("Tier 3 (SeaLagom mirror)", _fetch_tier3_mirror))
    for tier_name, fetcher in tiers:
        try:
            candidate, reason = fetcher()
            _validate(candidate, tier_name)
            text = candidate
            stop_reason = reason
            print(f"[+] Acquisition succeeded via {tier_name}")
            break
        except Exception as e:
            print(f"[!] {tier_name} failed: {e}")
            errors.append(f"{tier_name}: {e}")

    if text is None:
        # Salvage: Tier 1 may have left a partial harvest on disk before
        # falling through. Hand it to the parser — but marked partial.
        if os.path.exists(raw_txt):
            with open(raw_txt, "r", encoding="utf-8") as f:
                partial = f.read()
            n = _header_count(partial)
            if n >= MIN_EXPECTED_WARNINGS:
                print(f"[!] All tiers failed; salvaging Tier 1 partial harvest "
                      f"({n} warning header(s)) — sync will treat it as PARTIAL.")
                text = partial
                stop_reason = "early_stop"
        if text is None:
            raise RuntimeError("All acquisition tiers failed and no partial "
                               "harvest to salvage: " + " | ".join(errors))

    os.makedirs(os.path.dirname(raw_txt), exist_ok=True)
    with open(raw_txt, "w", encoding="utf-8") as f:
        f.write(text)

    full_snapshot = stop_reason in ("completed", "all_known")
    print(f"Extracted {len(text)} chars -> {raw_txt} "
          f"(stop_reason={stop_reason}, full_snapshot={full_snapshot})")
    return {"text": text, "stop_reason": stop_reason, "full_snapshot": full_snapshot}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "navareas/navarea_8/navarea_8_warnings.txt"
    try:
        scrape_viii_warnings(target)
        sys.exit(0)
    except Exception as e:
        print(f"NAVAREA VIII scrape failed: {e}", file=sys.stderr)
        sys.exit(1)

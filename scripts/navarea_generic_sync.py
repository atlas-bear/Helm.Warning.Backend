#!/usr/bin/env python3
"""
navarea_generic_sync.py — Differential Sync for the universal-parser
regions (NAVAREA II / III / V / VI / VII / IX / X / XIII–XXI).

One wrapper serves all universal regions: the universal parser stamps
'region', 'navarea' and 'source_id' into the parsed JSON's memo block,
and this sync passes them to the shared engine, which is fully
region-agnostic (keyed on source_id). Template: scripts/baltic_sync.py.

navarea column normalization (audit finding F3, 2026-10-30):
record values are normalized in ONE place here — a bare coordinator
code ("II"), AUSCOAST pseudo-regions filed under "X" — while the
display label passed to the engine keeps its prefixed form (it is
the canonical-id / cross-ref target prefix).

Cross-ref CANCEL supersede transitions are resolved by the shared
sync engine (resolve_cancel_cross_refs — audit-first, gated by the
SYNC_APPLY_CANCEL_REFS environment variable).
"""
import os
import json
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from shared.sync_engine import run_differential_sync
from shared.sync_sink import configured_sink_path


def _normalized_navarea_column(value):
    """navarea column value for one warning (audit finding F3, 2026-10-30).

    The live column mixed bare codes ('II'), prefixed labels
    ('NAVAREA II') and AUSCOAST area labels, which breaks region
    grouping in the Data Bank. Canonical form: the bare coordinator
    code, with AUSCOAST pseudo-regions filed under 'X' (the PR #62
    amendment). None passes through (the caller may fill it from the
    memo code, but never from the prefixed display label).
    """
    if not value or not isinstance(value, str):
        return None
    label = value.strip()
    if not label:
        return None
    if label.upper().startswith("AUSCOAST"):
        return "X"
    if label.upper().startswith("NAVAREA "):
        label = label[len("NAVAREA "):].strip()
    return label or None


def sync_to_supabase(json_file="navareas/manual/navarea_ii/parsed.json",
                     full_snapshot: bool = True) -> bool:
    """Sync parsed universal-region warnings to Supabase, or no-op dry run
    when credentials are missing.

    full_snapshot=False (a harvest that skipped already-stored rows)
    disables the evidence-based cancel pass in the sync engine: the
    feed is partial BY DESIGN, so absence is not withdrawal evidence."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    # A configured file sink (HELM_SYNC_SINK) needs no credentials.
    if (not url or not key) and configured_sink_path() is None:
        print("ℹ️ SUPABASE_URL or SUPABASE_KEY missing; skipping universal "
              "sync in dry-run mode.")
        return True

    if not os.path.isabs(json_file):
        json_file = os.path.join(ROOT_DIR, json_file)

    if not os.path.exists(json_file):
        print("❌ Error: Parsed file " + json_file + " does not exist.", file=sys.stderr)
        return False

    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("❌ Error reading JSON file " + json_file + ": " + str(e), file=sys.stderr)
        return False

    memo = data.get("memo") or {}
    source_id = memo.get("source_id")
    navarea = memo.get("navarea")
    if navarea and navarea.upper().startswith("NAVAREA "):
        # Legacy parsed JSON may carry the prefixed label; the
        # display label below re-prefixes it, so strip first (F3).
        navarea = navarea[len("NAVAREA "):].strip()
    if not source_id or not navarea:
        print("❌ Error: parsed JSON memo lacks source_id/navarea — cannot "
              "determine the region for sync. Re-run the universal parser "
              "to regenerate it.", file=sys.stderr)
        return False

    scraped_messages = data.get("raw_messages", [])
    # navarea column normalization (audit F3): the engine only fills
    # navarea when a record lacks it, so the record value IS the DB
    # column — normalize it before persisting. Copies, never in-place
    # mutation of the loaded JSON. Records missing navarea get the
    # normalized memo code, so the prefixed display label never lands
    # in the column via the engine fallback either.
    scraped_warnings = []
    for warn in data.get("nav_warnings", []):
        if not isinstance(warn, dict):
            scraped_warnings.append(warn)
            continue
        warn = dict(warn)
        column = (_normalized_navarea_column(warn.get("navarea"))
                  or _normalized_navarea_column(navarea))
        if column is not None:
            warn["navarea"] = column
        scraped_warnings.append(warn)

    return run_differential_sync(
        source_id=source_id,
        # AUSCOAST pseudo-regions carry a display-ready navarea label
        # ('AUSCOAST B'); NAVAREA regions keep the 'NAVAREA ' prefix.
        navarea_name=(navarea if navarea.startswith("AUSCOAST")
                      else "NAVAREA " + navarea),
        scraped_warnings=scraped_warnings,
        scraped_messages=scraped_messages,
        full_snapshot=full_snapshot,
    )


if __name__ == "__main__":
    success = sync_to_supabase()
    sys.exit(0 if success else 1)

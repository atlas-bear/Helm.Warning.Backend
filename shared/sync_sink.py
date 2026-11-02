#!/usr/bin/env python3
"""shared/sync_sink.py — Optional file sink for sync runs.

Setting HELM_SYNC_SINK=jsonl:<path> makes every sync entry point
(shared.sync_engine.run_differential_sync, and the NAVAREA II pipeline,
which writes per warning) append ONE JSON record per run to <path> and
return WITHOUT opening a database client. Scraping and parsing run
exactly as they do against Supabase; only the final write changes.

Uses: running the pipelines without database credentials and still
keeping their full output (local development, debugging a parser
change, CI artefacts), or handing harvests to a different storage
backend. Unset (the default), nothing changes.

Record contract (schema "helm.sync_run.v1", one line per run):

  schema          "helm.sync_run.v1"
  emitted_at      ISO-8601 UTC timestamp of the write
  source_id       sync source id (e.g. "manual-navarea-ix")
  navarea         navarea name the sync was called with (e.g. "NAVAREA IX")
  full_snapshot   the sync's own completeness flag. False means the
                  harvest is partial BY DESIGN (skipped stored rows,
                  stopped early): absence from it is NOT withdrawal
                  evidence. Same meaning as run_differential_sync's
                  argument.
  warnings        list of {warning_id, master, geometries, cross_refs};
                  master and geometries are exactly what the Supabase
                  path would write (build_master_payload /
                  build_geometry_payloads).
  cancel_targets  {target canonical_id: carrier canonical_id} resolved
                  from CANCEL cross-references by the same pure mapping
                  the Supabase path uses (_cancel_ref_target_ids).
                  Explicit withdrawal evidence, valid even when
                  full_snapshot is False.

A malformed HELM_SYNC_SINK raises ValueError instead of falling back to
the database path: a misconfigured sink must fail loudly, never write
somewhere the operator did not ask for.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SINK_ENV = "HELM_SYNC_SINK"
RECORD_SCHEMA = "helm.sync_run.v1"


def configured_sink_path() -> Optional[str]:
    """The sink file path from HELM_SYNC_SINK, or None when unset.

    Raises ValueError when the variable is set but not 'jsonl:<path>'.
    """
    raw = os.environ.get(SINK_ENV, "").strip()
    if not raw:
        return None
    kind, sep, path = raw.partition(":")
    path = path.strip()
    if kind.strip().lower() != "jsonl" or not sep or not path:
        raise ValueError(
            f"{SINK_ENV} must be 'jsonl:<path>' (got {raw!r})")
    return path


def build_run_record(source_id: str,
                     navarea_name: str,
                     entries: List[Dict[str, Any]],
                     full_snapshot: bool,
                     cancel_targets: Optional[Dict[str, str]] = None,
                     ) -> Dict[str, Any]:
    """Assembles one sink record (see the module docstring)."""
    return {
        "schema": RECORD_SCHEMA,
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "source_id": source_id,
        "navarea": navarea_name,
        "full_snapshot": bool(full_snapshot),
        "warnings": list(entries),
        "cancel_targets": dict(cancel_targets or {}),
    }


def write_run_record(path: str, record: Dict[str, Any]) -> None:
    """Appends one record as a single JSON line (one write call)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

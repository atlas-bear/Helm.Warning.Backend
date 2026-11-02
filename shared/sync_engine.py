#!/usr/bin/env python3
"""shared/sync_engine.py — Core Differential Ingestion Engine for all NAVAREA Regions.
Handles:
1. Environment pre-flight credential checks (loud failure if missing credentials)
2. Differential change detection keyed on canonical_id
3. Atomic master + child geometry writes via upsert_nav_warning RPC
4. Audit trail persistence into public.raw_messages
5. Evidence-based lifecycle transitions (guarded cancel / expiry — a warning
   missing from a scrape is never auto-cancelled without evidence)
6. Run ledger persistence into public.ingestion_generations
"""

import os
import sys
import json
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from shared.models import build_master_payload, build_geometry_payloads, parse_safe_iso_timestamp
from shared.utils import compute_differential
from shared.sync_sink import configured_sink_path, build_run_record, write_run_record

VALID_RAW_COLUMNS = {
    "warning_id", "source_id", "subject_header",
    "full_raw_text", "received_timestamp", "checksum_sha256"
}

# ---------------------------------------------------------------------------
# Lifecycle-transition evidence thresholds.
#
# A scrape that is suspiciously small relative to the DB's active set is
# treated as an INCOMPLETE SNAPSHOT: scrape-absent warnings are left
# untouched (recorded as 'unobserved' in the run ledger) instead of being
# cancelled. This mirrors the frontend guard (computeDifferentialGuarded,
# minFeedSizeForCancel) and prevents partial-source failures from wiping
# warnings off the live map.
# ---------------------------------------------------------------------------
CANCEL_MIN_FEED_COUNT = int(os.environ.get("SYNC_CANCEL_MIN_FEED_COUNT", "10"))
CANCEL_MIN_FEED_FRACTION = float(os.environ.get("SYNC_CANCEL_MIN_FEED_FRACTION", "0.5"))


def get_supabase_client():
    """Initializes and returns the Supabase client.
    Exits loud if SUPABASE_URL or SUPABASE_KEY are missing unless ALLOW_DRY_RUN=1.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    allow_dry_run = os.environ.get("ALLOW_DRY_RUN", "").lower() in ("1", "true", "yes")

    # A configured file sink means no database for the whole process: every
    # caller (stored-row lookups, the sync, post-sync supersede passes) gets
    # None, exactly as in dry-run mode, so a credential-less run never exits
    # on a lookup that only exists to serve the database path.
    if configured_sink_path() is not None:
        print("ℹ️ HELM_SYNC_SINK active: no Supabase client (output goes to the sink file).")
        return None

    # Explicit dry-run request always wins, even with valid credentials present,
    # so CI can validate the full pipeline without touching the database.
    if allow_dry_run:
        print("⚠️ ALLOW_DRY_RUN active: running in dry-run mode (no Supabase client).")
        return None

    if not url or not key:
        if allow_dry_run:
            print("⚠️ SUPABASE_URL or SUPABASE_KEY missing in environment (ALLOW_DRY_RUN active).")
            return None
        print("❌ FATAL: SUPABASE_URL or SUPABASE_KEY is missing from the environment.", file=sys.stderr)
        print("   Set both variables or run with ALLOW_DRY_RUN=1 to simulate.", file=sys.stderr)
        sys.exit(2)

    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        if allow_dry_run:
            print("⚠️ supabase Python package not installed (ALLOW_DRY_RUN active).")
            return None
        print("❌ FATAL: 'supabase' package is not installed. Install with `pip install supabase`.", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"❌ FATAL: Failed to initialize Supabase client: {e}", file=sys.stderr)
        sys.exit(2)


def upsert_warning_atomic(supabase_client, master: Dict[str, Any], geometries: List[Dict[str, Any]]) -> bool:
    """Invokes public.upsert_nav_warning RPC to update master and child geometries in one transaction.
    Falls back to direct master upsert if RPC is unavailable.
    """
    if supabase_client is None:
        return True

    payload = {
        "master": master,
        "geometries": geometries
    }

    try:
        supabase_client.rpc("upsert_nav_warning", {"p_payload": payload}).execute()
        return True
    except Exception as rpc_err:
        # Fallback to direct master upsert if RPC is missing.
        # IMPORTANT: the direct upsert CANNOT persist geometries (the
        # geom_postgis column requires PostGIS/ST_GeomFromGeoJSON, only
        # available inside the RPC). Persisting the master while dropping
        # its geometries corrupts the nav_warnings_active_spatial JOIN
        # (Tier-A masters with zero geometry rows = invisible on the map),
        # so this is treated as an error, not a silent success.
        print(f"    ⚠️ RPC upsert failed ({rpc_err}); attempting direct master upsert...")
        try:
            supabase_client.table("nav_warnings").upsert(master, on_conflict="canonical_id").execute()
        except Exception as direct_err:
            print(f"    ❌ Direct master upsert failed: {direct_err}")
            return False
        if geometries:
            print(
                f"    ❌ GEOMETRIES DROPPED for {master.get('canonical_id')}: "
                f"{len(geometries)} geometry payload(s) NOT persisted "
                f"(nav_warnings_active_spatial will not show this warning). "
                f"Fix the upsert_nav_warning RPC and re-run the sync."
            )
            return False
        return True


def _record_generation(
    supabase_client,
    source_id: str,
    navarea_name: str,
    started_at: str,
    status: str,
    counts: Dict[str, Any],
    evidence_ids: List[str],
) -> None:
    """Best-effort run ledger row in public.ingestion_generations.

    Non-fatal by design: the ledger must never block a sync, but its absence
    should be visible. Frontend (Data Bank) reads: generation_id, source_id,
    started_at, completed_at, status, parsed_count, active_count, evidence_hash.
    """
    if supabase_client is None:
        return
    evidence_hash = hashlib.sha256("|".join(sorted(evidence_ids)).encode("utf-8")).hexdigest()
    row = {
        "generation_id": str(uuid.uuid4()),
        "source_id": source_id,
        "navarea": navarea_name,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "parsed_count": int(counts.get("harvested", 0)),
        "active_count": int(counts.get("skip", 0)) + int(counts.get("new", 0)) + int(counts.get("update", 0)),
        "evidence_hash": evidence_hash,
        "details": counts,
    }
    try:
        supabase_client.table("ingestion_generations").insert(row).execute()
    except Exception as e:
        print(f"  ⚠️ Run ledger not recorded (ingestion_generations): {e}")


def _as_utc_datetime(value) -> Optional[datetime]:
    """Tolerantly converts DB/API date strings into aware UTC datetimes."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        parsed = parse_safe_iso_timestamp(value)
        if not parsed:
            return None
        try:
            dt = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_status_transition(supabase_client, canonical_id: str, patch: Dict[str, Any]) -> bool:
    try:
        (
            supabase_client.table("nav_warnings")
            .update(patch)
            .eq("canonical_id", canonical_id)
            .execute()
        )
        return True
    except Exception as e:
        print(f"  ❌ Failed to transition {canonical_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Cross-reference cancel resolution (audit finding F2, 2026-10-30).
#
# The parsers extract CANCEL cross-references onto the warning payload
# ('cross_refs'), but the sync layer never resolved them — 0/594 live
# rows transitioned on a published cancellation. A CANCEL reference is
# explicit, positive withdrawal evidence for its target (unlike
# scrape-absence), so it is valid even for partial harvests.
#
# Audit-first: without SYNC_APPLY_CANCEL_REFS the pass only resolves and
# LOGS (zero writes). With it, resolved ACTIVE targets transition
# status -> 'cancelled' (status_basis 'cancelled_by_cross_reference') —
# a status change, never a deletion (safeguard: never erase history).
# ---------------------------------------------------------------------------
def _carrier_number_year(carrier):
    """Splits a canonical id into its own number and year
    ("NAVAREA V 0622/26" -> ("0622", "26")) so padding variants of the
    carrier's own number can be excluded as self-references."""
    tail = carrier.rsplit(" ", 1)[-1] if carrier else ""
    num, _, year = tail.partition("/")
    if num and year:
        return num, year
    return "", ""


def _cancel_ref_target_ids(scraped_warnings, navarea_name, scraped_ids):
    """Pure mapping: CANCEL cross-refs -> {target canonical_id: carrier id}.

    Excludes self-references (including padding variants of the
    carrier's own number) and targets still present in the current
    feed (the feed listing a warning in force is fresher evidence than
    a cross-reference). Registers every padding variant of each ref
    number (as-is, zero-padded, unpadded digits) so verbatim-stored ids
    still match (audit F8). Deduplicates across warnings.
    """
    targets = {}
    for warn in scraped_warnings or []:
        if not isinstance(warn, dict):
            continue
        carrier = str(warn.get("canonical_id") or warn.get("warning_id") or "")
        carrier_num, carrier_year = _carrier_number_year(carrier)
        for ref in warn.get("cross_refs") or []:
            if not isinstance(ref, dict):
                continue
            number = str(ref.get("number") or "").strip()
            year = str(ref.get("year") or "").strip()
            if not number or not year:
                continue
            # F8 (2026-10-30, live-DB confirmed): some parser configs pad
            # cross-ref numbers (NGA XII memo refs are zero-padded to 3)
            # while the target's stored canonical_id is the memo header
            # VERBATIM ("NAVAREA XII 88/26" is a live row). A single-form
            # target can never resolve, so register every padding variant
            # of the ref number. Verified live: no source carries both a
            # padded and an unpadded form, so the variants are aliases of
            # the same warning, never distinct targets.
            digits = number.lstrip("0")
            for variant in dict.fromkeys(
                    (number, number.zfill(3), digits or number)):
                target = (navarea_name + " " + variant + "/" + year).strip()
                if (not target or target == carrier
                        or target in (scraped_ids or set())):
                    continue
                if (carrier_num and year == carrier_year
                        and variant.lstrip("0") == carrier_num.lstrip("0")):
                    # a padding variant of the carrier's own number is
                    # the carrier itself, not a target (F8 self-guard)
                    continue
                targets.setdefault(target, carrier)
    return targets


def resolve_cancel_cross_refs(supabase_client, scraped_warnings, navarea_name,
                              scraped_ids=None, apply_changes=None):
    """Resolves CANCEL cross-references into lifecycle evidence.

    Returns a counts dict: referenced / resolved / applied / unresolved
    (plus 'errors' when a transition fails, 'lookup_failed' when the DB
    lookup fails — the pass then aborts WITHOUT any transition).
    """
    counts = {"referenced": 0, "resolved": 0, "applied": 0, "unresolved": 0}
    if supabase_client is None:
        return counts
    if apply_changes is None:
        apply_changes = os.environ.get(
            "SYNC_APPLY_CANCEL_REFS", "").lower() in ("1", "true", "yes")
    targets = _cancel_ref_target_ids(
        scraped_warnings, navarea_name, scraped_ids)
    counts["referenced"] = len(targets)
    if not targets:
        return counts

    ids = sorted(targets)
    statuses = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            res = (supabase_client.table("nav_warnings")
                   .select("canonical_id, status")
                   .in_("canonical_id", chunk).execute())
            for r in (res.data or []):
                if isinstance(r, dict) and r.get("canonical_id"):
                    statuses[r["canonical_id"]] = r.get("status")
        except Exception as e:
            print("  ❌ Cancel cross-ref lookup failed (" + str(e) + "); "
                  "pass aborted WITHOUT any transition — re-run the sync.")
            counts["lookup_failed"] = True
            return counts

    now_stamp = datetime.now(timezone.utc).isoformat()
    for tid in ids:
        carrier = targets[tid]
        status = statuses.get(tid)
        if status is None:
            counts["unresolved"] += 1
            print("  ⚠️ Unresolved CANCEL ref: " + tid + " (referenced by "
                  + carrier + ") not found — verify warning id / zero-padding.")
            continue
        if status != "active":
            print("  ✔ " + tid + " already '" + str(status) + "' — CANCEL "
                  "ref from " + carrier + " is a no-op.")
            continue
        counts["resolved"] += 1
        if not apply_changes:
            print("  🔎 [audit-only] would mark " + tid + " cancelled "
                  "(CANCEL ref in " + carrier + "). "
                  "Set SYNC_APPLY_CANCEL_REFS=1 to apply.")
            continue
        patch = {
            "status": "cancelled",
            "status_basis": "cancelled_by_cross_reference",
            "status_changed_at": now_stamp,
        }
        if _apply_status_transition(supabase_client, tid, patch):
            counts["applied"] += 1
            print("  🗄️ Cancelled via cross-reference: " + tid
                  + " (CANCEL ref in " + carrier + ")")
        else:
            counts["errors"] = counts.get("errors", 0) + 1
    return counts


def _normalize_scraped_warning(
    source_id: str,
    navarea_name: str,
    raw_warn: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    """(master, geometries, warning_id) for one scraped warning.

    The single normalisation both the Supabase path and the file sink
    use, so a sink record holds exactly what the database would receive.
    """
    raw_warn_copy = dict(raw_warn)
    if "source_id" not in raw_warn_copy or not raw_warn_copy["source_id"]:
        raw_warn_copy["source_id"] = source_id
    if "navarea" not in raw_warn_copy or not raw_warn_copy["navarea"]:
        raw_warn_copy["navarea"] = navarea_name

    master = build_master_payload(raw_warn_copy)
    geoms = build_geometry_payloads(raw_warn_copy)
    return master, geoms, raw_warn_copy.get("warning_id", master["canonical_id"])


def _emit_to_sink(
    sink_path: str,
    source_id: str,
    navarea_name: str,
    scraped_warnings: List[Dict[str, Any]],
    full_snapshot: bool,
) -> bool:
    """HELM_SYNC_SINK path: one record per run, no database client."""
    entries = []
    for raw_warn in scraped_warnings or []:
        master, geoms, wid = _normalize_scraped_warning(source_id, navarea_name, raw_warn)
        entries.append({
            "warning_id": wid,
            "master": master,
            "geometries": geoms,
            "cross_refs": raw_warn.get("cross_refs") or [],
        })
    scraped_ids = {e["master"]["canonical_id"] for e in entries}
    # Carriers are keyed by canonical_id so a target maps to a full id
    # ("NAVAREA IX 430/26"), not a bare warning number.
    cancel_targets = _cancel_ref_target_ids(
        [{"canonical_id": e["master"]["canonical_id"], "cross_refs": e["cross_refs"]}
         for e in entries],
        navarea_name, scraped_ids)
    record = build_run_record(source_id, navarea_name, entries, full_snapshot, cancel_targets)
    write_run_record(sink_path, record)
    print(f"📝 Sync sink: {len(entries)} warning(s), {len(cancel_targets)} cancel "
          f"target(s) for {navarea_name} -> {sink_path} "
          f"(full_snapshot={full_snapshot}); no database client opened.")
    return True


def run_differential_sync(
    source_id: str,
    navarea_name: str,
    scraped_warnings: List[Dict[str, Any]],
    scraped_messages: List[Dict[str, Any]],
    supabase_client: Optional[Any] = None,
    full_snapshot: bool = True,
) -> bool:
    """Executes full differential synchronization against Supabase using canonical_id and atomic RPC.
    Returns True if completely successful, False if any error occurred.

    full_snapshot=False marks the feed as partial BY DESIGN (a harvest
    that skipped already-stored rows): the evidence-based cancel pass is
    disabled and scrape-absent warnings stay 'unobserved' (retained
    active) — absence is a skipping decision, not withdrawal evidence.

    With HELM_SYNC_SINK set (see shared/sync_sink.py) the run is written
    to the sink file instead and no database client is opened, even when
    one is passed in.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    print("=" * 65)
    print(f"⚡ {navarea_name} Differential Sync Engine ({source_id})")
    print("=" * 65)

    sink_path = configured_sink_path()
    if sink_path is not None:
        return _emit_to_sink(sink_path, source_id, navarea_name,
                             scraped_warnings, full_snapshot)

    if supabase_client is None:
        supabase_client = get_supabase_client()

    if supabase_client is None:
        print(f"ℹ️ Dry-run mode for {navarea_name}: {len(scraped_warnings)} warnings inspected.")
        return True

    if not scraped_warnings:
        print(f"❓ No warnings provided to sync for {navarea_name}: existing records left untouched.")
        _record_generation(
            supabase_client, source_id, navarea_name, started_at, "empty_feed",
            {"harvested": 0, "skip": 0, "new": 0, "update": 0,
             "cancelled": 0, "expired": 0, "unobserved": 0, "missing": 0},
            [],
        )
        return True

    # 1. Normalize scraped warnings into master payloads & geometries
    normalized_items: List[Tuple[Dict[str, Any], List[Dict[str, Any]], str]] = []
    scraped_for_diff: List[Dict[str, Any]] = []

    for raw_warn in scraped_warnings:
        master, geoms, wid = _normalize_scraped_warning(source_id, navarea_name, raw_warn)
        cid = master["canonical_id"]

        normalized_items.append((master, geoms, wid))
        scraped_for_diff.append({
            "canonical_id": cid,
            "warning_id": wid,
            "status": master["status"],
            "raw_text": master["raw_text"],
            "_master": master,
            "_geoms": geoms,
        })

    print("\n[1/4] Querying remote database state...")
    try:
        res_warn = (
            supabase_client.table("nav_warnings")
            .select("id, canonical_id, status, raw_text, valid_to, triage_tier")
            .eq("source_id", source_id)
            .execute()
        )
        existing_warnings = {
            r["canonical_id"]: r
            for r in (res_warn.data or [])
            if isinstance(r, dict) and r.get("canonical_id")
        }
    except Exception as e:
        print(f"❌ DB query failed on 'nav_warnings': {e}")
        return False

    try:
        res_raw = (
            supabase_client.table("raw_messages")
            .select("warning_id, checksum_sha256")
            .eq("source_id", source_id)
            .execute()
        )
        existing_checksums = {
            (r["warning_id"], r["checksum_sha256"])
            for r in (res_raw.data or [])
            if isinstance(r, dict) and "warning_id" in r and "checksum_sha256" in r
        }
    except Exception as e:
        print(f"⚠️ Query on 'raw_messages' returned: {e} (proceeding without checksum cache)")
        existing_checksums = set()

    print(f"  ✔ Found {len(existing_warnings)} existing warnings in database")
    print(f"  ✔ Found {len(existing_checksums)} existing checksums in database")

    # 2. Compute differential keyed on canonical_id
    diff = compute_differential(
        scraped_warnings=scraped_for_diff,
        scraped_messages=scraped_messages,
        existing_warnings=existing_warnings,
        existing_checksums=existing_checksums,
        key_fn=lambda w: w.get("canonical_id")
    )

    # Warnings active in DB but absent from this scrape (lifecycle input).
    # Computed here rather than in compute_differential so the shared
    # differential contract stays backward-compatible: absence alone
    # never mutates state.
    scraped_ids = {w["canonical_id"] for w in scraped_for_diff}
    missing_ids = [
        wid for wid, rec in existing_warnings.items()
        if rec.get("status") == "active" and wid not in scraped_ids
    ]

    # 2b. Geometry backfill guard (self-healing).
    # A warning skipped as 'unchanged' whose DB row is active + Tier A but
    # has ZERO rows in nav_warning_geometries is invisible in
    # nav_warnings_active_spatial (INNER JOIN against geometries). This
    # happens after an RPC/regression window that drops geometries while
    # raw-text checksums stay unchanged (observed 2026-10: five regions
    # silently disappeared from the map). Force such warnings through the
    # RPC again so their geometry payloads get rewritten from this parse.
    geometry_seen = None  # None = check unavailable; set() = checked
    # NOTE: compute_differential returns 'skip' as a flat list of canonical_id
    # strings (unlike 'new'/'update', which hold (warning, msg) tuples).
    skipped_cids = set(diff["skip"]) if diff["skip"] else set()
    backfill_candidate_ids = [
        rec["id"]
        for w in scraped_for_diff
        if w["canonical_id"] in skipped_cids and w.get("_geoms")
        for rec in [existing_warnings.get(w["canonical_id"]) or {}]
        if rec.get("id") and rec.get("status") == "active" and rec.get("triage_tier") == "A"
    ]
    if backfill_candidate_ids:
        geometry_seen = set()
        try:
            # PostgREST caps .in_() lists; query in chunks of 100
            for i in range(0, len(backfill_candidate_ids), 100):
                chunk = backfill_candidate_ids[i:i + 100]
                res_geom = (
                    supabase_client.table("nav_warning_geometries")
                    .select("warning_id")
                    .in_("warning_id", chunk)
                    .execute()
                )
                geometry_seen.update(
                    g.get("warning_id")
                    for g in (res_geom.data or [])
                    if isinstance(g, dict) and g.get("warning_id")
                )
        except Exception as e:
            print(f"⚠️ Geometry backfill check failed ({e}); guard skipped this run.")
            geometry_seen = None

    if geometry_seen is not None and diff["skip"]:
        # Candidates (active + Tier A + fresh geoms) whose DB id has zero
        # geometry rows get reclassified from 'skip' into 'update'.
        backfilled_cids = set()
        for cid in diff["skip"]:
            if cid not in skipped_cids:
                continue
            rec = existing_warnings.get(cid) or {}
            if rec.get("id") in geometry_seen:
                continue
            backfilled_cids.add(cid)
            print(f"  🔁 Geometry backfill: {cid} is Tier A active with zero geometry rows → re-upserting")
        if backfilled_cids:
            wrapper_by_cid = {w["canonical_id"]: w for w in scraped_for_diff}
            for cid in sorted(backfilled_cids):
                wrapper = wrapper_by_cid.get(cid)
                if wrapper:
                    # 'update' entries are (warning, msg) tuples; the raw message
                    # is already recorded (checksum matched), so pass an empty
                    # msg to skip the redundant raw_messages insert.
                    diff["update"].append((wrapper, {}))
            diff["skip"] = [cid for cid in diff["skip"] if cid not in backfilled_cids]

    print(
        f"\n[2/4] Differential analysis: "
        f"{len(diff['skip'])} skipped, {len(diff['new'])} new, "
        f"{len(diff['update'])} update, {len(missing_ids)} missing-from-scrape"
    )

    has_errors = False

    # 3. Process new and updated records
    to_write = diff["new"] + diff["update"]
    if to_write:
        print(f"\n[3/4] Persisting {len(to_write)} records...")
        for diff_entry in to_write:
            warn_wrapper, msg = diff_entry
            master = warn_wrapper.get("_master")
            geoms = warn_wrapper.get("_geoms", [])
            cid = master["canonical_id"]
            wid = warn_wrapper.get("warning_id", cid)

            # Persist raw_message audit row
            if msg and isinstance(msg, dict):
                clean_msg = {k: v for k, v in msg.items() if k in VALID_RAW_COLUMNS}
                clean_msg.setdefault("source_id", source_id)
                clean_msg.setdefault("warning_id", wid)
                clean_msg.setdefault("full_raw_text", master["raw_text"])
                try:
                    supabase_client.table("raw_messages").insert(clean_msg).execute()
                    print(f"  ✅ [raw_messages] {wid}")
                except Exception as msg_err:
                    # Ignore unique violation if message already recorded
                    if "duplicate key" not in str(msg_err).lower():
                        print(f"  ⚠️ [raw_messages] {wid}: {msg_err}")

            # Atomic master + geometry upsert
            success = upsert_warning_atomic(supabase_client, master, geoms)
            if success:
                print(f"  ✨ [nav_warnings + geoms] {cid} (Triage: {master['triage_tier']}, Geoms: {len(geoms)})")
            else:
                print(f"  ❌ Failed to upsert: {cid}")
                has_errors = True

    # 3b. Cross-reference cancel resolution (audit finding F2): CANCEL
    #     cross-refs carried on scraped warnings are explicit withdrawal
    #     evidence for their targets. Audit-first: without
    #     SYNC_APPLY_CANCEL_REFS this pass resolves + logs only.
    cancel_ref_counts = resolve_cancel_cross_refs(
        supabase_client, scraped_warnings, navarea_name,
        scraped_ids=scraped_ids,
    )
    if cancel_ref_counts.get("referenced"):
        print("[3b] Cross-ref cancels: "
              + str(cancel_ref_counts["referenced"]) + " referenced, "
              + str(cancel_ref_counts["resolved"]) + " resolved, "
              + str(cancel_ref_counts["applied"]) + " applied, "
              + str(cancel_ref_counts["unresolved"]) + " unresolved")
        if cancel_ref_counts.get("errors"):
            has_errors = True

    # 4. Evidence-based lifecycle transitions for warnings missing from the
    #    scrape. Archiving is a status change, never a deletion:
    #      - 'expired'   : the warning's own valid_to date has passed
    #                      (evidence: validity period in the DB record).
    #      - 'cancelled' : absent from a CREDIBLE complete snapshot
    #                      (evidence: feed size >= SYNC_CANCEL_MIN_FEED_COUNT
    #                      and >= SYNC_CANCEL_MIN_FEED_FRACTION of DB actives).
    #                      Anything else leaves the warning active.
    missing = missing_ids
    expired_ids: List[str] = []
    unobserved_ids: List[str] = []

    now_dt = datetime.now(timezone.utc)
    for wid in missing:
        record = existing_warnings.get(wid, {})
        valid_to_dt = _as_utc_datetime(record.get("valid_to"))
        if valid_to_dt and valid_to_dt <= now_dt:
            expired_ids.append(wid)
        else:
            unobserved_ids.append(wid)

    feed_size = len(scraped_for_diff)
    total_actives = feed_size + len(missing)
    feed_credible = (
        full_snapshot
        and feed_size >= CANCEL_MIN_FEED_COUNT
        and total_actives > 0
        and feed_size >= CANCEL_MIN_FEED_FRACTION * total_actives
    )
    if not full_snapshot:
        print("  ⏸ Partial harvest (skip-stored-rows): snapshot not "
              "credible as complete — cancel pass disabled; scrape-absent "
              "warnings stay 'unobserved' and active.")
    cancelled_ids: List[str] = []
    if feed_credible:
        cancelled_ids = unobserved_ids
        unobserved_ids = []

    print(f"\n[4/4] Lifecycle pass: {len(missing)} missing-from-scrape → "
          f"{len(cancelled_ids)} cancelled, {len(expired_ids)} expired, "
          f"{len(unobserved_ids)} left active (unobserved)")
    if unobserved_ids:
        print(
            f"  ⏸ Cancel guard active: feed not credible as a complete snapshot "
            f"({feed_size} scraped vs {total_actives} DB actives; thresholds: "
            f">={CANCEL_MIN_FEED_COUNT} and >={CANCEL_MIN_FEED_FRACTION:.0%}). "
            f"Existing warnings left untouched."
        )

    now_stamp = datetime.now(timezone.utc).isoformat()
    for wid in expired_ids:
        if _apply_status_transition(supabase_client, wid, {
            "status": "expired",
            "status_basis": "expired_by_validity",
            "status_changed_at": now_stamp,
        }):
            print(f"  🕰️ Expired (valid_to passed): {wid}")
        else:
            has_errors = True

    for wid in cancelled_ids:
        if _apply_status_transition(supabase_client, wid, {
            "status": "cancelled",
            "status_basis": "source_absence_verified_snapshot",
            "status_changed_at": now_stamp,
        }):
            print(f"  🗄️ Archived (cancelled, absent from verified snapshot): {wid}")
        else:
            has_errors = True

    for wid in unobserved_ids:
        print(f"  👁️ Unobserved this run, retained active: {wid}")

    counts = {
        "harvested": len(scraped_warnings),
        "skip": len(diff["skip"]),
        "new": len(diff["new"]),
        "update": len(diff["update"]),
        "cancelled": len(cancelled_ids),
        "expired": len(expired_ids),
        "unobserved": len(unobserved_ids),
        "missing": len(missing),
        "cancel_refs": cancel_ref_counts,
        "cancel_guard": {
            "min_feed_count": CANCEL_MIN_FEED_COUNT,
            "min_feed_fraction": CANCEL_MIN_FEED_FRACTION,
            "feed_credible": feed_credible,
            "full_snapshot": full_snapshot,
        },
    }
    _record_generation(
        supabase_client, source_id, navarea_name, started_at,
        "failed" if has_errors else "succeeded",
        counts,
        [w.get("canonical_id", "") for w in scraped_for_diff],
    )

    print("\n" + "=" * 65)
    print(
        f"📊 {navarea_name} Summary: "
        f"{counts['harvested']} harvested, {counts['skip']} unchanged, "
        f"{counts['new']} inserted, {counts['update']} updated, "
        f"{counts['cancelled']} cancelled, {counts['expired']} expired, "
        f"{counts['unobserved']} unobserved (retained active)"
    )
    print("=" * 65)

    return not has_errors

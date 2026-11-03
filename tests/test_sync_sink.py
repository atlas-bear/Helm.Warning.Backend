#!/usr/bin/env python3
"""tests/test_sync_sink.py — HELM_SYNC_SINK file sink.

Covers:
  * HELM_SYNC_SINK parsing: unset -> None, 'jsonl:<path>' -> path,
    anything else -> ValueError (never a silent fall-back to the DB).
  * run_differential_sync with the sink set writes ONE record per run and
    opens no database client, even when one is passed in.
  * The record holds exactly what the Supabase path would write: the
    master payloads equal the ones upsert_warning_atomic receives on a
    database run of the same input (the contract the sink exists for).
  * full_snapshot and CANCEL cross-reference targets are carried through,
    an empty feed still writes a record, and runs append.
  * The NAVAREA II pipeline (which writes per warning) writes one record
    per run, and marks a run with an undecodable dataset as partial.
"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from unittest.mock import MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from shared import sync_engine
from shared.sync_engine import run_differential_sync
from shared.sync_sink import RECORD_SCHEMA, SINK_ENV, configured_sink_path
from scrapers.navarea_ii import run_navarea_ii_pipeline


def _warning(number, **extra):
    rec = {
        "warning_id": f"{number}/26",
        "navarea": "NAVAREA IX",
        "title": f"NAVAREA IX {number}/26",
        "raw_text": f"NAVAREA IX {number}/26\nARABIAN SEA.\n1. TEST WARNING {number}.",
        "valid_from": "2026-10-27T08:00:00Z",
        "categories": ["military"],
        "latitude": 24.5,
        "longitude": 61.25,
    }
    rec.update(extra)
    return rec


def _read_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class _Exec:
    def __init__(self, data):
        self.data = data


def _empty_db_client():
    """A supabase double whose tables are empty (every scraped row is new)."""
    client = MagicMock()

    def table(name):
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value = _Exec([])
        t.select.return_value.in_.return_value.execute.return_value = _Exec([])
        t.insert.return_value.execute.return_value = _Exec([])
        t.update.return_value.eq.return_value.execute.return_value = _Exec([])
        return t

    client.table.side_effect = table
    return client


class TestSinkConfiguration(unittest.TestCase):
    def test_unset_means_no_sink(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SINK_ENV, None)
            self.assertIsNone(configured_sink_path())

    def test_jsonl_path_is_returned(self):
        with mock.patch.dict(os.environ, {SINK_ENV: "jsonl:/tmp/helm/runs.jsonl"}):
            self.assertEqual(configured_sink_path(), "/tmp/helm/runs.jsonl")

    def test_malformed_value_raises_instead_of_falling_back(self):
        for bad in ("/tmp/runs.jsonl", "jsonl:", "csv:/tmp/runs.csv", "jsonl"):
            with mock.patch.dict(os.environ, {SINK_ENV: bad}):
                with self.assertRaises(ValueError, msg=bad):
                    configured_sink_path()


class TestDifferentialSyncSink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "out", "runs.jsonl")
        self._env = mock.patch.dict(os.environ, {SINK_ENV: f"jsonl:{self.path}"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_writes_one_record_and_opens_no_client(self):
        passed_client = MagicMock()
        with mock.patch.object(sync_engine, "get_supabase_client",
                               side_effect=AssertionError("client opened")):
            ok = run_differential_sync("manual-navarea-ix", "NAVAREA IX",
                                       [_warning(424), _warning(425)], [],
                                       supabase_client=passed_client)
        self.assertTrue(ok)
        self.assertEqual(passed_client.mock_calls, [])
        records = _read_records(self.path)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["schema"], RECORD_SCHEMA)
        self.assertEqual(rec["source_id"], "manual-navarea-ix")
        self.assertEqual(rec["navarea"], "NAVAREA IX")
        self.assertTrue(rec["full_snapshot"])
        self.assertEqual([w["master"]["canonical_id"] for w in rec["warnings"]],
                         ["NAVAREA IX 424/26", "NAVAREA IX 425/26"])
        self.assertEqual(rec["warnings"][0]["geometries"][0]["geojson"],
                         {"type": "Point", "coordinates": [61.25, 24.5]})

    def test_masters_match_what_the_database_path_writes(self):
        scraped = [_warning(424), _warning(425, latitude=None, longitude=None)]

        upserted = []

        def capture(client, master, geoms):
            upserted.append((master, geoms))
            return True

        with mock.patch.dict(os.environ, {SINK_ENV: ""}), \
             mock.patch.object(sync_engine, "upsert_warning_atomic", side_effect=capture):
            self.assertTrue(run_differential_sync(
                "manual-navarea-ix", "NAVAREA IX", scraped, [],
                supabase_client=_empty_db_client()))

        run_differential_sync("manual-navarea-ix", "NAVAREA IX", scraped, [])
        sink = _read_records(self.path)[0]["warnings"]

        self.assertEqual(len(upserted), 2)
        self.assertEqual([w["master"] for w in sink], [m for m, _ in upserted])
        self.assertEqual([w["geometries"] for w in sink], [g for _, g in upserted])

    def test_partial_flag_and_cancel_targets_are_carried(self):
        carrier = _warning(430, cross_refs=[{"number": "401", "year": "26"}])
        run_differential_sync("manual-navarea-ix", "NAVAREA IX", [carrier], [],
                              full_snapshot=False)
        rec = _read_records(self.path)[0]
        self.assertFalse(rec["full_snapshot"])
        self.assertEqual(rec["warnings"][0]["cross_refs"],
                         [{"number": "401", "year": "26"}])
        self.assertEqual(rec["cancel_targets"].get("NAVAREA IX 401/26"), "NAVAREA IX 430/26")
        self.assertNotIn("NAVAREA IX 430/26", rec["cancel_targets"])

    def test_sink_means_no_client_for_any_caller(self):
        # Without the sink, missing credentials exit the process (code 2).
        no_creds = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "ALLOW_DRY_RUN": ""}
        with mock.patch.dict(os.environ, no_creds):
            self.assertIsNone(sync_engine.get_supabase_client())
            with mock.patch.dict(os.environ, {SINK_ENV: ""}):
                with self.assertRaises(SystemExit):
                    sync_engine.get_supabase_client()

    def test_region_sync_module_runs_end_to_end_without_credentials(self):
        # navarea_8_sync opens a client AFTER the engine for its supersede
        # pass; under the sink that must be None, not a credential exit.
        from scripts import navarea_8_sync
        parsed = os.path.join(self._tmp.name, "parsed_navarea_8_warnings.json")
        warn = _warning(101, navarea="NAVAREA VIII", warning_id="101/26",
                        cross_refs=[{"number": "90", "year": "26"}])
        with open(parsed, "w", encoding="utf-8") as f:
            json.dump({"raw_messages": [], "nav_warnings": [warn]}, f)
        no_creds = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "ALLOW_DRY_RUN": ""}
        with mock.patch.dict(os.environ, no_creds):
            self.assertTrue(navarea_8_sync.sync_to_supabase(parsed, full_snapshot=False))
        rec = _read_records(self.path)[0]
        self.assertEqual(rec["source_id"], "nho-navarea-viii")
        self.assertFalse(rec["full_snapshot"])
        self.assertEqual(rec["cancel_targets"].get("NAVAREA VIII 90/26"), "NAVAREA VIII 101/26")

    def test_credential_gated_sync_modules_reach_the_sink(self):
        # navarea_generic_sync (IX, III, VII, X, XIX, ...) and baltic_sync
        # check credentials themselves before the engine; under the sink
        # they must still emit, not report a dry run.
        from scripts import navarea_generic_sync, baltic_sync
        generic = os.path.join(self._tmp.name, "parsed.json")
        with open(generic, "w", encoding="utf-8") as f:
            json.dump({"raw_messages": [], "nav_warnings": [_warning(424)],
                       "memo": {"source_id": "manual-navarea-ix", "navarea": "IX"}}, f)
        baltic = os.path.join(self._tmp.name, "parsed_baltic.json")
        with open(baltic, "w", encoding="utf-8") as f:
            json.dump({"raw_messages": [], "nav_warnings": [
                _warning(7, navarea="BALTIC", warning_id="7/26")]}, f)
        no_creds = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "ALLOW_DRY_RUN": ""}
        with mock.patch.dict(os.environ, no_creds):
            self.assertTrue(navarea_generic_sync.sync_to_supabase(generic, full_snapshot=False))
            self.assertTrue(baltic_sync.sync_to_supabase(baltic))
        records = _read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["navarea"], "NAVAREA IX")
        self.assertFalse(records[0]["full_snapshot"])
        self.assertEqual(len(records[1]["warnings"]), 1)

    def test_empty_feed_still_writes_a_record_and_runs_append(self):
        run_differential_sync("manual-navarea-ix", "NAVAREA IX", [], [])
        run_differential_sync("manual-navarea-ix", "NAVAREA IX", [_warning(424)], [])
        records = _read_records(self.path)
        self.assertEqual([len(r["warnings"]) for r in records], [0, 1])


def _exchange_set(*gml_docs, broken=0):
    items = []
    for i, gml in enumerate(gml_docs):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"124FR00N0{i}.gml", gml)
        items.append({"data": base64.b64encode(buf.getvalue()).decode("ascii")})
    items.extend({"data": "not base64 !!"} for _ in range(broken))
    return {"dataResponseObject": items,
            "pagination": {"totalItems": len(items)},
            "responseText": ""}


_GML = ("<S124><warningNumber>{n}/26</warningNumber><generalArea>ENGLISH CHANNEL"
        "</generalArea><text>UNLIT BUOY REPORTED</text><posList>49.5 -2.0</posList></S124>")


class TestNavareaIISink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "runs.jsonl")
        self._env = mock.patch.dict(os.environ, {SINK_ENV: f"jsonl:{self.path}"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_one_record_per_run_without_database_writes(self):
        with mock.patch("scrapers.navarea_ii.upsert_warning_atomic",
                        side_effect=AssertionError("database write")):
            summary = run_navarea_ii_pipeline(
                mock_payload=_exchange_set(_GML.format(n=777), _GML.format(n=778)))
        self.assertEqual(summary["synced"], 2)
        records = _read_records(self.path)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["navarea"], "NAVAREA II")
        self.assertTrue(rec["full_snapshot"])
        self.assertEqual(sorted(w["master"]["canonical_id"] for w in rec["warnings"]),
                         ["NAVAREA II 777/26", "NAVAREA II 778/26"])
        for w in rec["warnings"]:
            self.assertIn("publication_time", w)

    def test_entry_carries_the_s124_publication_time(self):
        # NAVAREA II's issue time is its S-124 publication time, which is also
        # its valid_from (II states no validity windows).
        gml = _GML.replace("</S124>", "<publicationTime>2026-11-02T13:49:33Z</publicationTime></S124>")
        run_navarea_ii_pipeline(mock_payload=_exchange_set(gml.format(n=779)))
        w = _read_records(self.path)[0]["warnings"][0]
        self.assertTrue(w["publication_time"].startswith("2026-11-02T13:49:33"))
        self.assertEqual(w["publication_time"], w["master"]["valid_from"])

    def test_undecodable_dataset_makes_the_run_partial(self):
        run_navarea_ii_pipeline(mock_payload=_exchange_set(_GML.format(n=777), broken=1))
        rec = _read_records(self.path)[0]
        self.assertFalse(rec["full_snapshot"])
        self.assertEqual(len(rec["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()

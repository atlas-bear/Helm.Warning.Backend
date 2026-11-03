#!/usr/bin/env python3
"""
tests/test_dtg.py — Issue time (date-time group) of a warning.

The universal pipeline and NAVAREA VIII emitted no issue time and NAVAREA
XI's "Date:" field was not parseable, so publication_time was missing and
build_master_payload had no valid_from to fall back to. Fixtures are live
warnings (2026-11-03). Parsing is fail-closed: anything not exactly a
date-time group yields None.
"""

import os
import sys
import tempfile
import json
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.dtg import parse_dtg, find_dtg_line
from scripts.navarea_universal_parser import parse_bulletin_text
from scripts import navarea_8_parser
from scripts.navarea_xi_parser import parse_warning as parse_xi_warning
from shared.models import build_master_payload
from shared import sync_engine
from shared.sync_sink import SINK_ENV


IX_280 = """NAVAREA IX 280/26
CENTRAL GULF
QATAR
CHARTS BA 2523 AND 2886

1.\tSUBSEA SLEEPER/ PIPELINE INSTALLATION WILL BE CONDUCTED
FROM 10 AUG TO 10 NOV 26 BY DERRICK BARGE 30
SUPPORTED BY MULTI VESSELS IN AREA BOUNDED
BY FOLLOWING COORDINATES:

 26-25.30N 052-12.50E
 26-31.70N 051-59.40E
 26-23.50N 051-59.40E
 26-18.70N 052-12.40E

2.\tCANCEL THIS MSG 102359 UTC NOV 26

MARINERS CAUTIONED

091500 UTC AUG 2026"""

IX_093 = """NAVAREA IX 093/26
CENTRAL GULF
QATAR
CHARTS BA 2523, 2837, 2886 AND 3781

TEMPORARY NAVIGATION LIGHT INSTALLED ON PA JACKET IN
POSITION 26-40.22N 051-17.13E (.) LIGHT CHARACTERISTICS
WHITE FL 15S. MARINERS CAUTIONED

091500 UTC MAR 2026"""

III_036 = """NAVAREA III - 0036/26
04 March 2026 00:00 UTC
MEDITERRANEAN SEA
NATO OPERATION SEA GUARDIAN
NATO OPERATION SEA GUARDIAN (OSG) IN PROGRESS UNTIL FURTHER NOTICE TO DETER TERRORISM AND MITIGATE MARITIME SECURITY THREATS."""


class TestParseDtg(unittest.TestCase):
    def test_ix_four_digit_year(self):
        self.assertEqual(parse_dtg("031500 UTC MAR 2026"), "2026-03-03T15:00:00+00:00")

    def test_ix_two_digit_year(self):
        self.assertEqual(parse_dtg("161500 UTC OCT 24"), "2024-10-16T15:00:00+00:00")

    def test_ix_no_zone(self):
        # NAVAREA IX 50/26 closes with "061500 FEB 26".
        self.assertEqual(parse_dtg("061500 FEB 26"), "2026-02-06T15:00:00+00:00")

    def test_ix_byte_order_mark(self):
        # NAVAREA IX 311/24 and 361/24 carry a BOM before the DTG.
        self.assertEqual(parse_dtg("﻿151500 UTC AUG 2024"), "2024-08-15T15:00:00+00:00")

    def test_viii_slash(self):
        self.assertEqual(parse_dtg("031006Z/JUN 26"), "2026-06-03T10:06:00+00:00")

    def test_iii_long_form(self):
        self.assertEqual(parse_dtg("25 February 2022 00:00 UTC"), "2022-02-25T00:00:00+00:00")

    def test_xi_date_field_hour_only(self):
        self.assertEqual(parse_dtg("2026/11/02 12 UTC"), "2026-11-02T12:00:00+00:00")

    def test_xi_date_field_with_minutes(self):
        self.assertEqual(parse_dtg("2026-10-09 07:30 UTC"), "2026-10-09T07:30:00+00:00")

    def test_missing_year_uses_default(self):
        self.assertEqual(parse_dtg("301000Z NOV", default_year="26"), "2026-11-30T10:00:00+00:00")

    def test_missing_year_without_default_is_none(self):
        self.assertIsNone(parse_dtg("301000Z NOV"))

    def test_impossible_date_is_none(self):
        self.assertIsNone(parse_dtg("310200 UTC FEB 26"))

    def test_impossible_time_is_none(self):
        self.assertIsNone(parse_dtg("032500 UTC MAR 26"))

    def test_unknown_month_is_none(self):
        self.assertIsNone(parse_dtg("031500 UTC XYZ 26"))

    def test_sentence_is_none(self):
        self.assertIsNone(parse_dtg("CANCEL THIS MSG 060200Z NOV."))

    def test_bare_date_is_not_a_long_form_dtg(self):
        self.assertIsNone(parse_dtg("14 NOV 2026"))

    def test_empty(self):
        self.assertIsNone(parse_dtg(None))
        self.assertIsNone(parse_dtg("   "))


# IX_093 and IX_280 are written with the DTG CLOSING the warning, to exercise that
# layout; the coordinator's documents open with it instead (see
# test_ix_documents_open_with_their_dtg). The times are each warning's own, from the
# coordinator's in-force table.


class TestFindDtgLine(unittest.TestCase):
    def test_closing_line(self):
        self.assertEqual(find_dtg_line(IX_093), "2026-03-09T15:00:00+00:00")

    def test_second_line(self):
        self.assertEqual(find_dtg_line(III_036), "2026-03-04T00:00:00+00:00")

    def test_times_inside_sentences_are_ignored(self):
        # IX 280/26 states a validity window and a self-cancel time before its DTG.
        self.assertEqual(find_dtg_line(IX_280), "2026-08-09T15:00:00+00:00")

    def test_no_dtg_line(self):
        self.assertIsNone(find_dtg_line("NAVAREA IX 397/21\n4.  CANCEL NAVAREA IX WARNING 215 OF 2020"))


class TestParsersEmitPublicationTime(unittest.TestCase):
    def _universal(self, text, region):
        warnings = parse_bulletin_text(text, region)["nav_warnings"]
        self.assertEqual(len(warnings), 1)
        return warnings[0]

    def test_ix_without_validity_window(self):
        warn = self._universal(IX_093, "ix")
        self.assertEqual(warn["publication_time"], "2026-03-09T15:00:00+00:00")
        # build_master_payload's existing fallback now has something to use.
        self.assertTrue(build_master_payload(warn)["valid_from"].startswith("2026-03-09T15:00"))

    def test_ix_validity_window_kept_apart_from_issue_time(self):
        warn = self._universal(IX_280, "ix")
        self.assertEqual(warn["publication_time"], "2026-08-09T15:00:00+00:00")
        self.assertEqual(warn["valid_from"], "2026-08-10")

    def test_ix_documents_open_with_their_dtg(self):
        # The IX harvest joins the coordinator's documents, and each opens with its
        # DTG above the header (2026-11-03). Split on headers alone, 426/26 carried
        # 425/26's time; the coordinator's in-force table settled which is which.
        harvest = (
            "021720 UTC NOV 2026\n"
            "NAVAREA IX 426/26\nARABIAN SEA\nPAKISTAN\n\n"
            "1. LIVE WEAPON FIRING SCHEDULED ON 03 NOV 26 IN 25-54.71N 065-52.02E\n\n"
            "2. CANCEL THIS MSG 030800 UTC NOV 26\n\n"
            "021500 UTC NOV 2026\n"
            "NAVAREA IX 425/26\nSTRAIT OF HORMUZ\n\n"
            "TUG BOAT REPORTED SUNK IN 26-22.55N 056-20.47E (.) MARINERS CAUTIONED\n\n"
            "011500 UTC NOV 2026\n"
            "NAVAREA IX 424/26\nARABIAN SEA\n\nBUOY ADRIFT IN 24-00.00N 062-00.00E\n")
        warnings = {w["warning_id"]: w for w in parse_bulletin_text(harvest, "ix")["nav_warnings"]}
        self.assertEqual(warnings["NAVAREA IX 426/26"]["publication_time"], "2026-11-02T17:20:00+00:00")
        self.assertEqual(warnings["NAVAREA IX 425/26"]["publication_time"], "2026-11-02T15:00:00+00:00")
        self.assertEqual(warnings["NAVAREA IX 424/26"]["publication_time"], "2026-11-01T15:00:00+00:00")
        self.assertNotIn("021500 UTC NOV 2026", warnings["NAVAREA IX 426/26"]["raw_text"])
        self.assertTrue(warnings["NAVAREA IX 425/26"]["raw_text"].endswith("021500 UTC NOV 2026"))

    def test_a_dtg_closing_its_own_warning_stays_with_it(self):
        # Separated from the next header by a blank line, a DTG closes the warning above.
        bulletin = IX_093 + "\n\n" + IX_280
        warnings = {w["warning_id"]: w for w in parse_bulletin_text(bulletin, "ix")["nav_warnings"]}
        self.assertEqual(warnings["NAVAREA IX 093/26"]["publication_time"], "2026-03-09T15:00:00+00:00")
        self.assertEqual(warnings["NAVAREA IX 280/26"]["publication_time"], "2026-08-09T15:00:00+00:00")

    def test_iii(self):
        warn = self._universal(III_036, "iii")
        self.assertEqual(warn["publication_time"], "2026-03-04T00:00:00+00:00")

    def test_viii_dtg_sits_above_its_header(self):
        # The NHO bulletin (2026-11-02) puts each DTG ABOVE its warning's header. Split on
        # headers alone, 499/26 carried 664/26's time; an independent feed confirmed that
        # every warning had read its successor's issue time.
        dashes = "-" * 60
        bulletin = (
            "NAVAREA VIII - WARNINGS IN FORCE AS ON 02 NOV 2026 \n \n" + dashes + "\n"
            "241631Z/APR 26 \n"
            "NAVAREA VIII -499/26 \n" + dashes + "\n"
            "  INDIA EAST COAST - OFF KRISHNAPATNAM (.) CHARTS IN 31 356 391 INT 756 (.) \n"
            "SURFACE BUOY MOORED AT 14-05.21N 080-28.50E (.) WIDE BERTH REQUESTED \n" + dashes + "\n"
            "031006Z/JUN 26 \n"
            "NAVAREA VIII -664/26 \n" + dashes + "\n"
            "  INDIA WEST COAST - OFF MUMBAI (.) DATA BUOY MOORED AT 18-55.00N 072-30.00E \n")
        warnings = {w["warning_id"]: w for w in navarea_8_parser.parse_bulletin_text(bulletin)["nav_warnings"]}
        first, second = warnings["NAVAREA VIII 499/26"], warnings["NAVAREA VIII 664/26"]
        self.assertEqual(first["publication_time"], "2026-04-24T16:31:00+00:00")
        self.assertEqual(second["publication_time"], "2026-06-03T10:06:00+00:00")
        # Each warning's text carries its own DTG, not its successor's.
        self.assertTrue(first["raw_text"].endswith("241631Z/APR 26"))
        self.assertNotIn("031006Z", first["raw_text"])
        self.assertTrue(second["raw_text"].endswith("031006Z/JUN 26"))

    def test_xi(self):
        raw = ("NO.26-0451       Date:2026/10/09 07 UTC\n"
               "NORTH PACIFIC, OKINOTORI SHIMA.\n"
               "HATCH COVER ADRIFT IN 16-54.1N 134-39.5E")
        _, warn, _ = parse_xi_warning(raw, "NAVAREA XI NO.26-0451")
        self.assertEqual(warn["publication_time"], "2026-10-09T07:00:00+00:00")


class TestSinkCarriesPublicationTime(unittest.TestCase):
    def test_entry_has_issue_time_separate_from_valid_from(self):
        warn = parse_bulletin_text(IX_280, "ix")["nav_warnings"][0]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            with mock.patch.dict(os.environ, {SINK_ENV: f"jsonl:{path}"}):
                self.assertTrue(sync_engine.run_differential_sync(
                    "manual-navarea-ix", "NAVAREA IX", [warn], [], full_snapshot=True))
            with open(path, encoding="utf-8") as f:
                entry = json.loads(f.readline())["warnings"][0]
        self.assertEqual(entry["publication_time"], "2026-08-09T15:00:00+00:00")
        self.assertTrue(entry["master"]["valid_from"].startswith("2026-08-10"))


if __name__ == "__main__":
    unittest.main()

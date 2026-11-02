#!/usr/bin/env python3
"""tests/test_navarea_ix_scraper.py — NAVAREA IX NHO Pakistan scraper tests.

Reproduces the live NHO page structure in miniature (nav menu, intro
prose, the 6-column metadata table) and verifies the extract -> fetch ->
concatenate -> universal-parser chain end to end, offline, with an
injected document fetcher.

Also covers the stored-state row skipping (strategy point 0): rows
whose warning is already in the DB (source 'manual-navarea-ix',
canonical 'NAVAREA IX nnn/yy') are not re-fetched; new, recent
(recheck window) and unparseable-DTG rows are. Unknown stored state
(dry run / no credentials / query error) fails open to a full fetch.

Also covers the legacy WWNWS prosign header 'NAVAREA IX (.) nnn/yy (.)'
of the site's 2023-era files (live CI failure 2026-10-28: the correct
376/23 document was refused as a wrong link because of the '(.)'
separator) — it must validate AND parse like any other warning, while
wrong numbers/years stay refused — and the run-level flow control of
Alfred's revised IX strategy (2026-10-28): a run stops CLEANLY at
MAX_REQUESTS_PER_RUN requests or on the FIRST HTTP 429 (stored rows
kept, exit 0; no 20/40/60s retry ladder), and pre-2025 warnings are
archived (status 'expired', never published).

The fixture mirrors the raw server HTML as re-verified live
2026-10-27: some ATTACHMENT cells carry a real href (412/26), some carry
only a link-less 'View Document' placeholder while the row DTG allows
the document URL to be derived (410/26 with a SEC file, 394/26 with an
EC-only file), and one row has neither a link nor a parseable DTG
(378/26 — its DTG cell is malformed on the live page too) and must be
skipped loudly. The linked files mirror the live plain-text documents:
DTG line, 'NAVAREA IX nnn/yy' header, region / area / charts lines,
numbered paragraphs.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from scripts import navarea_ix_scraper
from scripts.navarea_ix_scraper import (
    _candidate_doc_urls,
    _dtg_is_recent,
    _filter_stored_rows,
    _known_ix_keys_from_file,
    _resolve_stored_keys,
    _stored_ix_keys,
    assemble_bulletin,
    extract_rows,
    fetch_url,
)
from scripts.navarea_universal_parser import parse_bulletin_text

_BASE = ("https://hydrography.paknavy.gov.pk/wp-content/plugins/"
         "navarea-warnings/custom_uploaded_warnings_for_navarea/")

_MINI_HTML = """<html><body>
<ul><li>Home</li><li>NAVAREA IX WARNINGS</li></ul>
<p>Hydrographer of Pakistan (HoP) leads both HQ NAVAREA IX ...</p>
<table>
<thead><tr><th>S.NO</th><th>REGION/AREA</th><th>DTG</th>
<th>WARNING #</th><th>DESCRIPTION</th><th>ATTACHMENT</th></tr></thead>
<tbody>
<tr><td>1</td><td>CENTRAL GULF/QATAR</td><td>271500 UTC OCT 2026</td>
<td>412/26</td><td>TOWING OPERATION</td>
<td><a href="__BASE__20261027SEC%20412.txt">View Document</a></td></tr>
<tr><td>3</td><td>NAVAREA IX</td><td>271500 UTC OCT 2026</td>
<td>410/26</td><td>SELF CANCELING</td>
<td><span class="view-document">View Document</span></td></tr>
<tr><td>13</td><td>SOUTHERN GULF/ UAE</td><td>211500 UTC OCT 2026</td>
<td>394/26</td><td>BOUYS UNLIT</td>
<td>View Document</td></tr>
<tr><td>16</td><td>NORTHERN GULF KUWAIT</td>
<td>NORTHERN GULF KUWAIT</td><td>378/26</td>
<td>BATHYMETRIC SURVEY</td><td>View Document</td></tr>
</tbody>
</table>
</body></html>""".replace("__BASE__", _BASE)

_DOC_412 = (
    "271500 UTC OCT 2026\r\n"
    "NAVAREA IX 412/26\r\n"
    "CENTRAL GULF\r\n"
    "QATAR\r\n"
    "CHARTS BA 2523, 2886, 3783, 3787 AND 3950\r\n"
    "\r\n"
    "1. TOWING OF 350 METERS FLOATING HOSE IN PROGRESS BY\r\n"
    "VESSEL AHTS ACCOMPLISH FROM 26-35.34N 052-02.17E\r\n"
    "TO 24-57.42N 051-35.59E (.) MARINERS CAUTIONED\r\n"
    "\r\n"
    "2. CANCEL THIS MSG 282300 UTC OCT 26 \r\n"
)

_DOC_410 = (
    "271500 UTC OCT 2026\r\n"
    "NAVAREA IX 410/26\r\n"
    "\r\n"
    "CANCEL NAVAREA IX 373/26,\r\n"
    "403/26, 404/26 AND THIS MSG\r\n"
)

_DOC_394 = (
    "211500 UTC OCT 2026\r\n"
    "NAVAREA IX 394/26\r\n"
    "SOUTHERN GULF\r\n"
    "UAE\r\n"
    "CHARTS BA 2888, 2889 AND 3739\r\n"
    "\r\n"
    "1. FOLLOWING DUBAI HARBOUR BUOYS REPORTED UNLIT\r\n"
    "(.) MARINERS CAUTIONED\r\n"
)

_FAKE_DOCS = {
    _BASE + "20261027SEC%20412.txt": _DOC_412,
    _BASE + "20261027SEC%20410.txt": _DOC_410,
    # 394/26 exists ONLY under the EC prefix (live behaviour).
    _BASE + "20261021EC%20394.txt": _DOC_394,
}

# Same table with the message rows removed (layout change regression).
_EMPTY_HTML = """<html><body>
<ul><li>Home</li></ul>
<table><thead><tr><th>S.NO</th><th>REGION/AREA</th><th>DTG</th>
<th>WARNING #</th><th>DESCRIPTION</th><th>ATTACHMENT</th></tr></thead>
<tbody></tbody></table>
</body></html>"""


def _fake_fetch(href):
    return _FAKE_DOCS.get(href)


class TestExtractRows(unittest.TestCase):
    def test_extracts_every_numbered_row_with_and_without_links(self):
        rows = extract_rows(_MINI_HTML)
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("412", "26"), ("410", "26"),
                          ("394", "26"), ("378", "26")])

    def test_only_linked_row_carries_an_href(self):
        rows = extract_rows(_MINI_HTML)
        by_num = {r[0]: r for r in rows}
        self.assertTrue(by_num["412"][2].startswith(_BASE), rows)
        self.assertIsNone(by_num["410"][2])
        self.assertIsNone(by_num["394"][2])
        self.assertIsNone(by_num["378"][2])

    def test_dtg_is_captured_for_candidate_resolution(self):
        rows = extract_rows(_MINI_HTML)
        by_num = {r[0]: r for r in rows}
        self.assertEqual(by_num["394"][3], "211500 UTC OCT 2026")
        self.assertEqual(by_num["378"][3], "NORTHERN GULF KUWAIT")

    def test_empty_table_yields_no_rows(self):
        self.assertEqual(extract_rows(_EMPTY_HTML), [])

    def test_candidate_urls_follow_dtg_day_and_both_prefixes(self):
        urls = _candidate_doc_urls("394", "211500 UTC OCT 2026")
        self.assertEqual(urls, [_BASE + "20261021SEC%20394.txt",
                                _BASE + "20261021EC%20394.txt"])
        self.assertEqual(_candidate_doc_urls("378", "NORTHERN GULF KUWAIT"),
                         [], "malformed DTG must yield no candidates")

    def test_malformed_dtg_counts_as_recent(self):
        # Fail-safe: a row whose DTG cannot be parsed is always fetched.
        self.assertTrue(_dtg_is_recent("NORTHERN GULF KUWAIT"))
        self.assertFalse(_dtg_is_recent("211500 UTC OCT 2026",
                                         today=date(2026, 12, 31)))


class TestAssembleBulletin(unittest.TestCase):
    def test_concatenates_documents_in_table_order(self):
        rows = extract_rows(_MINI_HTML)
        bulletin = assemble_bulletin(rows, _fake_fetch)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 412/26", bulletin)
        self.assertIn("NAVAREA IX 410/26", bulletin)
        self.assertIn("NAVAREA IX 394/26", bulletin)
        self.assertLess(bulletin.index("412/26"), bulletin.index("410/26"))
        self.assertLess(bulletin.index("410/26"), bulletin.index("394/26"))

    def test_linkless_row_resolved_via_dtg_candidate(self):
        rows = [r for r in extract_rows(_MINI_HTML) if r[0] == "394"]
        bulletin = assemble_bulletin(rows, _fake_fetch)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 394/26", bulletin)

    def test_row_without_link_and_malformed_dtg_is_skipped_loudly(self):
        rows = extract_rows(_MINI_HTML)
        bulletin = assemble_bulletin(rows, _fake_fetch)
        self.assertIsNotNone(bulletin)
        self.assertNotIn("NAVAREA IX 378/26", bulletin)

    def test_crlf_normalized_and_lines_trimmed(self):
        rows = extract_rows(_MINI_HTML)
        bulletin = assemble_bulletin(rows, _fake_fetch)
        self.assertNotIn("\r", bulletin)

    def test_zero_resolved_documents_refuses_snapshot(self):
        # Every document unreachable: the 3rd consecutive unreachable
        # row trips the circuit breaker (identical fail-closed refusal
        # as the zero-resolved check would give).
        rows = extract_rows(_MINI_HTML)
        self.assertIsNone(assemble_bulletin(rows, lambda href: None))

    def test_systemic_block_trips_the_circuit_breaker(self):
        # Three unreachable rows IN A ROW = wholesale client block
        # (rate limit / WAF) — abort loudly instead of grinding retry
        # sleeps per remaining document (that grind wedged CI for 20
        # minutes, live 2026-10-28).
        rows = extract_rows(_MINI_HTML)
        self.assertIsNone(assemble_bulletin(rows, lambda href: None))

    def test_intermittent_unreachable_does_not_trip_the_breaker(self):
        # fail -> success -> fail: the counter resets on every success;
        # isolated dead documents still skip loudly and the harvest
        # survives.
        def _flaky(href):
            if href.endswith("412.txt") or href.endswith("394.txt"):
                return None
            return _fake_fetch(href)
        rows = extract_rows(_MINI_HTML)
        bulletin = assemble_bulletin(rows, _flaky)
        self.assertIsNotNone(bulletin)
        self.assertNotIn("NAVAREA IX 412/26", bulletin)
        self.assertIn("NAVAREA IX 410/26", bulletin)
        self.assertNotIn("NAVAREA IX 394/26", bulletin)

    def test_unreachable_advertised_doc_is_skipped_loudly(self):
        # The site advertises links its own WAF can refuse (persistent
        # HTTP 429 on 20250903SEC 405.txt for the CI runner, live
        # 2026-10-28): one dead document must NOT abort the whole sync —
        # it skips loudly and the rest of the harvest survives.
        rows = extract_rows(_MINI_HTML)
        bulletin = assemble_bulletin(
            rows, lambda href: None if href.endswith("412.txt")
            else _fake_fetch(href))
        self.assertIsNotNone(bulletin)
        self.assertNotIn("NAVAREA IX 412/26", bulletin)
        self.assertIn("NAVAREA IX 410/26", bulletin)
        self.assertIn("NAVAREA IX 394/26", bulletin)

    def test_fail_closed_when_advertised_document_is_the_wrong_warning(self):
        rows = extract_rows(_MINI_HTML)
        self.assertIsNone(assemble_bulletin(rows, lambda href: _DOC_410))

    def test_fail_closed_when_no_document_resolves_at_all(self):
        rows = [r for r in extract_rows(_MINI_HTML) if r[0] == "378"]
        self.assertIsNone(assemble_bulletin(rows, _fake_fetch))


class _FakeResponse:
    """Minimal requests-like response object for fetch_url tests."""
    def __init__(self, status_code, text, headers):
        self.status_code = status_code
        self.text = text
        self.headers = headers


class TestFetchUrlTransientRetry(unittest.TestCase):
    """Transient HTTP 503 answers are retried with backoff. HTTP 429 is
    NOT retried anymore (Alfred, 2026-10-28): the FIRST 429 raises
    _HttpRateLimited and stops the run cleanly — no retry ladder."""

    _URL = "https://example.invalid/doc.txt"

    def _fetch(self, side_effect):
        # tests.yml runs unittest discovery on a BARE python (no pip
        # install — live failure 2026-10-28: ModuleNotFoundError
        # 'requests' when patching the requests module by string). Patch
        # the module attribute the scraper actually calls, faking the
        # module when absent, so the requests-fallback retry path stays
        # testable in every CI environment.
        requests_mod = (navarea_ix_scraper.requests
                        if getattr(navarea_ix_scraper, "requests", None)
                        is not None
                        else types.ModuleType("requests"))
        with (mock.patch.object(navarea_ix_scraper, "HAS_SCRAPLING", False),
              mock.patch.object(navarea_ix_scraper, "HAS_REQUESTS", True),
              mock.patch.object(navarea_ix_scraper, "requests", requests_mod,
                                create=True),
              mock.patch.object(requests_mod, "get", side_effect=side_effect,
                                create=True) as get_mock,
              mock.patch("scripts.navarea_ix_scraper.time.sleep") as sleep_mock):
            body = fetch_url(self._URL, "text/plain")
        return body, get_mock, sleep_mock

    def test_transient_503_is_retried_until_success(self):
        body, get_mock, sleep_mock = self._fetch([
            _FakeResponse(503, "unavailable", {"Retry-After": "7"}),
            _FakeResponse(200, "NAVAREA IX 412/26", {}),
        ])
        self.assertEqual(body, "NAVAREA IX 412/26")
        self.assertEqual(get_mock.call_count, 2)
        sleep_mock.assert_called_once_with(7.0)

    def test_persistent_503_fails_closed_after_retries(self):
        body, get_mock, sleep_mock = self._fetch(
            [_FakeResponse(503, "unavailable", {})] * 10)
        self.assertIsNone(body)
        self.assertEqual(get_mock.call_count,
                         navarea_ix_scraper._TRANSIENT_RETRIES + 1)
        self.assertEqual(sleep_mock.call_count,
                         navarea_ix_scraper._TRANSIENT_RETRIES)

    def test_first_429_raises_without_retry_ladder(self):
        requests_mod = (navarea_ix_scraper.requests
                         if getattr(navarea_ix_scraper, "requests", None)
                         is not None
                         else types.ModuleType("requests"))
        with (mock.patch.object(navarea_ix_scraper, "HAS_SCRAPLING", False),
              mock.patch.object(navarea_ix_scraper, "HAS_REQUESTS", True),
              mock.patch.object(navarea_ix_scraper, "requests", requests_mod,
                                create=True),
              mock.patch.object(requests_mod, "get", side_effect=[
                  _FakeResponse(429, "slow down", {}),
                  _FakeResponse(200, "never reached", {}),
              ], create=True) as get_mock,
              mock.patch("scripts.navarea_ix_scraper.time.sleep") as sleep_mock):
            with self.assertRaises(navarea_ix_scraper._HttpRateLimited):
                fetch_url(self._URL, "text/plain", {})
        self.assertEqual(get_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_non_transient_status_is_not_retried(self):
        body, get_mock, sleep_mock = self._fetch(
            [_FakeResponse(404, "not found", {})] * 10)
        self.assertIsNone(body)
        self.assertEqual(get_mock.call_count, 1)
        sleep_mock.assert_not_called()


class TestUniversalParserIngestion(unittest.TestCase):
    def setUp(self):
        rows = extract_rows(_MINI_HTML)
        self.parsed = parse_bulletin_text(
            assemble_bulletin(rows, _fake_fetch), "ix")

    def test_exactly_three_warnings(self):
        self.assertEqual(len(self.parsed["nav_warnings"]), 3)

    def test_warning_ids_carry_the_block_numbers(self):
        ids = [w["warning_id"] for w in self.parsed["nav_warnings"]]
        self.assertTrue(any("412" in i for i in ids), ids)
        self.assertTrue(any("410" in i for i in ids), ids)
        self.assertTrue(any("394" in i for i in ids), ids)

    def test_cancel_crossref_is_not_a_phantom_warning(self):
        ids = [w["warning_id"] for w in self.parsed["nav_warnings"]]
        self.assertFalse(any("373" in i for i in ids), ids)

    def test_towing_operation_yields_geometry(self):
        with_geom = [w for w in self.parsed["nav_warnings"] if w.get("spatial")]
        self.assertTrue(with_geom, "expected at least one warning with geometry")


class _FakeStoredStateClient:
    """Minimal supabase-like client serving one canned nav_warnings
    result for the stored-state lookup (source_id-filtered select)."""

    def __init__(self, canonical_ids):
        self._rows = [{"canonical_id": cid} for cid in canonical_ids]
        self.queries = []

    def table(self, name):
        self.queries.append(name)
        return self

    def select(self, *_args, **_kw):
        return self

    def eq(self, column, value):
        self.queries.append((column, value))
        return self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FailingStoredStateClient:
    def table(self, name):
        raise RuntimeError("waf block")


class TestSkipStoredRows(unittest.TestCase):
    """Steady-state harvests must not re-fetch documents already in the
    DB (live 2026-10-28: the site's WAF rate-limited full harvests with
    persistent 429s). Only NEW rows, RECENT rows (recheck window) and
    rows whose DTG cannot be parsed are fetched."""

    _FAR_FUTURE = date(2026, 12, 31)   # fixture DTGs (Oct 2026) are old
    _TODAY = date(2026, 10, 28)        # fixture DTG 27 Oct is recent

    def test_stored_keys_are_parsed_from_canonical_ids(self):
        client = _FakeStoredStateClient([
            "NAVAREA IX 412/26", "NAVAREA IX 376/23",
            "garbage", "NAVAREA II 001/26", "NAVAREA IX 405/2025",
        ])
        self.assertEqual(_stored_ix_keys(client),
                         {(412, "26"), (376, "23")})
        self.assertIn(("source_id", "manual-navarea-ix"),
                      client.queries)

    def test_no_client_means_unknown_state(self):
        self.assertIsNone(_stored_ix_keys(None))

    def test_query_failure_fails_open(self):
        self.assertIsNone(_stored_ix_keys(_FailingStoredStateClient()))

    def test_stored_rows_are_not_fetched(self):
        rows = extract_rows(_MINI_HTML)
        stored = {(412, "26"), (410, "26")}
        to_fetch, skipped = _filter_stored_rows(
            rows, stored, today=self._FAR_FUTURE)
        self.assertEqual([r[0] for r in to_fetch], ["394", "378"])
        self.assertEqual(skipped, 2)

    def test_new_and_previously_unstored_rows_are_fetched(self):
        rows = extract_rows(_MINI_HTML)
        to_fetch, skipped = _filter_stored_rows(
            rows, {(394, "26")}, today=self._FAR_FUTURE)
        self.assertEqual([r[0] for r in to_fetch], ["412", "410", "378"])
        self.assertEqual(skipped, 1)

    def test_recent_stored_rows_are_rechecked(self):
        rows = [r for r in extract_rows(_MINI_HTML) if r[0] == "394"]
        # 394/26 DTG 211500 UTC OCT 2026: 7 days before _TODAY -> recent.
        to_fetch, _ = _filter_stored_rows(
            rows, {(394, "26")}, today=self._TODAY)
        self.assertEqual([r[0] for r in to_fetch], ["394"])
        to_fetch, skipped = _filter_stored_rows(
            rows, {(394, "26")}, today=self._FAR_FUTURE)
        self.assertEqual(to_fetch, [])
        self.assertEqual(skipped, 1)

    def test_unparseable_dtg_is_always_fetched(self):
        rows = [r for r in extract_rows(_MINI_HTML) if r[0] == "378"]
        to_fetch, _ = _filter_stored_rows(
            rows, {(378, "26")}, today=self._FAR_FUTURE)
        self.assertEqual([r[0] for r in to_fetch], ["378"])

    def test_unknown_state_fetches_everything(self):
        rows = extract_rows(_MINI_HTML)
        to_fetch, skipped = _filter_stored_rows(rows, None)
        self.assertEqual(len(to_fetch), len(rows))
        self.assertEqual(skipped, 0)


class TestKnownIdsFile(unittest.TestCase):
    """IX_KNOWN_IDS_FILE supplies the stored set without database access,
    so a credential-less run (e.g. writing to HELM_SYNC_SINK) still skips
    stored documents instead of re-fetching the same first rows into the
    WAF's request cap every run."""

    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                        encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_file_lines_parse_like_stored_canonical_ids(self):
        path = self._write("# known ids\nNAVAREA IX 412/26\n\n"
                           "  NAVAREA IX 376/23  \ngarbage\nNAVAREA II 001/26\n")
        self.assertEqual(_known_ix_keys_from_file(path),
                         {(412, "26"), (376, "23")})

    def test_empty_file_is_known_and_empty(self):
        self.assertEqual(_known_ix_keys_from_file(self._write("")), set())

    def test_unreadable_file_fails_open(self):
        self.assertIsNone(_known_ix_keys_from_file(
            os.path.join(tempfile.gettempdir(), "no-such-ix-known-ids.txt")))

    def test_file_skips_stored_rows_like_the_database(self):
        path = self._write("NAVAREA IX 412/26\nNAVAREA IX 410/26\n")
        rows = extract_rows(_MINI_HTML)
        to_fetch, skipped = _filter_stored_rows(
            rows, _known_ix_keys_from_file(path),
            today=TestSkipStoredRows._FAR_FUTURE)
        self.assertEqual([r[0] for r in to_fetch], ["394", "378"])
        self.assertEqual(skipped, 2)

    def test_file_takes_precedence_over_the_database(self):
        path = self._write("NAVAREA IX 412/26\n")
        env = {"IX_KNOWN_IDS_FILE": path, "IX_FULL_SNAPSHOT": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(navarea_ix_scraper, "_supabase_client_or_none",
                               side_effect=AssertionError("database queried")):
            self.assertEqual(_resolve_stored_keys(), {(412, "26")})

    def test_full_snapshot_overrides_the_file(self):
        path = self._write("NAVAREA IX 412/26\n")
        env = {"IX_KNOWN_IDS_FILE": path, "IX_FULL_SNAPSHOT": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(_resolve_stored_keys())

    def test_without_the_file_the_database_lookup_is_used(self):
        client = _FakeStoredStateClient(["NAVAREA IX 394/26"])
        with mock.patch.dict(os.environ, {"IX_KNOWN_IDS_FILE": "", "IX_FULL_SNAPSHOT": ""}), \
             mock.patch.object(navarea_ix_scraper, "_supabase_client_or_none",
                               return_value=client):
            self.assertEqual(_resolve_stored_keys(), {(394, "26")})


class TestDocPause(unittest.TestCase):
    """Fixed random 3-5 s politeness pause between document fetches
    (one constant pair, env-overridable; the flat 0.6 s burst was
    rate-limited by the site's WAF — live 2026-10-28)."""

    def test_pause_is_uniform_three_to_five_seconds(self):
        for _ in range(10):
            pause = navarea_ix_scraper._doc_pause_seconds()
            self.assertGreaterEqual(pause, 3.0)
            self.assertLessEqual(pause, 5.0)

    def test_pause_bounds_are_order_safe(self):
        with mock.patch.object(navarea_ix_scraper, "_DOC_PAUSE_MIN_S", 5.0), \
             mock.patch.object(navarea_ix_scraper, "_DOC_PAUSE_MAX_S", 3.0), \
             mock.patch("scripts.navarea_ix_scraper.random.uniform") as uniform_mock:
            navarea_ix_scraper._doc_pause_seconds()
        uniform_mock.assert_called_with(3.0, 5.0)

    def test_pause_sleeps_between_fetches(self):
        with mock.patch("scripts.navarea_ix_scraper.random.uniform",
                        return_value=4.25) as uniform_mock, \
             mock.patch("scripts.navarea_ix_scraper.time.sleep") as sleep_mock:
            navarea_ix_scraper._pause_before_doc_fetch()
        uniform_mock.assert_called_with(3.0, 5.0)
        sleep_mock.assert_called_once_with(4.25)


class TestLegacyProsignHeader(unittest.TestCase):
    """Live CI failure 2026-10-28: the 2023-era paknavy files open with
    the legacy WWNWS prosign header 'NAVAREA IX (.) 376/23 (.)' — the
    '(.)' separator once failed the scraper's header validation and
    aborted the whole harvest. The form must validate AND parse like
    any other warning; wrong numbers/years stay refused."""

    # Verbatim shape of the live file 20231009SEC 376.txt.
    _DOC_376 = (
        "091500 UTC OCT 2023\r\n"
        "NAVAREA IX (.) 376/23 (.) IRAN (.) TANGEH-YE HORMOZ (.)\r\n"
        "CHARTS BA 2888, 2851 AND 3173\r\n"
        "\r\n"
        "2. FOLLOWING CHANGES IN AIDS TO NAVIGATION OF HORMOZGAN\r\n"
        "COASTAL WATERS\r\n"
        "\r\n"
        "A. STARBOARD HAND BEACON TOWER FL 5 SEC 5 M 5 METERS\r\n"
        "ESTABLISHED IN POSITION 25-40.99N 057-51.93E\r\n"
    )

    def _rows(self, number, year):
        return [(number, year, _BASE + "20231009SEC%20376.txt",
                 "091500 UTC OCT 2023")]

    def test_prosign_form_is_accepted(self):
        bulletin = assemble_bulletin(self._rows("376", "23"),
                                     lambda href: self._DOC_376)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX (.) 376/23", bulletin)

    def test_prosign_header_with_wrong_number_is_refused(self):
        self.assertIsNone(assemble_bulletin(self._rows("377", "23"),
                                             lambda href: self._DOC_376))

    def test_prosign_header_with_wrong_year_is_refused(self):
        self.assertIsNone(assemble_bulletin(self._rows("376", "26"),
                                             lambda href: self._DOC_376))

    def test_prosign_block_parses_via_universal_parser(self):
        bulletin = assemble_bulletin(self._rows("376", "23"),
                                     lambda href: self._DOC_376)
        parsed = parse_bulletin_text(bulletin, "ix")
        ids = [w["warning_id"] for w in parsed["nav_warnings"]]
        self.assertEqual(ids, ["NAVAREA IX 376/23"])


class TestRequestCapAndRateLimitStop(unittest.TestCase):
    """Alfred's revised IX strategy (2026-10-28): a run stops CLEANLY
    (not an error, exit 0, stored rows kept) at MAX_REQUESTS_PER_RUN
    requests or on the FIRST HTTP 429 — no 20/40/60s retry ladder. A
    deliberate stop is never reported as 'the site is blocking us',
    but a 429 before anything is stored stays a loud refusal."""

    @staticmethod
    def _stats(requests=0):
        return {"requests": requests, "rows_stored": 0,
                "rows_skipped": 0, "rows_remaining": 0,
                "http_429": 0, "stop_reason": None}

    def test_run_stops_cleanly_at_the_cap(self):
        rows = extract_rows(_MINI_HTML)
        cap = navarea_ix_scraper.MAX_REQUESTS_PER_RUN
        stats = self._stats(cap - 1)
        def _fetch(href):
            stats["requests"] += 1
            return _FAKE_DOCS.get(href)
        bulletin = assemble_bulletin(rows, _fetch, stats)
        # 412/26 fetched (the 60th request), then the cap stops the run
        # before 410/26: stored rows kept, clean stop, not an error.
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 412/26", bulletin)
        self.assertNotIn("NAVAREA IX 410/26", bulletin)
        self.assertEqual(stats["stop_reason"], "request_cap")
        self.assertEqual(stats["rows_stored"], 1)
        self.assertEqual(stats["rows_remaining"], 3)

    def test_retries_count_toward_the_cap(self):
        # Every retry counts toward the cap: two 503s then a 200 = 3.
        stats = {}
        requests_mod = (navarea_ix_scraper.requests
                        if getattr(navarea_ix_scraper, "requests", None)
                        is not None else types.ModuleType("requests"))
        with (mock.patch.object(navarea_ix_scraper, "HAS_SCRAPLING", False),
              mock.patch.object(navarea_ix_scraper, "HAS_REQUESTS", True),
              mock.patch.object(navarea_ix_scraper, "requests", requests_mod,
                                create=True),
              mock.patch.object(requests_mod, "get", side_effect=[
                  _FakeResponse(503, "unavailable", {}),
                  _FakeResponse(503, "unavailable", {}),
                  _FakeResponse(200, "NAVAREA IX 412/26", {}),
              ], create=True),
              mock.patch("scripts.navarea_ix_scraper.time.sleep")):
            body = fetch_url("https://example.invalid/doc.txt",
                             "text/plain", stats)
        self.assertEqual(body, "NAVAREA IX 412/26")
        self.assertEqual(stats["requests"], 3)

    def test_cap_stop_does_not_trip_the_circuit_breaker(self):
        # An unreachable row then a clean cap stop: the deliberate stop
        # must not be reported as a wholesale block (breaker untouched).
        rows = extract_rows(_MINI_HTML)
        cap = navarea_ix_scraper.MAX_REQUESTS_PER_RUN
        stats = self._stats(cap - 2)
        def _fetch(href):
            stats["requests"] += 1
            if href.endswith("412.txt"):
                return None  # unreachable row (breaker counter = 1)
            return _FAKE_DOCS.get(href)
        bulletin = assemble_bulletin(rows, _fetch, stats)
        self.assertIsNotNone(bulletin)  # 410/26 stored before the cap
        self.assertIn("NAVAREA IX 410/26", bulletin)
        self.assertNotIn("NAVAREA IX 412/26", bulletin)
        self.assertEqual(stats["stop_reason"], "request_cap")

    def test_single_429_ends_the_run_cleanly_with_stored_rows_kept(self):
        rows = extract_rows(_MINI_HTML)
        stats = self._stats()
        def _fetch(href):
            stats["requests"] += 1
            if href.endswith("412.txt"):
                return _FAKE_DOCS.get(href)  # first row stored
            raise navarea_ix_scraper._HttpRateLimited("HTTP 429")
        bulletin = assemble_bulletin(rows, _fetch, stats)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 412/26", bulletin)
        self.assertNotIn("NAVAREA IX 410/26", bulletin)
        self.assertEqual(stats["http_429"], 1)
        self.assertEqual(stats["stop_reason"], "http_429")
        self.assertEqual(stats["rows_remaining"], 3)

    def test_429_on_the_first_requests_is_a_loud_refusal(self):
        rows = extract_rows(_MINI_HTML)
        stats = self._stats()
        def _fetch(href):
            stats["requests"] += 1
            raise navarea_ix_scraper._HttpRateLimited("HTTP 429")
        self.assertIsNone(assemble_bulletin(rows, _fetch, stats))
        self.assertEqual(stats["stop_reason"], "http_429")


class TestPartialHarvestSalvage(unittest.TestCase):
    """Alfred (2026-10-30, live full-sweep dispatch run #83): when the
    scrape stops partway — wholesale block (circuit breaker), wrong-
    header advertised link — every row already scraped must still reach
    the parser/sync instead of being thrown away. Only a stop with ZERO
    rows harvested stays a loud refusal."""

    @staticmethod
    def _stats():
        return {"requests": 0, "rows_stored": 0, "rows_skipped": 0,
                "rows_remaining": 0, "http_429": 0, "stop_reason": None}

    def test_circuit_breaker_keeps_rows_already_harvested(self):
        rows = [("412", "26", _BASE + "20261027SEC%20412.txt",
                 "271500 UTC OCT 2026"),
                ("410", "26", None, "271500 UTC OCT 2026"),
                ("394", "26", None, "211500 UTC OCT 2026"),
                ("411", "26", None, "271500 UTC OCT 2026")]
        def _fetch(href):
            if href.endswith("412.txt"):
                return _DOC_412
            return None  # 410, 394, 411 unreachable in a row -> breaker
        stats = self._stats()
        bulletin = assemble_bulletin(rows, _fetch, stats)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 412/26", bulletin)
        self.assertNotIn("NAVAREA IX 410/26", bulletin)
        self.assertEqual(stats["stop_reason"], "circuit_breaker")
        self.assertEqual(stats["rows_stored"], 1)
        self.assertEqual(stats["rows_remaining"], 1)
        # The salvaged harvest must still parse like any bulletin.
        parsed = parse_bulletin_text(bulletin, "ix")
        self.assertEqual([w["warning_id"] for w in parsed["nav_warnings"]],
                         ["NAVAREA IX 412/26"])

    def test_wrong_header_stops_and_keeps_rows_already_harvested(self):
        rows = [("412", "26", _BASE + "20261027SEC%20412.txt",
                 "271500 UTC OCT 2026"),
                ("999", "26", _BASE + "20261027SEC%20999.txt",
                 "271500 UTC OCT 2026")]
        def _fetch(href):
            if href.endswith("412.txt"):
                return _DOC_412
            return _DOC_410  # advertised link carrying the WRONG header
        stats = self._stats()
        bulletin = assemble_bulletin(rows, _fetch, stats)
        self.assertIsNotNone(bulletin)
        self.assertIn("NAVAREA IX 412/26", bulletin)
        self.assertNotIn("NAVAREA IX 999/26", bulletin)
        self.assertEqual(stats["stop_reason"], "wrong_header")
        self.assertEqual(stats["rows_stored"], 1)
        self.assertEqual(stats["rows_remaining"], 1)

    def test_zero_harvested_circuit_breaker_still_refuses(self):
        rows = [("410", "26", None, "271500 UTC OCT 2026"),
                ("394", "26", None, "211500 UTC OCT 2026"),
                ("411", "26", None, "271500 UTC OCT 2026")]
        stats = self._stats()
        self.assertIsNone(assemble_bulletin(rows, lambda href: None, stats))
        self.assertEqual(stats["stop_reason"], "circuit_breaker")


class TestArchiveOldWarnings(unittest.TestCase):
    """Pre-2025 warnings are stored as archive (status 'expired'),
    never published on the active-only portal (Alfred 2026-10-28)."""

    def test_pre_2025_warnings_are_archived_others_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "parsed.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"nav_warnings": [
                    {"warning_id": "NAVAREA IX 412/26"},
                    {"warning_id": "NAVAREA IX 347/25"},
                    {"warning_id": "NAVAREA IX 376/23"},
                    {"warning_id": "NAVAREA IX 100/24"},
                    {"warning_id": "not a warning id"},
                ], "memo": {"region": "ix"}}, f)
            archived = navarea_ix_scraper._archive_old_warnings(path)
            self.assertEqual(archived, 2)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            by_id = {r["warning_id"]: r for r in data["nav_warnings"]}
            self.assertEqual(by_id["NAVAREA IX 376/23"]["status"], "expired")
            self.assertEqual(by_id["NAVAREA IX 100/24"]["status"], "expired")
            self.assertNotIn("status", by_id["NAVAREA IX 412/26"])
            self.assertNotIn("status", by_id["NAVAREA IX 347/25"])
            self.assertNotIn("status", by_id["not a warning id"])
            self.assertEqual(data["memo"], {"region": "ix"})


if __name__ == "__main__":
    unittest.main()

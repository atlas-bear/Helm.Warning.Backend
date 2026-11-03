import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import navarea_xi_scraper as xi


def _list_xml(*tanas):
    members = "".join(
        f"<Member><categoly>Depth</categoly><number>{t[2:]}</number><tana>{t}</tana>"
        f"<title>TITLE {t}</title><visual>0</visual></Member>" for t in tanas)
    return f"<opt>{members}</opt>\n\n"


# Shape of disp_warnings.cgi's answer, from the coordinator on 2026-11-03.
TEXTS = {
    "120262": ("NO.12-0262       Date:2012/04/29 09 UTC",
               "NORTH PACIFIC, NANPO SYOTO.<br>DISCOLOURED WATER OBSERVED IN VICINITY <br>"
               "OF 24-49.3N 141-20.4E AT 290640Z APR.<br>VESSELS REQUESTED TO BE CAUTION ADVISED."),
    "160024": ("NO.16-0024       Date:2016/01/15 13 UTC",
               "SOUTH CHINA SEA, SOUTHERN PART.<br>PLATFORM REPORTED TO EXISTS IN 05-32.2N<br>114-22.2E."),
    "260496": ("NO.26-0496       Date:2026/11/03 12 UTC",
               "SULU SEA.<br>SPACE DEBRIS.<br>081800Z TO 082400Z NOV. AREAS BOUNDED BY<br>"
               "A. 09-16.8N 118-59.4E<br>CANCEL 0494/26.<br>CANCEL THIS MSG 090100Z NOV."),
}


def _disp_html(tanas):
    blocks = "".join(f"<STRONG>{TEXTS[t][0]}</STRONG><br>\n<br>{TEXTS[t][1]}<br><br><hr><br>\n" for t in tanas)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<html><body><H1>NAVAREA XI</H1>\n'
            + blocks + "</body></html>")


class FakeCoordinator:
    """warnings.cgi and disp_warnings.cgi, answering from a {year: [tana]} table."""

    def __init__(self, by_year, list_status=None, list_body=None, drop=()):
        self.by_year = by_year
        self.list_status = list_status or {}
        self.list_body = list_body or {}
        self.drop = set(drop)
        self.years_asked = []
        self.disp_requests = []

    async def post(self, url, data):
        if url == xi.URL_LIST:
            year = int(data["YEAR"])
            self.years_asked.append(year)
            if year in self.list_status:
                return self.list_status[year], ""
            if year in self.list_body:
                return 200, self.list_body[year]
            return 200, _list_xml(*self.by_year.get(year, []))
        if url == xi.URL_DISP:
            tanas = [t for t in data["TANA"].split(":") if t]
            self.disp_requests.append(tanas)
            return 200, _disp_html([t for t in tanas if t not in self.drop])
        return 404, ""


def harvest(coord, first=2010, last=2026):
    return asyncio.run(xi._harvest_in_force(coord.post, first, last, pause=None))


class TestInForceHarvest(unittest.TestCase):

    def test_warnings_from_earlier_years_are_harvested(self):
        coord = FakeCoordinator({2012: ["120262"], 2016: ["160024"], 2026: ["260496"]})
        got = harvest(coord)
        self.assertEqual(set(got), {"NAVAREA XI NO.12-0262", "NAVAREA XI NO.16-0024", "NAVAREA XI NO.26-0496"})
        self.assertIn("2012/04/29 09 UTC", got["NAVAREA XI NO.12-0262"]["date_line"])
        self.assertIn("PLATFORM REPORTED", got["NAVAREA XI NO.16-0024"]["body"])

    def test_every_year_in_range_is_asked(self):
        coord = FakeCoordinator({2026: ["260496"]})
        harvest(coord, first=2020, last=2026)
        self.assertEqual(sorted(coord.years_asked), list(range(2020, 2027)))

    def test_new_year_keeps_last_years_warnings(self):
        # 1 January: nothing issued yet this year, last year's warnings still in force.
        coord = FakeCoordinator({2026: ["260496"]})
        got = harvest(coord, first=2020, last=2027)
        self.assertEqual(set(got), {"NAVAREA XI NO.26-0496"})

    def test_nothing_in_force_is_an_empty_harvest_not_a_failure(self):
        self.assertEqual(harvest(FakeCoordinator({})), {})

    def test_a_failed_year_fails_the_harvest(self):
        coord = FakeCoordinator({2016: ["160024"], 2026: ["260496"]}, list_status={2016: 503})
        self.assertIsNone(harvest(coord))

    def test_a_malformed_year_fails_the_harvest(self):
        coord = FakeCoordinator({2026: ["260496"]}, list_body={2019: "<html>Service Unavailable</html>"})
        self.assertIsNone(harvest(coord))
        coord = FakeCoordinator({2026: ["260496"]}, list_body={2019: "not xml at all"})
        self.assertIsNone(harvest(coord))

    def test_an_id_from_another_year_fails_the_harvest(self):
        coord = FakeCoordinator({2026: ["260496"]}, list_body={2016: _list_xml("260496")})
        self.assertIsNone(harvest(coord))

    def test_a_listed_warning_missing_from_the_texts_fails_the_harvest(self):
        coord = FakeCoordinator({2012: ["120262"], 2026: ["260496"]}, drop={"120262"})
        self.assertIsNone(harvest(coord))

    def test_texts_are_requested_in_chunks(self):
        tanas = [f"26{n:04d}" for n in range(1, 2 * xi.DISP_CHUNK + 6)]
        for t in tanas:
            TEXTS.setdefault(t, (f"NO.26-{t[2:]}       Date:2026/11/01 00 UTC", "EAST CHINA SEA.<br>TEST."))
        coord = FakeCoordinator({2026: tanas})
        got = harvest(coord)
        self.assertEqual(len(got), len(tanas))
        self.assertEqual(len(coord.disp_requests), 3)
        self.assertTrue(all(len(r) <= xi.DISP_CHUNK for r in coord.disp_requests))


class TestParseMemberIds(unittest.TestCase):

    def test_empty_year(self):
        self.assertEqual(xi._parse_member_ids("<opt></opt>\n\n", 2017), [])

    def test_ids_in_order(self):
        self.assertEqual(xi._parse_member_ids(_list_xml("250437", "250412"), 2025), ["250437", "250412"])

    def test_wrong_root_raises(self):
        with self.assertRaises(ValueError):
            xi._parse_member_ids("<html></html>", 2025)


class TestScrapeEntryPoint(unittest.TestCase):

    def test_both_paths_failing_aborts_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "xi.txt")
            with mock.patch.object(xi, "HAS_PLAYWRIGHT", False), \
                 mock.patch.object(xi, "fetch_direct_disp", return_value=None):
                self.assertIsNone(xi.scrape_xi_warnings(out))
            self.assertFalse(os.path.exists(out))

    def test_incomplete_browser_harvest_falls_back_to_direct(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "xi.txt")
            direct = {"NAVAREA XI NO.26-0496": {"header": "NAVAREA XI NO.26-0496",
                                                "date_line": TEXTS["260496"][0], "body": "SULU SEA."}}

            async def browser_incomplete(**kw):
                return None

            with mock.patch.object(xi, "HAS_PLAYWRIGHT", True), \
                 mock.patch.object(xi, "fetch_via_browser", browser_incomplete), \
                 mock.patch.object(xi, "fetch_direct_disp", return_value=direct) as d_mock:
                self.assertEqual(xi.scrape_xi_warnings(out), direct)
                d_mock.assert_called_once()
            with open(out, encoding="utf-8") as f:
                self.assertIn("--- NAVAREA XI NO.26-0496 ---", f.read())

    def test_empty_harvest_writes_an_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "sub", "xi.txt")
            with mock.patch.object(xi, "HAS_PLAYWRIGHT", False), \
                 mock.patch.object(xi, "fetch_direct_disp", return_value={}):
                self.assertEqual(xi.scrape_xi_warnings(out), {})
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "")


if __name__ == "__main__":
    unittest.main()

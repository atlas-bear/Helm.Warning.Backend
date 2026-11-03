#!/usr/bin/env python3
"""shared/dtg.py — Date-time group (issue time) of a warning.

Every coordinator stamps a warning with the time it was issued, but most
parsers never read it: the universal pipeline and NAVAREA VIII emit
issued_text=None, and NAVAREA XI's "Date:2026/11/02 12 UTC" is not a
format parse_safe_iso_timestamp accepts. Without it a warning has no
publication_time, so build_master_payload has no valid_from to fall
back to.

Formats seen in the live feeds (2026-11-03):

  IX    "031500 UTC MAR 2026", "161500 UTC OCT 24", "061500 FEB 26"
        (last line of the warning; sometimes preceded by a BOM)
  VIII  "031006Z/JUN 26"                     (last line)
  III   "25 February 2022 00:00 UTC"         (second line)
  XI    "2026/11/02 12 UTC"                  (the "Date:" field)

Fail-closed, like _extract_validity_dates: an unknown month, an
impossible date or a missing year with no default yields None, never a
guess. find_dtg_line only accepts a line that is NOTHING BUT a
date-time group, so a time inside a sentence ("FROM 270300 UTC OCT 26
TO ...", "CANCEL THIS MSG 060200Z NOV") is never read as the issue time.
"""

import re
from datetime import datetime
from typing import Optional

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# DDHHMM[Z|UTC][/]MON[.] [YY|YYYY]  (IX, VIII, I, Baltic)
RE_DTG_NUMERIC = re.compile(
    r"(\d{2})(\d{2})(\d{2})\s*(?:Z|UTC)?\s*/?\s*([A-Za-z]{3,9})\.?(?:\s+(\d{2}|\d{4}))?",
    re.IGNORECASE)
# DD Month YYYY HH:MM UTC  (III). The time and zone are required, so a bare
# date standing alone in a warning body ("14 NOV 2026") is not taken for one.
RE_DTG_LONG = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\s+(\d{2}):?(\d{2})\s*(?:Z|UTC)",
    re.IGNORECASE)
# YYYY/MM/DD [HH[[:]MM]] [UTC]  (XI)
RE_DTG_ISOISH = re.compile(
    r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})(?:\s+(\d{2}):?(\d{2})?)?(?:\s*(?:Z|UTC))?",
    re.IGNORECASE)


def _year(token, default_year) -> Optional[int]:
    value = token if token else default_year
    if value is None or value == "":
        return None
    try:
        y = int(str(value).strip())
    except ValueError:
        return None
    return y + 2000 if y < 100 else y


def _stamp(y, month, day, hh=None, mm=None) -> Optional[str]:
    try:
        dt = datetime(int(y), int(month), int(day),
                      int(hh) if hh is not None else 0,
                      int(mm) if mm is not None else 0)
    except (TypeError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:00+00:00")


def parse_dtg(text, default_year=None) -> Optional[str]:
    """ISO-8601 UTC timestamp for a string that is exactly one DTG, else None.

    default_year (e.g. the warning's own year, "26" or 2026) is used only
    when the DTG carries no year. It is a fallback, not a check: the
    number's year and the issue year can differ (NAVAREA IX 001/26 was
    issued 131500 UTC DEC 2025).
    """
    if not text:
        return None
    s = str(text).replace("﻿", "").strip()
    if not s:
        return None

    m = RE_DTG_NUMERIC.fullmatch(s)
    if m:
        month = MONTHS.get(m.group(4)[:3].lower())
        y = _year(m.group(5), default_year)
        if month is None or y is None:
            return None
        if int(m.group(2)) > 23 or int(m.group(3)) > 59:
            return None
        return _stamp(y, month, m.group(1), m.group(2), m.group(3))

    m = RE_DTG_LONG.fullmatch(s)
    if m:
        month = MONTHS.get(m.group(2)[:3].lower())
        if month is None:
            return None
        if int(m.group(4)) > 23 or int(m.group(5)) > 59:
            return None
        return _stamp(m.group(3), month, m.group(1), m.group(4), m.group(5))

    m = RE_DTG_ISOISH.fullmatch(s)
    if m:
        hh, mm = m.group(4), m.group(5)
        if hh is not None and (int(hh) > 23 or (mm is not None and int(mm) > 59)):
            return None
        return _stamp(m.group(1), m.group(2), m.group(3), hh, mm)

    return None


def find_dtg_line(raw_text, default_year=None) -> Optional[str]:
    """Issue time from the first line of raw_text that is only a DTG, else None."""
    for line in (raw_text or "").splitlines():
        stamp = parse_dtg(line, default_year)
        if stamp:
            return stamp
    return None

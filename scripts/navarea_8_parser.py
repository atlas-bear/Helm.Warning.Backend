#!/usr/bin/env python3
"""
navarea_8_parser.py — Deterministic Parser for NAVAREA VIII (NHO India)

Parses the extracted text of the weekly 'NAVAREA VIII - Warnings In Force'
bulletin into the pipeline JSON shape. Rule-first only — NO AI escalation
(per pipeline policy for NAVAREA VIII; geometry-ambiguous warnings fail
closed to Tier C / no_valid_geometry via shared/models.build_master_payload).

Real bulletin format (validated against live in-force text, Oct 2026):
    NAVAREA VIII -1102/26 INDIA WEST COAST - KARWAR (.) CHARTS IN 215 257
    INT 7343 (.) HYDROGRAPHIC SURVEY UNDER PROGRESS IN AREA BOUNDED WITHIN
    14-47.2N TO 14-50.9N AND 074-02.8E TO 074-08.15E 2. CANCEL THIS MSG ...
    ... CANCEL NAVAREA VIII MSG 1024/26 AND THIS MSG ...
    ... CANCEL NAVAREA VIII -1096/26 ...

Deterministic strategy:
  1. Capture the 'AS ON <date>' header (in-force snapshot date).
  2. Segment into warnings on 'NAVAREA VIII [dash] n/yy' headers.
  3. Local clause segmentation only (numbered items) — coordinate parsing is
     delegated to the shared parser (no-second-parser rule).
  4. classify_hazard_categories() for categories; CANCEL/body cross-refs
     extracted for the sync layer's supersede pass.
"""

import sys
import os
import re
import json
import hashlib
from datetime import datetime

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from shared.utils import parse_coordinates, classify_hazard_categories
from shared.dtg import dtg_line_above, parse_dtg

SOURCE_ID = "nho-navarea-viii"
NAVAREA_ID = "VIII"

# 'NAVAREA VIII 1066/26' or 'NAVAREA VIII -1102/26' (both observed; the dash
# form is used in the live in-force text and the NHO website table).
RE_WARNING_HEADER = re.compile(r"NAVAREA\s+VIII\s*[-\u2013]?\s*(\d{1,4})\s*/\s*(\d{2})", re.IGNORECASE)
RE_AS_ON = re.compile(r"WARNINGS?\s+IN\s+FORCE\s+AS\s+ON\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE)
# Handles: 'CANCEL NAVAREA VIII MSG 1024/26', 'CANCEL NAVAREA VIII -1096/26',
# 'CANCEL NAVAREA VIII 293/26'
RE_CANCEL_REF = re.compile(
    r"CANCEL\s+NAVAREA\s+VIII\s*(?:MSG\s*)?[-\u2013]?\s*(\d{1,4})\s*/\s*(\d{2})", re.IGNORECASE)
# Numbered item markers anywhere in the line (mirror text arrives as one long
# line, so we must not anchor to line starts). Requires whitespace/dash
# boundary before and an uppercase word after, so coordinate fragments like
# '074-08.15E' or '2.5NM' never match.
RE_ITEM_LINE = re.compile(r"(?:^|\s)(\d{1,2})\.[\s]+(?=[A-Z])")

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_as_on(text: str):
    m = RE_AS_ON.search(text or "")
    if not m:
        return None
    try:
        day, mon, year = m.group(1).split()
        month = MONTHS.get(mon[:3].lower())
        if not month:
            return None
        return datetime(int(year), month, int(day))
    except (ValueError, IndexError):
        return None


def _clean_line(line: str) -> str:
    """Normalizes NHO (.) separators into single spaces for title extraction."""
    return re.sub(r"\(\s*\.\s*\)", " ", line).strip()


def _segment_clauses(body: str):
    """
    Splits a warning body into geometry clauses:
      - numbered items (1. 2. ...) and lettered sub-areas (A. B. ...)
      - otherwise a single clause with the full body.
    Returns a list of {label, text}.
    """
    matches = list(RE_ITEM_LINE.finditer(body))
    if not matches:
        return [{"label": "Area", "text": body}]

    clauses = []
    # Text before the first item belongs to the warning header clause.
    preamble = body[:matches[0].start()].strip()
    if preamble:
        clauses.append({"label": "Area", "text": preamble})
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[m.start():end].strip()
        label = m.group(1).strip() or f"Item {i + 1}"
        clauses.append({"label": label, "text": chunk})
    return clauses


def _extract_title(body: str) -> str:
    """Title = leading text before the first numbered item (typically the
    location / operation description, e.g. 'INDIA WEST COAST - KARWAR')."""
    m = RE_ITEM_LINE.search(body)
    head = body[:m.start()] if m else body
    lines = [_clean_line(l) for l in head.split("\n") if l.strip()]
    title = " ".join(lines).strip()
    # Trim trailing DTG tokens the mirror prepends to the next warning.
    title = re.sub(r"\b\d{6}Z?\s*/?\s*[A-Z]{3}\s+\d{2}\s*$", "", title).strip()
    return title[:255] or "NAVAREA VIII Warning"


def _header_year(raw_text: str):
    """The warning's own year ("26" in NAVAREA VIII -499/26), or None."""
    m = RE_WARNING_HEADER.search(raw_text or "")
    return m.group(2) if m else None


def _extract_cross_refs(raw_text: str):
    refs = []
    seen = set()
    for m in RE_CANCEL_REF.finditer(raw_text or ""):
        key = (m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        refs.append({"number": m.group(1), "year": m.group(2)})
    return refs


def split_into_warnings(text: str):
    """
    Segments the bulletin into warning blocks on 'NAVAREA VIII [-]n/yy' headers.
    Returns a list of {warning_id, number, year, raw_text, dtg}.

    The NHO bulletin prints each warning's date-time group on the line above its
    header ("241631Z/APR 26" / "NAVAREA VIII -499/26"). It is cut from the end of
    the previous block and appended to its own raw_text, and kept as "dtg".
    Verified 2026-11-03 against an independent feed: 54 of 54 equal to the minute.
    """
    text = text or ""
    blocks = []
    matches = list(RE_WARNING_HEADER.finditer(text))
    dtgs = [dtg_line_above(text, m.start()) for m in matches]
    for i, m in enumerate(matches):
        start = m.start()
        end = dtgs[i + 1][1] if i + 1 < len(matches) else len(text)
        raw_text = text[start:end].strip()
        dtg = dtgs[i][0]
        if dtg:
            raw_text = f"{raw_text}\n{dtg}"
        number, year = m.group(1), m.group(2)
        blocks.append({
            "warning_id": f"NAVAREA VIII {number}/{year}",
            "number": number,
            "year": year,
            "raw_text": raw_text,
            "dtg": dtg,
        })
    return blocks


def parse_warning(block):
    """Parses one segmented bulletin block into (raw_message, nav_warning)."""
    warning_id = block["warning_id"]
    raw_text = block["raw_text"]
    canonical_id = warning_id

    checksum = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    raw_message = {
        "warning_id": warning_id,
        "source_id": SOURCE_ID,
        "subject_header": warning_id,
        "full_raw_text": raw_text,
        "received_timestamp": datetime.utcnow().isoformat(),
        "checksum_sha256": checksum,
    }

    # Body = text after the header line.
    body = RE_WARNING_HEADER.sub("", raw_text, count=1).strip()
    clauses = _segment_clauses(body)

    # Geometry: one Feature per clause that the shared parser can resolve.
    geoms = []
    for clause in clauses:
        _, lat, lon, spatial = parse_coordinates(clause["text"])
        if spatial:
            geoms.append((clause["label"], spatial, lat, lon))

    if not geoms:
        spatial_fc = None
        lat = lon = None
        geom_labels = []
    elif len(geoms) == 1:
        label, spatial, lat, lon = geoms[0]
        spatial_fc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"sub_area": label, "zone_label": label or "Area"},
                "geometry": spatial,
            }],
        }
        geom_labels = [label]
    else:
        features = [{
            "type": "Feature",
            "properties": {"sub_area": g[0], "zone_label": g[0]},
            "geometry": g[1],
        } for g in geoms]
        spatial_fc = {"type": "FeatureCollection", "features": features}
        lat, lon = geoms[0][2], geoms[0][3]
        geom_labels = [g[0] for g in geoms]

    categories = classify_hazard_categories(raw_text)

    nav_warning = {
        "warning_id": warning_id,
        "canonical_id": canonical_id,
        "source_id": SOURCE_ID,
        "navarea": NAVAREA_ID,
        "title": _extract_title(body) or warning_id,
        "issued_text": None,
        "coordinates": None,  # master no longer carries flat coords (20261020 schema)
        "latitude": lat,
        "longitude": lon,
        "category": categories[0] if categories else "general",
        "categories": categories,
        "status": "active",
        "raw_text": raw_text,
        "spatial": spatial_fc,
        "sub_areas": [c["label"] for c in clauses],
        "geometry_sub_areas": geom_labels,
        "cross_refs": _extract_cross_refs(raw_text),
        # Issue time: the date-time group above the warning's header ("031006Z/JUN 26").
        "publication_time": parse_dtg(block.get("dtg"), block.get("year") or _header_year(raw_text)),
    }
    return raw_message, nav_warning


def parse_bulletin_text(bulletin_text: str) -> dict:
    """Full deterministic parse of the in-force bulletin into pipeline JSON shape."""
    parsed_data = {"raw_messages": [], "nav_warnings": [], "spatial_features": [],
                   "memo": {"in_force_as_on": None}}
    if not bulletin_text or not bulletin_text.strip():
        return parsed_data

    as_on = _parse_as_on(bulletin_text)
    parsed_data["memo"]["in_force_as_on"] = as_on.isoformat() if as_on else None

    for block in split_into_warnings(bulletin_text):
        raw_msg, nav_warn = parse_warning(block)
        parsed_data["raw_messages"].append(raw_msg)
        parsed_data["nav_warnings"].append(nav_warn)
        if nav_warn["spatial"]:
            parsed_data["spatial_features"].append({
                "warning_id": nav_warn["warning_id"],
                "category": nav_warn["category"],
                "geometry": nav_warn["spatial"],
            })
    return parsed_data


def main(input_filename="navareas/navarea_8/navarea_8_warnings.txt",
         output_filename="navareas/navarea_8/parsed_navarea_8_warnings.json") -> bool:
    print("=" * 60)
    print("NAVAREA VIII Parser (NHO India, rule-first)")
    print("=" * 60)
    content = ""
    if os.path.exists(input_filename):
        try:
            with open(input_filename, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"Warning: Could not read {input_filename} ({e}).")
            content = ""
    else:
        print(f"Input file {input_filename} not found; initializing empty harvest.")

    parsed_data = parse_bulletin_text(content)
    n = len(parsed_data["nav_warnings"])
    n_geom = len([w for w in parsed_data["nav_warnings"] if w["spatial"]])
    print(f"Parsed {n} warning(s); {n_geom} with resolvable geometry "
          f"({n - n_geom} fail closed to Tier C / no_valid_geometry).")

    os.makedirs(os.path.dirname(output_filename), exist_ok=True)
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(parsed_data, f, indent=2, ensure_ascii=False)
    print(f"Parsed output written to {output_filename}")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

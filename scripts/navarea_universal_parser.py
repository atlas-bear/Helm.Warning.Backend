#!/usr/bin/env python3
"""
navarea_universal_parser.py — Deterministic Universal Parser for NAVAREA
regions without a dedicated parser (II, III, V, VI, VII, IX, X and the
later MET areas XIII–XXI).

This file keeps the repo's deterministic parsing as the final authority.
A Gemini-first path is layered in front of it (shared/gemini_navarea_parser),
but the rule parser remains the fallback whenever the model is
unavailable, times out, or returns invalid data. If the Gemini layer is
absent entirely, this parser degrades to pure rule-first parsing instead
of crashing on import (PR-A hotfix for the #29 breakage).

Header quirks (live coordinator corpus, Oct 2026) are carried by
shared/navarea_universal_text.py: per-region extra header patterns
(V bare '0648/26', VI 'NAVAREA - 0355-2026', VII 'n OF yyyy',
XV 'NAVAREA XV 0142' with a bulletin year hint, XIX 'Number:n/yy'),
2- and 4-digit years (normalized to 2-digit warning ids), the XIV
'WARNING' word, and cancel-prefixed headers that must NEVER split a
block (a 'CANCEL NAVAREA II 100/26' cross-ref is part of the enclosing
warning, not a new one).

Spanish bulletins (XV Chile / XVI Peru) get titles machine-translated
via shared/translation.py — fail-closed: without a key or on any error
the original Spanish title is kept and the ingest still succeeds.

NAVAREA I companion exception: NAVAREA I (UK / North Atlantic) warnings
are routinely bundled by universal-region publishers (e.g. Norwegian
NAVAREA XIX bulletins also carry North Sea NAVAREA I items). Its headers
never make a paste mixed-region, and its blocks are truncated out during
segmentation so the regional snapshot stays pure. A paste that is ONLY
NAVAREA I still routes to the dedicated NAVAREA 1 parser.
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from shared.gemini_navarea_parser import parse_bulletin_with_fallback
except ImportError:  # PR-A hotfix: Gemini layer not deployed — stay usable.
    parse_bulletin_with_fallback = None

from shared.utils import (
    parse_coordinates, parse_coordinates_detailed, classify_hazard_categories,
)
from shared.navarea_universal_text import (
    UNIVERSAL_REGIONS, NAVAREA_NAMES, regions_in_text,
    region_header_patterns, region_cancel_patterns, normalize_year, warning_header_re,
    RE_YEAR_HINT, TITLE_TRANSLATION_REGIONS,
    AUSCOAST_AREAS, RE_AUSCOAST_HEADER, RE_AUSCOAST_CANCEL, RE_NEVER_MATCHES,
)
from shared import translation
from shared.dtg import dtg_line_above, find_dtg_line, parse_dtg

# Numbered item line: '1. HAZARDOUS ...' / 'A. ...'-adjacent numbering.
# Line-anchored: a sentence-ending date like '... UTC oct 26.\nTo report'
# must never be mistaken for item 26 (NAVAREA II rig-list bug).
RE_ITEM_LINE = re.compile(r"^[ \t]*(\d{1,2})\.[\s]+(?=[A-Z])", re.MULTILINE)
# Lettered sub-area line: 'A. 12-34.5N ...' — the letter DIRECTLY introduces
# coordinates (lookahead digit). NAVAREA XI 0475/26 style: areas A./B./C.
# must parse as SEPARATE geometries, never one merged shape.
RE_LETTERED_LINE = re.compile(r"^[ \t]*([A-Z])\.[\s]+(?=\d)", re.MULTILINE)
# 'WARNINGS IN FORCE AS ON 13 OCTOBER 2026' (Australia X bulletin header).
RE_AS_ON = re.compile(
    r"WARNINGS?\s+IN\s+FORCE\s+AS\s+ON\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    re.IGNORECASE)
RE_PATH_REGION = re.compile(r"navarea[_-]([a-z0-9]+)", re.IGNORECASE)
# Broadcast tail: Australia terminates each message with 'NNNN' (and the
# Russian XX with '.=' before it). These end-of-message markers must not
# leak into the last warning's raw text / title. Requires a run of 3-6
# N's so a trailing single-coordinate hemisphere letter is never eaten.
RE_BROADCAST_TAIL = re.compile(r"(?:[\s=.]*N{3,6}[\s=.]*)+\s*$")

# Title cut length (audit finding F7, 2026-10-30): the title cuts at
# the first sentence boundary at or past this many characters — a
# short location opener keeps its operation sentence, an already-long
# first sentence ends the title, and the 255 hard cap is never the
# thing that truncates a title mid-word.
_TITLE_MIN_CHARS = 30

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Validity-date extraction (audit finding F1, 2026-10-29): the universal
# pipeline never emitted valid_from/valid_to — 301 live rows were 100%
# NULL on both, so the expiry lifecycle could never fire. Fail-closed:
# any unparseable token yields None, never a guess (hemisphere-safeguard
# philosophy). Patterns validated against the live corpus:
#   ranges:      "14 MAY 26 TO 14 NOV 26" / "270300 UTC OCT 26 TO ...
#                090259 UTC NOV 26" / "FROM 08 OCT TO 10 NOV 26"
#   self-cancel: "CANCEL THIS WARNING 142100 UTC NOV 26" /
#                "CANCEL THIS MSG 301000Z NOV" (authoritative end date)
#   until:       "UNTIL 31 DEC 2026" (never "UNTIL FURTHER NOTICE")
RE_VALIDITY_RANGE = re.compile(
    r"(?:FROM\s+)?(\d{1,2})(\d{2})?(\d{2})?(?:\s*(?:Z|UTC))?"
    r"\s+([A-Za-z]{3,9})\.?(?:\s+(\d{2,4}))?\s+TO\s+"
    r"(\d{1,2})(\d{2})?(\d{2})?(?:\s*(?:Z|UTC))?"
    r"\s+([A-Za-z]{3,9})\.?(?:\s+(\d{2,4}))?",
    re.IGNORECASE)
RE_CANCEL_SELF = re.compile(
    r"CANCEL\s+THIS\s+(?:WARNING|MSG|MESSAGE)\s+"
    r"(\d{1,2})(\d{2})?(\d{2})?(?:\s*(?:Z|UTC))?"
    r"\s+([A-Za-z]{3,9})\.?(?:[/\s]+(\d{2,4}))?",
    re.IGNORECASE)
RE_UNTIL = re.compile(
    r"\bUNTIL\s+(\d{1,2})\s+([A-Za-z]{3,9})\.?(?:\s+(\d{2,4}))?",
    re.IGNORECASE)

# NAVAREA I (UK / North Atlantic) is a tolerated COMPANION region: its
# warning headers never make a paste mixed-region, and its blocks are
# excluded from segmentation. Pure-NAVAREA-I pastes are still rejected by
# resolve_region (dedicated parser region).
COMPANION_ROMANS = {"I"}


def _region_config(region_key):
    """Resolves region-dependent constants (regexes, source_id, numeral).

    AUSCOAST pseudo-regions ('auscoast-a' ... 'auscoast-h') are
    Australian national coastal warnings outside NAVAREA numbering:
    area-scoped warning ids ('AUSCOAST B 327/26'), their own source ids
    ('manual-auscoast-b'), and no companion region. They parse rule-only
    by construction (the Gemini layer refuses keys outside
    UNIVERSAL_REGIONS).
    """
    if region_key in AUSCOAST_AREAS:
        area = AUSCOAST_AREAS[region_key]
        return {
            "key": region_key,
            "roman": area,
            "id_prefix": "AUSCOAST " + area,
            "source_id": "manual-" + region_key,
            "navarea_id": "AUSCOAST " + area,
            "header_res": [RE_AUSCOAST_HEADER],
            "cancel_res": [RE_AUSCOAST_CANCEL],
            "companion_re": RE_NEVER_MATCHES,
            "translate_titles": False,
        }
    roman = UNIVERSAL_REGIONS[region_key]
    return {
        "key": region_key,
        "roman": roman,
        "id_prefix": "NAVAREA " + roman,
        "source_id": "manual-navarea-" + region_key,
        "navarea_id": roman,
        "header_res": region_header_patterns(region_key, roman),
        "cancel_res": region_cancel_patterns(region_key, roman),
        "companion_re": warning_header_re("I"),  # default NAVAREA I header
        "translate_titles": region_key in TITLE_TRANSLATION_REGIONS,
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


def _parse_year_hint(text: str):
    """Bulletin-level year for regions whose headers omit it (NAVAREA XV):
    'YEAR 2026:' or 'DATE: OCT/16/2026'. None when no hint exists."""
    m = RE_YEAR_HINT.search(text or "")
    if not m:
        return None
    year = m.group(2) or m.group(3)  # 'YEAR 2026' alt / 'DATE: ...2026' alt
    return normalize_year(year)



def _extract_validity_dates(raw_text, year):
    """Extracts (valid_from, valid_to) ISO stamps from one warning block.

    Audit finding F1 (2026-10-29): the universal pipeline never emitted
    validity dates, so the shared expiry lifecycle could never fire and
    every universal row lived until manually cancelled. Extraction is
    conservative and fail-closed:
      - an explicit date range sets both ends;
      - a self-cancel dateline ("CANCEL THIS WARNING ...") is the
        authoritative end and wins over a range end;
      - "UNTIL <date>" sets valid_to when nothing else did;
      - a token with no year falls back to the warning year (from the
        header id / bulletin hint); a year-less END month earlier than
        the START month rolls over to the next year;
      - anything unparseable ("UNTIL FURTHER NOTICE", day-only ranges
        like "FROM 20 TO 31 OCT 26", impossible dates, unknown months)
        yields None — never a guess.
    """
    default_year = None
    if year is not None:
        try:
            y = int(str(year))
            default_year = y + 2000 if y < 100 else y
        except ValueError:
            default_year = None

    def parse_token(day, hh, mm, mon, ytok):
        month = MONTHS.get((mon or "")[:3].lower())
        if month is None:
            return None
        try:
            day_i = int(day)
        except (TypeError, ValueError):
            return None
        if not 1 <= day_i <= 31:
            return None
        if ytok:
            try:
                y = int(ytok)
            except ValueError:
                return None
            if y < 100:
                y += 2000
        else:
            y = default_year
        if y is None:
            return None
        try:
            datetime(y, month, day_i)
        except ValueError:
            return None
        stamp = "%04d-%02d-%02d" % (y, month, day_i)
        if hh:
            try:
                h_i, m_i = int(hh), int(mm or 0)
            except ValueError:
                return None
            if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
                return None
            stamp += "T%02d:%02d:00Z" % (h_i, m_i)
        return stamp

    text = raw_text or ""
    valid_from = valid_to = None
    m = RE_VALIDITY_RANGE.search(text)
    if m:
        valid_from = parse_token(m.group(1), m.group(2), m.group(3),
                                 m.group(4), m.group(5))
        valid_to = parse_token(m.group(6), m.group(7), m.group(8),
                               m.group(9), m.group(10))
        if valid_from and valid_to and not m.group(10):
            # Year-less ranges roll over: "16 OCT TO 14 JAN" -> Jan +1.
            if int(valid_to[5:7]) < int(valid_from[5:7]):
                valid_to = "%04d" % (int(valid_to[:4]) + 1) + valid_to[4:]
    m = RE_CANCEL_SELF.search(text)
    if m:
        cancel_to = parse_token(m.group(1), m.group(2), m.group(3),
                                m.group(4), m.group(5))
        if cancel_to:
            valid_to = cancel_to  # self-cancel dateline is authoritative
    if valid_to is None:
        m = RE_UNTIL.search(text)
        if m:
            valid_to = parse_token(m.group(1), None, None,
                                   m.group(2), m.group(3))
    return valid_from, valid_to


def _clean_line(line: str) -> str:
    """Normalizes '(.)' separators into single spaces for title extraction."""
    return re.sub(r"\(\s*\.\s*\)", " ", line).strip()


def _segment_clauses(body: str):
    """
    Splits a warning body into geometry clauses:
      - numbered items (1. 2. ...) and lettered sub-areas (A. B. ...) —
        the lettered form only when the letter DIRECTLY introduces a
        coordinate ('A. 12-34.5N ...'); NAVAREA XI 0475/26 areas A/B/C
        must never merge into one shape.
      - otherwise a single clause with the full body.
    Returns a list of {label, text}.
    """
    marks = [
        {"start": m.start(), "label": m.group(1).strip()}
        for m in RE_ITEM_LINE.finditer(body)
    ] + [
        {"start": m.start(), "label": m.group(1).strip()}
        for m in RE_LETTERED_LINE.finditer(body)
    ]
    if not marks:
        return [{"label": "Area", "text": body}]
    marks.sort(key=lambda x: x["start"])
    clauses = []
    preamble = body[:marks[0]["start"]].strip()
    if preamble:
        clauses.append({"label": "Area", "text": preamble})
    for idx, mark in enumerate(marks):
        end = marks[idx + 1]["start"] if idx + 1 < len(marks) else len(body)
        chunk = body[mark["start"]:end].strip()
        if not chunk:
            continue
        label = mark["label"] or "Item " + str(idx + 1)
        clauses.append({"label": label, "text": chunk})
    return clauses


def _zone_label(labels):
    """Display zone label built from the source's own sub-area markers.

    Audit finding F5 (2026-10-30): the live DB held 270 generic 'Area'
    zone labels plus bare numbers and letters. Labels now come from the
    markers the source itself used: 'A' -> 'Area A', a short lettered
    or numbered group -> 'Areas A-D'; longer prose labels pass through
    unchanged; nothing stays bare.
    """
    clean = [str(l).strip() for l in labels or [] if str(l or "").strip()]
    if not clean:
        return "Area"
    if len(clean) == 1:
        label = clean[0]
        return ("Area " + label) if len(label) <= 2 else label
    if all(len(l) <= 2 for l in clean):
        return "Areas " + clean[0] + "-" + clean[-1]
    return "Areas"


def _extract_title(body: str, cfg) -> str:
    """Title = leading text before the first numbered item (typically the
    location / operation description).

    Audit finding F7 (2026-10-30): the title joined the whole head, so
    the median title ran past 100 characters and the longest hit the
    255 hard cap mid-word. The title now cuts at the first sentence
    boundary at or past _TITLE_MIN_CHARS: a short location opener
    ('EAST CHINA SEA.') keeps the operation sentence after it, an
    already-long first sentence ends the title there, and a head with
    no sentence boundary (coordinate lists) keeps today's behaviour.
    """
    m = RE_ITEM_LINE.search(body)
    head = body[:m.start()] if m else body
    lines = [_clean_line(l) for l in head.split("\n") if l.strip()]
    title = " ".join(lines).strip()
    cut = None
    for sm in re.finditer(r"[.!?](?=\s|$)", title):
        if sm.end() >= _TITLE_MIN_CHARS:
            cut = sm.end()
            break
    if cut:
        title = title[:cut].strip()
    title = re.sub(r"\b\d{6}Z?\s*/?\s*[A-Z]{3}\s+\d{2}\s*$", "", title).strip()
    return title[:255] or ((cfg.get("id_prefix") or ("NAVAREA " + cfg["roman"]))
                              + " Warning")


def _extract_cross_refs(raw_text: str, cfg):
    refs = []
    seen = set()
    for rex in cfg["cancel_res"]:
        for m in rex.finditer(raw_text or ""):
            key = (m.group(1), m.group(2))
            if key in seen:
                continue
            seen.add(key)
            refs.append({"number": m.group(1), "year": normalize_year(m.group(2))})
    return refs


def _header_matches(text, cfg):
    """All warning-header matches for the target region, CANCEL-prefixed
    cross-references excluded: a 'CANCEL NAVAREA II 100/26' reference
    inside a warning body belongs to that warning — it must never split
    the block (previously it created a phantom warning). Overlapping
    matches from different patterns (default + region extra) collapse to
    the first."""
    found = []
    for rex in cfg["header_res"]:
        for m in rex.finditer(text or ""):
            prefix = text[max(0, m.start() - 8):m.start()].upper()
            if "CANCEL" in prefix:
                continue
            found.append(m)
    found.sort(key=lambda m: (m.start(), m.end()))
    out = []
    last_end = -1
    for m in found:
        if m.start() < last_end:
            continue  # nested inside an earlier (higher-priority) match
        out.append(m)
        last_end = m.end()
    return out


def _block_starts(text, cfg):
    """Start positions that END a warning block: the next header of the
    target region, or a tolerated companion (NAVAREA I) header — CANCEL
    cross-references excluded — so companion blocks are never absorbed
    into the tail of the last regional warning."""
    starts = [m.start() for m in _header_matches(text, cfg)]
    for m in cfg["companion_re"].finditer(text or ""):
        prefix = text[max(0, m.start() - 8):m.start()].upper()
        if "CANCEL" in prefix:
            continue
        starts.append(m.start())
    return sorted(set(starts))


def _collapse_same_warning(matches):
    """Dedupes header matches that point at the SAME warning: a region can
    match a warning twice (e.g. NAVAREA XIX hits both the 'Number:123/26'
    extra anchor and the default 'NAVAREA XIX 123/26...' header). The
    second start would truncate the block before its coordinates — keep
    only the FIRST match per (number, year)."""
    seen = set()
    out = []
    for m in matches:
        year = m.group(2)
        key = (m.group(1), normalize_year(year) if year else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def split_into_warnings(text: str, cfg):
    """
    Segments the bulletin into warning blocks on the region's header
    patterns (default + per-region extras). Returns a list of
    {warning_id, number, year, header_text, raw_text, dtg}.

    A date-time group printed directly above a header (every NAVAREA IX
    document opens with one) belongs to that header's warning: it is cut
    from the end of the previous block, appended to its own raw_text and
    kept as "dtg" (see shared.dtg.dtg_line_above).
    """
    blocks = []
    matches = _collapse_same_warning(_header_matches(text, cfg))
    starts = [m.start() for m in matches]
    for m in cfg["companion_re"].finditer(text or ""):
        prefix = text[max(0, m.start() - 8):m.start()].upper()
        if "CANCEL" in prefix:
            continue
        starts.append(m.start())
    starts = sorted(set(starts))
    for m in matches:
        start = m.start()
        end = len(text)
        for s in starts:
            if s > start:
                end = dtg_line_above(text, s)[1]
                break
        raw_text = text[start:end].strip()
        raw_text = RE_BROADCAST_TAIL.sub("", raw_text).strip()
        dtg = dtg_line_above(text, start)[0]
        if dtg:
            raw_text = f"{raw_text}\n{dtg}"
        number, year = m.group(1), m.group(2)
        blocks.append({
            "number": number,
            "year": year,  # may be None (NAVAREA XV) — resolved by caller
            "header_text": m.group(0),
            "raw_text": raw_text,
            "dtg": dtg,
        })
    return blocks


def parse_warning(block, cfg):
    """Parses one segmented bulletin block into (raw_message, nav_warning)."""
    number = block["number"]
    year = normalize_year(block["year"]) or block.get("year_hint")
    roman = cfg["roman"]
    id_prefix = cfg.get("id_prefix") or ("NAVAREA " + roman)
    warning_id = (id_prefix + " " + str(number)
                 + (("/" + str(year)) if year else ""))
    canonical_id = warning_id
    raw_text = block["raw_text"]

    checksum = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    raw_message = {
        "warning_id": warning_id,
        "source_id": cfg["source_id"],
        "subject_header": warning_id,
        "full_raw_text": raw_text,
        "received_timestamp": datetime.utcnow().isoformat(),
        "checksum_sha256": checksum,
    }

    body = raw_text.replace(block["header_text"], "", 1).strip()
    clauses = _segment_clauses(body)

    geoms = []
    geom_sweeps = []
    qa_flags = []
    _dq_order = {"EXPLICIT": 0, "INFERRED": 1, "AMBIGUOUS": 2}
    data_quality = None
    for clause in clauses:
        _, lat, lon, spatial, report = parse_coordinates_detailed(clause["text"])
        for flag in report.get("flags") or []:
            if flag not in qa_flags:
                qa_flags.append(flag)
        basis = report.get("basis")
        if basis:
            entry = "geometry_basis:" + basis
            if entry not in qa_flags:
                qa_flags.append(entry)
        clause_quality = report.get("data_quality")
        if clause_quality and (
            data_quality is None
            or _dq_order[clause_quality] > _dq_order[data_quality]
        ):
            data_quality = clause_quality
        sweep = None
        if (report.get("sweep_radius_nm") is not None
                and report.get("sweep_start_azimuth") is not None
                and report.get("sweep_end_azimuth") is not None):
            sweep = {
                "sweep_radius_nm": report["sweep_radius_nm"],
                "sweep_start_azimuth": report["sweep_start_azimuth"],
                "sweep_end_azimuth": report["sweep_end_azimuth"],
            }
        if spatial:
            geoms.append((clause["label"], spatial, lat, lon))
            geom_sweeps.append(sweep)
    if not geoms:
        spatial_fc = None
        lat = lon = None
        geom_labels = []
    elif (len(geoms) >= 2
          and all(g[1].get("type") == "Point" for g in geoms)):
        # Numbered/lettered item lists of DISCRETE positions (rig
        # lists, buoy lists): per-clause Point features would regress
        # the scattered-markers contract (NAVAREA II 349/26, VII
        # 258/26 fixture tests) — ONE MultiPoint feature holds every
        # position. Lettered sub-areas that yield areas (XI 475/26
        # A/B/C polygons) are unaffected (not all Points).
        coords = [g[1]["coordinates"] for g in geoms]
        spatial_fc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"zone_label": _zone_label([g[0] for g in geoms])},
                "geometry": {"type": "MultiPoint", "coordinates": coords},
            }],
        }
        lats = [g[2] for g in geoms if g[2] is not None]
        lons = [g[3] for g in geoms if g[3] is not None]
        lat = round(sum(lats) / len(lats), 6) if lats else None
        lon = round(sum(lons) / len(lons), 6) if lons else None
        geom_labels = [g[0] for g in geoms]
    elif len(geoms) == 1:
        label, spatial, lat, lon = geoms[0]
        props = {"sub_area": label, "zone_label": _zone_label([label])}
        if geom_sweeps and geom_sweeps[0]:
            props.update(geom_sweeps[0])
        spatial_fc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": props,
                "geometry": spatial,
            }],
        }
        geom_labels = [label]
    else:
        features = []
        for i, g in enumerate(geoms):
            props = {"sub_area": g[0], "zone_label": _zone_label([g[0]])}
            if i < len(geom_sweeps) and geom_sweeps[i]:
                props.update(geom_sweeps[i])
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": g[1],
            })
        spatial_fc = {"type": "FeatureCollection", "features": features}
        lat, lon = geoms[0][2], geoms[0][3]
        geom_labels = [g[0] for g in geoms]

    categories = classify_hazard_categories(raw_text)

    title = _extract_title(body, cfg) or warning_id

    # Non-English bulletins (XV/XVI Spanish; XIX may carry Norwegian
    # items): fail-closed title translation via language detection. The
    # raw text stays the original language; on ANY translation problem
    # the original title is kept and the ingest still succeeds.
    if cfg.get("translate_titles"):
        translated = translation.maybe_translate_title(title)
        if translated:
            title = translated[:255]

    # Audit finding F1 (2026-10-29): emit validity dates so the shared
    # expiry lifecycle can fire. Downstream (build_master_payload and the
    # upsert RPC) already consumed these keys — the parser just never
    # produced them. Fail-closed: None means "no date evidence".
    valid_from, valid_to = _extract_validity_dates(raw_text, year)
    # Issue time: the warning's own date-time group. NAVAREA IX prints it
    # above the header (moved here by split_into_warnings); NAVAREA III
    # prints "25 February 2022 00:00 UTC" under it. build_master_payload
    # falls back to it for valid_from.
    publication_time = (parse_dtg(block.get("dtg"), year) if block.get("dtg")
                        else find_dtg_line(raw_text, year))

    nav_warning = {
        "warning_id": warning_id,
        "canonical_id": canonical_id,
        "source_id": cfg["source_id"],
        "navarea": cfg["navarea_id"],
        "title": title,
        "issued_text": None,
        "coordinates": None,
        "latitude": lat,
        "longitude": lon,
        "category": categories[0] if categories else "general",
        "categories": categories,
        "status": "active",
        "raw_text": raw_text,
        "spatial": spatial_fc,
        "sub_areas": [c["label"] for c in clauses],
        "geometry_sub_areas": geom_labels,
        "cross_refs": _extract_cross_refs(raw_text, cfg),
        "qa_flags": qa_flags,
        "data_quality": data_quality or "EXPLICIT",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "publication_time": publication_time,
    }
    return raw_message, nav_warning


def parse_bulletin_text(bulletin_text: str, region_key: str) -> dict:
    """Full deterministic parse of the bulletin into pipeline JSON shape."""
    cfg = _region_config(region_key)
    parsed_data = {"raw_messages": [], "nav_warnings": [], "spatial_features": [],
                   "memo": {"region": region_key, "navarea": cfg["navarea_id"],
                            "source_id": cfg["source_id"],
                            "in_force_as_on": None}}
    if not bulletin_text or not bulletin_text.strip():
        return parsed_data

    as_on = _parse_as_on(bulletin_text)
    parsed_data["memo"]["in_force_as_on"] = as_on.isoformat() if as_on else None

    year_hint = _parse_year_hint(bulletin_text)
    for block in split_into_warnings(bulletin_text, cfg):
        block["year_hint"] = year_hint
        raw_msg, nav_warn = parse_warning(block, cfg)
        parsed_data["raw_messages"].append(raw_msg)
        parsed_data["nav_warnings"].append(nav_warn)
        if nav_warn["spatial"]:
            parsed_data["spatial_features"].append({
                "warning_id": nav_warn["warning_id"],
                "category": nav_warn["category"],
                "geometry": nav_warn["spatial"],
            })
    return parsed_data


def resolve_region(input_filename, text):
    """Determines the region for this parse. Returns (region_key, error).

    NAVAREA I is tolerated as a companion region: its headers never make a
    paste mixed-region. One NON-companion region per paste is still
    required; pure-NAVAREA-I pastes route to the dedicated parser.
    """
    romans = regions_in_text(text)

    path_key = None
    if input_filename:
        parent = os.path.basename(os.path.dirname(os.path.abspath(input_filename)))
        m = RE_PATH_REGION.search(parent)
        if m:
            key = m.group(1).lower()
            if key in UNIVERSAL_REGIONS or key in AUSCOAST_AREAS:
                path_key = key

    if path_key in AUSCOAST_AREAS:
        # AUSCOAST pseudo-regions: NAVAREA X cross-references inside an
        # Australian coastal-warning paste are normal (same publisher),
        # so the roman-numeral checks below must not apply.
        return path_key, None
    if path_key:
        target_roman = UNIVERSAL_REGIONS[path_key]
        foreign = {r for r in romans if r != target_roman} - COMPANION_ROMANS
        if foreign:
            return None, (
                "mixed-region paste: input path targets NAVAREA "
                + target_roman + " but the text contains warning headers for "
                + ", ".join(sorted(foreign))
                + ". Split the paste per region (one region per ingest).")
        if romans & COMPANION_ROMANS:
            print("Note: tolerated companion NAVAREA I warning block(s) "
                  "excluded from this " + target_roman + " snapshot.")
        return path_key, None

    if not romans:
        return None, ("could not determine the NAVAREA region: no "
                      "'NAVAREA <numeral> n/yy' warning header found in the text.")
    candidates = romans - COMPANION_ROMANS or (romans & COMPANION_ROMANS)
    if len(candidates) > 1:
        return None, ("mixed-region paste: warning headers for "
                      + ", ".join(sorted(candidates))
                      + " found. One ingest must cover exactly one region.")
    roman = next(iter(candidates))
    if roman not in UNIVERSAL_REGIONS.values():
        return None, ("NAVAREA " + roman + " has a dedicated parser — ingest it "
                      "with the matching --navarea choice instead of a "
                      "universal-region ingest.")
    key = next(k for k, v in UNIVERSAL_REGIONS.items() if v == roman)
    return key, None


def main(input_filename="navareas/manual/navarea_ii/pasted_raw.txt",
         output_filename="navareas/manual/navarea_ii/parsed.json") -> bool:
    print("=" * 60)
    print("NAVAREA Universal Parser (rule-first — Gemini layer optional)")
    print("=" * 60)
    content = ""
    if os.path.exists(input_filename):
        try:
            with open(input_filename, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print("Warning: Could not read " + input_filename + " (" + str(e) + ").")
            content = ""
    else:
        print("Input file " + input_filename + " not found; initializing empty harvest.")

    region_key, error = resolve_region(input_filename, content)
    if region_key is None:
        print("ERROR: " + str(error))
        parsed_data = {"raw_messages": [], "nav_warnings": [],
                       "spatial_features": [], "memo": {}}
        os.makedirs(os.path.dirname(output_filename), exist_ok=True)
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(parsed_data, f, indent=2, ensure_ascii=False)
        return False

    cfg = _region_config(region_key)
    region_name = NAVAREA_NAMES.get(region_key, "")
    print("Region: NAVAREA " + cfg["roman"]
          + " (" + region_name + ") — source_id=" + cfg["source_id"])

    if parse_bulletin_with_fallback is not None:
        parsed_data = parse_bulletin_with_fallback(content, region_key, parse_bulletin_text)
    else:
        print("Gemini layer not installed — using deterministic rule parser only.")
        parsed_data = parse_bulletin_text(content, region_key)
    n = len(parsed_data["nav_warnings"])
    n_geom = len([w for w in parsed_data["nav_warnings"] if w.get("spatial")])
    print("Parsed " + str(n) + " warning(s); " + str(n_geom)
          + " with resolvable geometry (" + str(n - n_geom)
          + " fail closed to Tier C / no_valid_geometry).")

    os.makedirs(os.path.dirname(output_filename), exist_ok=True)
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(parsed_data, f, indent=2, ensure_ascii=False)
    print("Parsed output written to " + output_filename)
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

#!/usr/bin/env python
"""Audit a CSV of geocoded results and classify every row TRUST, SUSPECT or REJECT.

A batch geocoder reports a match rate, and the match rate is not a quality
measure. It counts the rows the geocoder was willing to return something for. A
city centroid returned at score 98 with match type Zip5 is a match by that
count, and it is not an address. Two thousand code cases can come back "96
percent matched" with several hundred of them stacked on one downtown point.

This reads the score, the match type, the coordinate and the input address
together, classifies each row, finds coordinate pile-ups, optionally checks
containment against a county boundary, and exits non-zero when the TRUST rate
falls below a floor. That exit code is the point: it stops a scheduled job
before it publishes the map.

Why not the geocoder's own report. Esri's Geocode Addresses writes Status,
Score and Addr_type, and the ArcGIS Pro rematch pane shows all three, which is
the right data. It reports per row, and nobody rematches two thousand rows by
eye. The gap is the aggregate refusal: a threshold a scheduled job can fail on,
and the pile-up count, which no per-row field can express because it is a
property of the batch.

A healthy aggregate can also hide one kind of address. --by-class sorts every
row by the class of its submitted address (a directional, a numbered street, a
unit, a PO box, a rural route, a street suffix) and gates each class on the
same floor, with a Wilson interval and a refusal when a class has too few rows
to judge. --compare puts a second result file for the same addresses, such as
a control geocoder, beside it class by class.

    python geocodesift.py --self-test
    python geocodesift.py --csv geocoded.csv --profile esri
    python geocodesift.py --csv geocoded.csv --boundary county.geojson --min-trust-rate 0.95
    python geocodesift.py --csv geocoded.csv --out audited.csv --apply
    python geocodesift.py --csv geocoded.csv --by-class
    python geocodesift.py --csv vendor.csv --compare control.csv

Exit codes: 0 the batch passed, 1 the batch failed the gate, 2 the input could
not be read or the tool itself failed, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Score at or above which a row may be TRUST, as a percentage of the profile's
# score scale. 90 is the Esri figure people already use for rematch triage.
DEFAULT_TRUST_SCORE = 90.0

# Score below which a row is REJECT outright, whatever the match type says.
DEFAULT_SUSPECT_SCORE = 75.0

# Rows sharing one rounded coordinate. At or above this count the group is
# reported as a pile-up and every row in it drops to SUSPECT. 5 clears a small
# apartment complex, whose units legitimately share one rooftop point.
DEFAULT_PILEUP_THRESHOLD = 5

# Decimal places the coordinate is rounded to before grouping. 5 is about one
# metre in decimal degrees, and a tenth of a foot in State Plane feet.
DEFAULT_PILEUP_PRECISION = 5

# House number difference tolerated between the input address and the matched
# address. Interpolated street matches are routinely off by a few numbers; a
# difference above this means a different block.
DEFAULT_HOUSE_TOLERANCE = 20

# TRUST rate the batch must reach. Below it, main() returns 1. With --by-class
# every address class must reach it too.
DEFAULT_MIN_TRUST_RATE = 0.90

# Rows an address class needs before its rate alone is judged. Under it the
# exact 95 percent interval decides: BELOW when it lies wholly under the floor
# (2 of 20), ok when wholly over it, and a refusal when it holds the floor
# (3 of 5 is 60 percent, but its interval runs from 15 to 95 percent).
DEFAULT_MIN_CLASS_ROWS = 30

# The z value of the class intervals: 1.96 is a two sided 95 percent interval.
WILSON_Z = 1.96
# Each tail of the exact interval that decides a class under --min-class-rows.
EXACT_TAIL = 0.025

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Verdicts, worst first.
REJECT = "REJECT"
SUSPECT = "SUSPECT"
TRUST = "TRUST"

_RANK = {REJECT: 0, SUSPECT: 1, TRUST: 2}

# Profiles. reject_types are the match types that mean "I matched an area, not
# an address". They are rejected at any score, which is the whole point of the
# tool: the geocoder is not wrong about them. It is telling you plainly that it
# fell back, and the score then describes how well it matched the ZIP code.
PROFILES = {
    "esri": {
        "fields": {
            "score": "Score",
            "match_type": "Addr_type",
            "x": "X",
            "y": "Y",
            "matched_addr": "Match_addr",
            "in_addr": "USER_address",
            "status": "Status",
        },
        "score_max": 100.0,
        "score_optional": False,
        # Esri's Status column is the geocoder's own verdict: M matched, T tied,
        # U unmatched. A tie is a candidate picked arbitrarily from several
        # with the same best score, so it is refused exactly as the census
        # profile refuses its Tie match type.
        "status_reject": {
            "T": "status T is a tie: the geocoder picked one of several "
                 "equally good candidates, so the point may be the wrong one",
            "U": "status U means the geocoder matched nothing",
        },
        "trust_types": ("pointaddress", "subaddress", "streetaddress",
                        "streetaddressext", "buildingname", "parcel"),
        "suspect_types": ("streetname", "streetint", "streetmidpoint",
                          "distancemarker", "poi"),
        "reject_types": ("postal", "postalext", "postalloc", "zip5", "zip4",
                         "locality", "city", "county", "subregion", "region",
                         "state", "country", "admin", "block", "sector"),
    },
    # The Census batch geocoder returns no score at all, only Exact or
    # Non_Exact, so the match type carries the entire decision. Its own file
    # does not fit this map: it has no header, puts "lon,lat" in one cell, and
    # splits Match/Tie/No_Match from Exact/Non_Exact across two columns. A
    # result must be reshaped into these columns, with one match_type column
    # holding Exact, Non_Exact, Tie or No_Match, before it is audited.
    "census": {
        "fields": {
            "score": "",
            "match_type": "match_type",
            "x": "lon",
            "y": "lat",
            "matched_addr": "matched_address",
            "in_addr": "input_address",
            "status": "",
        },
        "score_max": 100.0,
        "score_optional": True,
        "status_reject": {},
        "trust_types": ("exact",),
        "suspect_types": ("non_exact", "nonexact"),
        "reject_types": ("tie", "no_match", "nomatch"),
    },
    # Nominatim's importance is a prominence measure, not a match confidence: a
    # famous city outranks a correct house. The floors are set low on purpose
    # and the match type decides. Do not read importance as an accuracy score.
    "nominatim": {
        "fields": {
            "score": "importance",
            "match_type": "addresstype",
            "x": "lon",
            "y": "lat",
            "matched_addr": "display_name",
            "in_addr": "query",
            "status": "",
        },
        "score_max": 1.0,
        "score_optional": False,
        "status_reject": {},
        "trust_types": ("house", "building", "address", "place_house"),
        "suspect_types": ("road", "street", "amenity", "shop", "leisure",
                          "tourism", "railway"),
        "reject_types": ("postcode", "postal_code", "city", "town", "village",
                         "hamlet", "suburb", "neighbourhood", "quarter",
                         "county", "state", "province", "country",
                         "administrative", "municipality", "borough"),
        "trust_score": 20.0,
        "suspect_score": 10.0,
    },
}

DEFAULT_PROFILE = "esri"


class RowResult(object):
    """One audited row: the verdict and every reason behind it."""

    def __init__(self, index, verdict, reasons, x, y):
        self.index = index
        self.verdict = verdict
        self.reasons = reasons
        self.x = x
        self.y = y

    def __repr__(self):
        return "RowResult(%d, %s, %r)" % (self.index, self.verdict, self.reasons)


class Report(object):
    """The audited batch: counts, pile-ups and the TRUST rate."""

    def __init__(self, results, pileups):
        self.results = results
        self.pileups = pileups
        self.counts = {TRUST: 0, SUSPECT: 0, REJECT: 0}
        for r in results:
            self.counts[r.verdict] += 1

    @property
    def total(self):
        return len(self.results)

    @property
    def trust_rate(self):
        """Fraction of rows that came back TRUST.

        An empty batch reports 0.0, not 1.0. An empty CSV means the upstream
        job broke, and a vacuous perfect rate would wave it through the gate.
        """
        if not self.results:
            return 0.0
        return self.counts[TRUST] / float(self.total)


# ----------------------------------------------------------------- pure core

def _is_finite(value):
    """True for a real number. NaN and infinity are not numbers this can read.

    NaN is the dangerous one. Every comparison against NaN is False, so a NaN
    score slips under both floors and comes out TRUST, and a NaN coordinate
    never equals another NaN, so eight stacked rows count as eight separate
    points and the pile-up detector goes quiet. Anything that is not a number
    reads as unusable rather than raising, because a CSV cell holds anything.
    """
    try:
        return not (math.isnan(value) or math.isinf(value))
    except TypeError:
        return False


def worse(a, b):
    """The more severe of two verdicts."""
    return a if _RANK[a] <= _RANK[b] else b


def score_percent(value, profile=DEFAULT_PROFILE):
    """A score, or a floor on the score's own scale, as a percent of that scale.

    classify() compares percents, so that one default floor fits an Esri score
    of 0 to 100 and a Nominatim importance of 0 to 1. A floor given on the
    command line goes through this same arithmetic as the score it is compared
    with, so a row exactly at the floor still lands on it.
    """
    return 100.0 * float(value) / PROFILES[profile]["score_max"]


def classify(score, match_type, profile=DEFAULT_PROFILE, trust_score=None,
             suspect_score=None, status=None):
    """Classify one row from its score and its match type together.

    Score alone is the check everybody writes, and it is the one that publishes
    the wrong map. A ZIP centroid fallback scores in the high nineties because
    the geocoder matched the ZIP code well. The match type is the field that
    says what was matched, so it is read first and it overrides the score.
    A status the profile refuses, such as an Esri tie, overrides both.

    trust_score and suspect_score are percents of the profile's score scale;
    score_percent() converts a floor from the score's own units.

    A bad floor raises, because it is the caller's mistake. A bad score is a
    cell in somebody's CSV, so it is a refusal of that row, never an error
    that stops the batch.
    """
    if profile not in PROFILES:
        raise ValueError("unknown profile %r, expected one of %s"
                         % (profile, ", ".join(sorted(PROFILES))))
    prof = PROFILES[profile]
    if trust_score is None:
        trust_score = prof.get("trust_score", DEFAULT_TRUST_SCORE)
    if suspect_score is None:
        suspect_score = prof.get("suspect_score", DEFAULT_SUSPECT_SCORE)
    # NaN first. Every comparison against a NaN floor is False, so a NaN trust
    # floor waved every row at or above the reject floor through as TRUST.
    if not (_is_finite(trust_score) and _is_finite(suspect_score)):
        raise ValueError("score floors must be real numbers, got %r and %r"
                         % (trust_score, suspect_score))
    if trust_score < 0 or suspect_score < 0:
        raise ValueError("score floors cannot be negative")
    if suspect_score > trust_score:
        # On the score's own scale, which is the scale the flags take.
        raise ValueError("the reject floor %.4g is above the trust floor %.4g"
                         % (suspect_score * prof["score_max"] / 100.0,
                            trust_score * prof["score_max"] / 100.0))
    if score is not None and not _is_finite(score):
        raise ValueError("score must be a real number, got %r" % (score,))

    code = (status or "").strip().upper()
    if code in prof["status_reject"]:
        return REJECT, prof["status_reject"][code]

    if score is not None and score < 0:
        # A cell, not a caller's mistake. Raising here stopped the whole batch
        # with a usage error and audited none of the other rows.
        return REJECT, "score %.4g is negative, which no geocoder writes" % score

    mt = (match_type or "").strip().lower()
    pct = None if score is None else score_percent(score, profile)

    if mt in prof["reject_types"]:
        if score is None:
            # The Census profile carries no score, so the sentence below has
            # nothing to put in it. Reading "The score is absent says how well
            # that AREA matched" is worse than saying less.
            return REJECT, ("match type %r is not an address-level match"
                            % (match_type,))
        return REJECT, ("match type %r is an area fallback, not an address. "
                        "The score %.4g says how well that AREA matched."
                        % (match_type, score))

    if pct is not None and pct < suspect_score:
        return REJECT, ("score %.4g is below the reject floor of %.4g"
                        % (score, suspect_score * prof["score_max"] / 100.0))

    if not mt:
        return SUSPECT, "no match type reported, so the score cannot be read"

    known = (tuple(prof["trust_types"]) + tuple(prof["suspect_types"])
             + tuple(prof["reject_types"]))
    if mt not in known:
        return SUSPECT, ("match type %r is not known to the %s profile"
                         % (match_type, profile))

    if mt in prof["suspect_types"]:
        return SUSPECT, "match type %r is not a house-level match" % (match_type,)

    if pct is None:
        if prof["score_optional"]:
            return TRUST, "match type %r at the address level" % (match_type,)
        return SUSPECT, "no score reported"

    if pct < trust_score:
        return SUSPECT, ("score %.4g is under the trust floor of %.4g"
                         % (score, trust_score * prof["score_max"] / 100.0))

    return TRUST, "match type %r at score %.4g" % (match_type, score)


def house_number(address):
    """Leading house number of an address, or None.

    Only the leading run of digits counts. A trailing unit ("123A"), a
    fractional number ("123 1/2") and a hyphenated Queens-style number
    ("123-45") all reduce to the first integer, which is what a block level
    comparison needs.
    """
    if not address:
        return None
    parts = address.strip().split()
    token = parts[0] if parts else ""
    digits = ""
    for ch in token:
        # Not str.isdigit. That is True for a superscript two, where int()
        # then raises, and True for the Arabic-Indic numerals, where int()
        # reads a number no US address ever had. A house number is ASCII
        # digits or it is not a house number.
        if "0" <= ch <= "9":
            digits += ch
        else:
            break
    # Ten digits or more is not a house number. int() refuses a run over
    # 4300 digits on Python 3.11 and later, and patched 3.9 and 3.10 too, so
    # one such cell raised ValueError and stopped the audit of the batch.
    if not digits or len(digits) > 9:
        return None
    return int(digits)


def house_number_problem(in_addr, matched_addr,
                         tolerance=DEFAULT_HOUSE_TOLERANCE):
    """Message when the matched house number is far from the input, else None.

    Silent when either side has no house number. A missing number is normal for
    an intersection or a place name, and the match type already reports that.
    Inventing a second failure here would count the same defect twice.
    """
    if tolerance < 0:
        raise ValueError("house number tolerance cannot be negative")
    a = house_number(in_addr)
    b = house_number(matched_addr)
    if a is None or b is None:
        return None
    if abs(a - b) <= tolerance:
        return None
    return "house number %d was matched to %d, %d apart" % (a, b, abs(a - b))


def pileup_key(x, y, precision=DEFAULT_PILEUP_PRECISION):
    """Rounded coordinate that two records must share to count as stacked."""
    return (round(float(x), precision), round(float(y), precision))


def find_pileups(coords, threshold=DEFAULT_PILEUP_THRESHOLD,
                 precision=DEFAULT_PILEUP_PRECISION):
    """Coordinates shared by at least `threshold` records, worst first.

    This is the check that catches the disaster, and it cannot be done per row.
    Every record in a centroid stack looks correct on its own line.
    """
    if threshold < 2:
        raise ValueError("the pile-up threshold must be at least 2")
    counts = {}
    for x, y in coords:
        key = pileup_key(x, y, precision)
        counts[key] = counts.get(key, 0) + 1
    hits = [(k, n) for k, n in counts.items() if n >= threshold]
    hits.sort(key=lambda kn: (-kn[1], kn[0]))
    return hits


def _on_segment(px, py, ax, ay, bx, by, eps=1e-12):
    """True when the point lies on the segment AB, within eps."""
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > eps:
        return False
    return (min(ax, bx) - eps <= px <= max(ax, bx) + eps
            and min(ay, by) - eps <= py <= max(ay, by) + eps)


def point_in_ring(x, y, ring):
    """Ray casting against one ring. A point on the ring counts as inside.

    The half-open rule `(ay > y) != (by > y)` is what makes a vertex on the
    test ray count once instead of twice, and it drops horizontal edges
    entirely rather than dividing by zero on them. Both cases turn up on a real
    county boundary, which is full of axis-aligned section lines.
    """
    n = len(ring)
    if n < 3:
        raise ValueError("a ring needs at least 3 points, got %d" % n)
    inside = False
    for i in range(n):
        ax, ay = ring[i][0], ring[i][1]
        bx, by = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if _on_segment(x, y, ax, ay, bx, by):
            return True
        if (ay > y) != (by > y):
            xint = ax + (y - ay) * (bx - ax) / float(by - ay)
            if x < xint:
                inside = not inside
    return inside


def point_in_polygon(x, y, rings):
    """Point in a GeoJSON polygon: ring 0 is the outside, the rest are holes.

    The edge of a hole is still the edge of the polygon, so every ring is
    tested for the point before any hole is tested for containment. Without
    that, a parcel on the shore of a lake would read as outside the county.
    """
    if not rings:
        raise ValueError("a polygon needs at least one ring")
    for ring in rings:
        n = len(ring)
        for i in range(n):
            ax, ay = ring[i][0], ring[i][1]
            bx, by = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if _on_segment(x, y, ax, ay, bx, by):
                return True
    if not point_in_ring(x, y, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(x, y, hole):
            return False
    return True


def point_in_any(x, y, polygons):
    """True when the point falls in any polygon of the boundary."""
    for rings in polygons:
        if point_in_polygon(x, y, rings):
            return True
    return False


def _polygon(rings):
    """The rings of one GeoJSON Polygon, checked, or ValueError.

    Checked here, while the file is read, so that a malformed boundary is an
    input that could not be read (exit 2). Unchecked, a ring of strings or a
    missing coordinates key raised TypeError or KeyError out of the audit,
    which exited 1, the code a scheduled job reads as "the batch failed".
    """
    if not isinstance(rings, (list, tuple)) or not rings:
        # Name the type, never the value: a wrong --boundary file can be a
        # credentials JSON, and this message goes to stderr, the job's log.
        raise ValueError("a GeoJSON Polygon needs a list of rings, got %s"
                         % type(rings).__name__)
    for ring in rings:
        if (not isinstance(ring, (list, tuple)) or len(ring) < 3
                or not all(_position(p) for p in ring)):
            raise ValueError("a GeoJSON ring needs at least 3 positions of "
                             "two real numbers each")
    return rings


def _position(point):
    """True for a GeoJSON position: a list of at least two real numbers."""
    return (isinstance(point, (list, tuple)) and len(point) >= 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and _is_finite(v) for v in point[:2]))


def polygons_from_geojson(obj):
    """Every Polygon and MultiPolygon in a GeoJSON object, as ring lists.

    Accepts a FeatureCollection, a Feature, a GeometryCollection or a bare
    geometry, because county boundaries arrive as all four and re-exporting
    one is not the operator's job. Anything malformed raises ValueError.
    """
    if not isinstance(obj, dict):
        raise ValueError("expected a GeoJSON object, got %s"
                         % type(obj).__name__)
    kind = obj.get("type")
    if kind == "FeatureCollection":
        out = []
        for feat in _members(obj, "features"):
            out.extend(polygons_from_geojson(feat))
        return out
    if kind == "Feature":
        # An exported county boundary routinely carries one feature with a
        # null geometry. Refusing the whole file over it helps nobody.
        geom = obj.get("geometry")
        return polygons_from_geojson(geom) if geom else []
    if kind == "GeometryCollection":
        out = []
        for geom in _members(obj, "geometries"):
            out.extend(polygons_from_geojson(geom))
        return out
    if kind == "Polygon":
        return [_polygon(obj.get("coordinates"))]
    if kind == "MultiPolygon":
        return [_polygon(rings) for rings in _members(obj, "coordinates")]
    raise ValueError("no polygon found in GeoJSON of type %r" % (kind,))


def _members(obj, key):
    """The list under key, [] when it is absent or null, else ValueError."""
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("GeoJSON %r must be a list, got %s"
                         % (key, type(value).__name__))
    return value


def to_number(text):
    """A float from a CSV cell, or None when the cell is blank or not a number.

    "nan", "inf" and an overflowing literal such as 1e400 are cells, not
    numbers. float() accepts all three, and a NaN score then clears every
    floor in classify(), because every comparison against NaN is False. They
    read as absent instead, which is honest and is already handled.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        value = float(text)
        return value if _is_finite(value) else None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if _is_finite(value) else None


# The columns a verdict cannot be reached without. The rest refine it.
REQUIRED_FIELDS = ("score", "match_type", "x", "y")

# The check each optional column feeds, named in the note when it is absent.
OPTIONAL_CHECKS = {
    "in_addr": "the house-number check is skipped",
    "matched_addr": "the house-number check is skipped",
    "status": "a tied match cannot be caught",
}

# The two columns --out adds to a copy of the input.
ADDED_COLUMNS = ("gcs_verdict", "gcs_reasons")


def missing_columns(fields, header):
    """Split absent columns into the ones that break the audit and the rest.

    Reporting both as one "warning" made a valid Esri export, which simply has
    no USER_address column, look broken. The optional ones come back as
    (column, the check its absence skips) pairs.
    """
    absent = [key for key, name in fields.items() if name and name not in header]
    return {
        "required": sorted(fields[k] for k in absent if k in REQUIRED_FIELDS),
        "optional": sorted((fields[k], OPTIONAL_CHECKS[k]) for k in absent
                           if k not in REQUIRED_FIELDS),
    }


def ambiguous_columns(fields, header):
    """Columns this reads that the header names more than once.

    A repeated name cannot be read by name: the last one silently won, and
    the operator had no way to know which score column was audited.
    """
    return sorted(set(name for name in fields.values()
                      if name and header.count(name) > 1))


def fields_for(profile, overrides=None):
    """Column names for a profile, with the explicit overrides applied."""
    if profile not in PROFILES:
        raise ValueError("unknown profile %r" % (profile,))
    fields = dict(PROFILES[profile]["fields"])
    for key, name in (overrides or {}).items():
        if key not in fields:
            raise ValueError("unknown field %r" % (key,))
        if name:
            fields[key] = name
    return fields


def extract_row(raw, fields):
    """Pull the seven values this tool reasons about out of one CSV row."""
    def cell(key):
        name = fields.get(key)
        if not name:
            return None
        value = raw.get(name)
        return value if value is None else str(value)

    return {
        "score": to_number(cell("score")),
        "match_type": cell("match_type"),
        "x": to_number(cell("x")),
        "y": to_number(cell("y")),
        "in_addr": cell("in_addr"),
        "matched_addr": cell("matched_addr"),
        "status": cell("status"),
    }


def audit(rows, profile=DEFAULT_PROFILE, trust_score=None, suspect_score=None,
          polygons=None, pileup_threshold=DEFAULT_PILEUP_THRESHOLD,
          pileup_precision=DEFAULT_PILEUP_PRECISION,
          house_tolerance=DEFAULT_HOUSE_TOLERANCE):
    """Audit extracted rows and return a Report. No file, no network.

    An empty polygon list raises. An empty list is falsy, so a boundary file
    that parsed to no polygon at all would otherwise skip the containment
    check in silence: the check failing open on the one input that most needs
    an answer.
    """
    if polygons is not None and not polygons:
        raise ValueError("the boundary holds no polygon, so containment "
                         "cannot be checked. Refusing rather than skipping it.")
    results = []
    for i, row in enumerate(rows):
        verdict, reason = classify(row.get("score"), row.get("match_type"),
                                   profile, trust_score, suspect_score,
                                   row.get("status"))
        reasons = [reason]

        x, y = row.get("x"), row.get("y")
        if not _is_finite(x) or not _is_finite(y):
            verdict = worse(verdict, REJECT)
            reasons.append("no usable coordinate on the row")
        elif polygons and not point_in_any(x, y, polygons):
            verdict = worse(verdict, REJECT)
            reasons.append("coordinate %r, %r falls outside the boundary"
                           % (x, y))

        problem = house_number_problem(row.get("in_addr"),
                                       row.get("matched_addr"),
                                       house_tolerance)
        if problem:
            verdict = worse(verdict, SUSPECT)
            reasons.append(problem)

        results.append(RowResult(i, verdict, reasons, x, y))

    coords = [(r.x, r.y) for r in results
              if _is_finite(r.x) and _is_finite(r.y)]
    pileups = find_pileups(coords, pileup_threshold, pileup_precision)
    stacked = dict(pileups)
    for r in results:
        if not _is_finite(r.x) or not _is_finite(r.y):
            continue
        key = pileup_key(r.x, r.y, pileup_precision)
        if key in stacked:
            # The pile-up is the failure no per-row field can show. A centroid
            # stack is all high scores, so this demotion is the only thing that
            # moves those rows out of the TRUST count and drops the rate.
            r.verdict = worse(r.verdict, SUSPECT)
            r.reasons.append("shares coordinate %r with %d record(s)"
                             % (key, stacked[key]))
    return Report(results, pileups)


def gate(report, min_trust_rate=DEFAULT_MIN_TRUST_RATE):
    """Exit code for a report: 0 when the TRUST rate clears the floor, else 1.

    A batch with no rows fails whatever the floor is. At a floor of 0 the
    rate test alone passed it, as 0.0 >= 0.0, and an empty batch means the
    upstream job broke.
    """
    if not 0.0 <= min_trust_rate <= 1.0:
        raise ValueError("the trust rate floor must be between 0 and 1")
    if not report.total:
        return 1
    return 0 if report.trust_rate >= min_trust_rate else 1


def describe(report, min_trust_rate=DEFAULT_MIN_TRUST_RATE, sample=5,
             numbers=None):
    """Render a report as the lines the CLI prints."""
    out = ["rows audited: %d" % report.total]
    for verdict in (TRUST, SUSPECT, REJECT):
        out.append("  %-8s %6d" % (verdict, report.counts[verdict]))
    out.append("TRUST rate: %.1f%% (floor %.1f%%)"
               % (report.trust_rate * 100.0, min_trust_rate * 100.0))

    if report.pileups:
        out.append("")
        out.append("coordinate pile-ups:")
        for (x, y), n in report.pileups[:sample]:
            out.append("  %d record(s) on %r, %r" % (n, x, y))
        if len(report.pileups) > sample:
            out.append("  ...and %d more" % (len(report.pileups) - sample))

    bad = [r for r in report.results if r.verdict != TRUST]
    if bad:
        out.append("")
        out.append("first %d row(s) that are not TRUST:" % min(sample, len(bad)))
        for r in bad[:sample]:
            # numbers holds the row each result came from, as read_csv()
            # counts it, blank lines included. Without it, +2 turns a 0-based
            # index into the row number under a header.
            out.append("  row %d %s: %s"
                       % (numbers[r.index] if numbers else r.index + 2,
                          r.verdict, "; ".join(r.reasons)))
    return out


# ------------------------------------------------------------ address classes
#
# The aggregate TRUST rate is one number over every kind of address, and a
# geocoder can fail one kind completely while the number stays healthy. A
# geocoder that matched 99 percent of plain street addresses and 12 percent of
# directional ones reads as a good geocoder on any batch where the directional
# addresses are a minority. Sorting the rows by address class and judging each
# class on its own is what shows it.
#
# The classifier below is deterministic and reads only the submitted address.
# It is a set of plain token rules, written out in address_classes(), so that a
# reader can predict the class of any address without running it.

# Directional words, both before the street name ("N MAIN ST") and after the
# suffix ("MAIN ST NW").
DIRECTIONALS = ("N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH",
                "EAST", "WEST", "NORTHEAST", "NORTHWEST", "SOUTHEAST",
                "SOUTHWEST")

# Common street suffixes from USPS Publication 28, spelled out and abbreviated,
# mapped to the standard abbreviation. Not the whole appendix. Words that are
# also common street or city names, such as PARK, HILL and LAKE, are left out
# on purpose, because the classifier cannot tell the two apart.
SUFFIXES = {
    "ALLEY": "ALY", "ALY": "ALY", "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "CIRCLE": "CIR", "CIR": "CIR",
    "COURT": "CT", "CT": "CT", "COVE": "CV", "CV": "CV", "CROSSING": "XING",
    "XING": "XING", "DRIVE": "DR", "DR": "DR", "HIGHWAY": "HWY", "HWY": "HWY",
    "LANE": "LN", "LN": "LN", "LOOP": "LOOP", "PASS": "PASS", "PATH": "PATH",
    "PARKWAY": "PKWY", "PKWY": "PKWY", "PIKE": "PIKE", "PLACE": "PL",
    "PL": "PL", "ROAD": "RD", "RD": "RD", "RUN": "RUN", "SQUARE": "SQ",
    "SQ": "SQ", "STREET": "ST", "ST": "ST", "TERRACE": "TER", "TER": "TER",
    "TRACE": "TRCE", "TRCE": "TRCE", "TRAIL": "TRL", "TRL": "TRL",
    "WAY": "WAY",
}

# A route street is a lead word, optionally more route words, then a number:
# "COUNTY ROAD 900", "US HWY 999", "SR 123", "I 999".
ROUTE_LEADS = ("CR", "SR", "US", "HWY", "HIGHWAY", "COUNTY", "STATE", "ROUTE",
               "RTE", "INTERSTATE", "I")
ROUTE_WORDS = ROUTE_LEADS + ("ROAD", "RD")

# Unit designators. FL is left out: it is a floor to some writers and a state
# abbreviation at the end of many more addresses.
UNIT_WORDS = ("#", "APT", "APARTMENT", "UNIT", "STE", "SUITE", "LOT", "BLDG",
              "BUILDING", "RM", "ROOM", "TRLR", "TRAILER", "SPC", "SPACE")

# Spelled ordinals. The first nine come first, because a compound such as
# TWENTY-FIRST or TWENTY FIRST is a tens word and one of them.
SPELLED_ORDINALS = ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH",
                    "SEVENTH", "EIGHTH", "NINTH", "TENTH", "ELEVENTH",
                    "TWELFTH", "THIRTEENTH", "FOURTEENTH", "FIFTEENTH",
                    "SIXTEENTH", "SEVENTEENTH", "EIGHTEENTH", "NINETEENTH",
                    "TWENTIETH", "THIRTIETH", "FORTIETH", "FIFTIETH",
                    "SIXTIETH", "SEVENTIETH", "EIGHTIETH", "NINETIETH",
                    "HUNDREDTH")
TENS = ("TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY",
        "NINETY")

# The order the class table prints in. Suffix classes follow, largest first.
CLASS_ORDER = ("all", "directional", "numbered_street", "route", "unit",
               "po_box", "rural_route", "no_number", "plain", "blank")


def _ascii_digits(text):
    """True for a non-empty run of ASCII digits, and for nothing else."""
    return bool(text) and all("0" <= ch <= "9" for ch in text)


def address_tokens(address):
    """The upper case tokens of the street line of an address.

    The street line is the text before the first comma, which drops the city,
    state and ZIP of a single-line address. A second part that starts with a
    unit word is kept, so "100 MAIN ST, APT 4" is still a unit. Full stops are
    dropped, which turns "P.O." into "PO", and "#" becomes a token of its own,
    so "#4" and "# 4" read the same.
    """
    if not address:
        return []
    parts = address.upper().replace(".", "").replace("#", " # ").split(",")
    tokens = parts[0].split()
    if len(parts) > 1:
        second = parts[1].split()
        if second and second[0] in UNIT_WORDS:
            tokens.extend(second)
    return tokens


def _route_number(tokens, i):
    """Index of the number of a route street such as "CR 900" at i, or -1."""
    if i >= len(tokens) or tokens[i] not in ROUTE_LEADS:
        return -1
    j = i
    while j < len(tokens) and tokens[j] in ROUTE_WORDS:
        j += 1
    if j < len(tokens) and house_number(tokens[j]) is not None:
        return j
    return -1


def _is_ordinal(token, following):
    """True for a numbered street name: 31ST, 2ND, SECOND, or 22 before ST.

    A spelled compound counts written as one word, TWENTY-FIRST, or as two,
    TWENTY FIRST, where the second word is the one that follows.
    """
    if token in SPELLED_ORDINALS:
        return True
    tens, _, unit = token.partition("-")
    if tens in TENS and (unit or following) in SPELLED_ORDINALS[:9]:
        return True
    if token[-2:] in ("ST", "ND", "RD", "TH") and _ascii_digits(token[:-2]):
        return True
    return _ascii_digits(token) and following in SUFFIXES


def address_classes(address):
    """Every class the submitted address belongs to, in a fixed order.

    The rules, applied to the tokens of address_tokens():

    - blank: no token at all.
    - po_box: starts PO BOX, P O BOX or POST OFFICE BOX. Nothing else applies.
    - rural_route: starts RR, HC, HCR, RURAL ROUTE or HIGHWAY CONTRACT.
      Nothing else applies.
    - no_number: the first token has no leading house number. A fraction such
      as 1/2 after the house number is skipped.
    - directional: the next token is a directional, with a token after it.
      One exception: when the token after the directional is a suffix word
      that ends the street, with nothing after it but a directional or a
      unit word, the directional is the street's name and the suffix word
      stays the suffix. "N MAIN ST" and "N COURT ST" are directional, "NORTH ST"
      is a street called North. "N HWY 999" is directional, because the
      route number follows HWY. A directional straight after the last suffix
      counts too.
    - route: the street is a route lead, route words, then a number. A
      directional straight after the route number counts as directional.
    - numbered_street: the street name is an ordinal, or a bare number
      followed by a suffix. An ordinal is a number ending ST, ND, RD or TH
      (31ST), or a spelled one from FIRST to NINETEENTH, a tens word such
      as TWENTIETH, HUNDREDTH, or a tens word and FIRST to NINTH, joined by
      a hyphen or a space (TWENTY-FIRST).
    - unit: a unit word after the first token of the street name, with a token
      after it.
    - plain: a house number and none of directional, route, numbered_street
      or unit.
    - suffix:XX: the last suffix before any unit word, as its USPS
      abbreviation. The first token of the street name is never the suffix,
      so "ST JOHNS AVE" is an avenue and "100 COURT" has none. suffix:none
      when there is none. A route street has no suffix class.

    A row belongs to every class that applies, so a class table counts it
    once in each and its rows do not sum to the total.
    """
    t = address_tokens(address)
    n = len(t)
    if not n:
        return ["blank"]
    if t[:2] == ["PO", "BOX"] or t[:3] in (["P", "O", "BOX"],
                                          ["POST", "OFFICE", "BOX"]):
        return ["po_box"]
    if t[0] in ("RR", "HC", "HCR") or t[:2] in (["RURAL", "ROUTE"],
                                                ["HIGHWAY", "CONTRACT"]):
        return ["rural_route"]

    found = set()
    i = 0
    if house_number(t[0]) is None:
        found.add("no_number")
    else:
        i = 1
        if i < n and "/" in t[i] and _ascii_digits(t[i].replace("/", "")):
            i += 1
    if i + 1 < n and t[i] in DIRECTIONALS:
        # "100 NORTH ST" is a street called North, but "100 N COURT ST" is
        # North Court Street: the directional is the name only when the
        # suffix word after it ends the street. Reading every suffix word as the name moved directional
        # rows into plain, where a failing geocoder hides.
        rest = t[i + 2:]
        named = (t[i + 1] in SUFFIXES
                 and (not rest or rest[0] in DIRECTIONALS
                      or rest[0] in UNIT_WORDS))
        if not named:
            found.add("directional")
            i += 1

    route = _route_number(t, i)
    if route >= 0:
        found.add("route")
        # "CR 900 N": a directional after the route number is a directional.
        if route + 1 < n and t[route + 1] in DIRECTIONALS:
            found.add("directional")
    elif i < n and _is_ordinal(t[i], t[i + 1] if i + 1 < n else None):
        found.add("numbered_street")

    end = n
    for k in range(i + 1, n - 1):
        if t[k] in UNIT_WORDS:
            found.add("unit")
            end = k
            break

    suffix = None
    if route < 0:
        suffix = "none"
        last = None
        for k in range(i + 1, end):
            if t[k] in SUFFIXES:
                suffix = SUFFIXES[t[k]]
                last = k
        if last is not None and last + 1 < end and t[last + 1] in DIRECTIONALS:
            found.add("directional")

    if "no_number" not in found and not found & set(
            ("directional", "route", "numbered_street", "unit")):
        found.add("plain")
    out = [c for c in CLASS_ORDER if c in found]
    if suffix:
        out.append("suffix:" + suffix)
    return out


def wilson_interval(successes, rows, z=WILSON_Z):
    """The Wilson score interval of a proportion, as (low, high).

    Not the normal approximation. That one reports 0 of 10 as exactly 0 with
    no uncertainty at all, and runs past 0 and 1 near the ends, which is where
    a failing address class lives. The Wilson interval of 0 of 10 runs to 28
    percent, which is the honest answer. The ends are exact rather than
    computed, so a class that matched nothing reports a low of 0.0 and not a
    rounding error.
    """
    _interval_counts(successes, rows)
    p = successes / float(rows)
    z2 = z * z
    denom = 1.0 + z2 / rows
    centre = (p + z2 / (2.0 * rows)) / denom
    half = z * math.sqrt(p * (1.0 - p) / rows + z2 / (4.0 * rows * rows)) / denom
    low = 0.0 if successes == 0 else centre - half
    high = 1.0 if successes == rows else centre + half
    return low, high


def _interval_counts(successes, rows):
    """ValueError unless successes of rows can make an interval."""
    if rows <= 0:
        raise ValueError("an interval needs at least one row")
    if not 0 <= successes <= rows:
        raise ValueError("successes %r are not between 0 and %r"
                         % (successes, rows))


def exact_interval(successes, rows, tail=EXACT_TAIL):
    """The exact (Clopper-Pearson) interval of a proportion, as (low, high).

    Each bound is the proportion at which the observed count sits exactly
    tail into its binomial tail, found by bisecting the binomial CDF. The
    Wilson interval is too narrow at the ends of a small class: it puts 0 of
    1 under 79.3 percent, where the exact bound is 97.5 percent. A class
    judged by the Wilson bound let one missed row fail a 99.9 percent batch.
    """
    _interval_counts(successes, rows)
    # ponytail: O(rows) per CDF step, 60 steps per bound. Only a class under
    # --min-class-rows reaches this, so rows stays small at the default of 30.
    log_n = math.lgamma(rows + 1)

    def cdf(k, p):
        return sum(math.exp(log_n - math.lgamma(i + 1)
                            - math.lgamma(rows - i + 1)
                            + i * math.log(p) + (rows - i) * math.log1p(-p))
                   for i in range(k + 1))

    def solve(k, target):
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if cdf(k, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    low = 0.0 if successes == 0 else solve(successes - 1, 1.0 - tail)
    high = 1.0 if successes == rows else solve(successes, tail)
    return low, high


class ClassRate(object):
    """One address class of one batch: its rows and how many came back TRUST."""

    def __init__(self, name, rows=0, trusted=0):
        self.name = name
        self.rows = rows
        self.trusted = trusted

    @property
    def rate(self):
        return self.trusted / float(self.rows) if self.rows else 0.0

    def status(self, floor, min_rows=DEFAULT_MIN_CLASS_ROWS):
        """ok, BELOW, or a refusal when the class has too few rows to judge.

        From min_rows up, the rate decides. Under it, the exact interval
        decides, and only when it lies wholly on one side of the floor. 2 of 3
        is 67 percent, but its interval reaches 99 percent, so it says nothing
        about a 90 percent floor and is refused. 2 of 20 reaches only 32
        percent. Refusing it passed the very class the gate exists to catch.
        0 of 1 reaches 97.5 percent and is refused. The Wilson bound of 79
        percent failed a whole batch on one row.
        """
        if self.rows >= min_rows:
            return "BELOW" if self.rate < floor else "ok"
        if self.rows:
            low, high = exact_interval(self.trusted, self.rows)
            if high < floor:
                return "BELOW"
            if low >= floor:
                return "ok"
        return "too few rows"

    def __repr__(self):
        return "ClassRate(%r, %d, %d)" % (self.name, self.rows, self.trusted)


def class_table(addresses, results):
    """ClassRates for an audited batch: "all", then each class in print order.

    addresses[i] is the submitted address of results[i]. A row counts as
    matched when its verdict is TRUST, the same test the aggregate rate uses,
    so a class cannot pass on rows the audit already refused.
    """
    if len(addresses) != len(results):
        raise ValueError("%d addresses for %d audited rows"
                         % (len(addresses), len(results)))
    table = {"all": ClassRate("all")}
    for address, result in zip(addresses, results):
        hit = 1 if result.verdict == TRUST else 0
        for name in ["all"] + address_classes(address):
            rate = table.setdefault(name, ClassRate(name))
            rate.rows += 1
            rate.trusted += hit
    head = [table[c] for c in CLASS_ORDER if c in table]
    tail = sorted((r for r in table.values() if r.name not in CLASS_ORDER),
                  key=lambda r: (-r.rows, r.name))
    return head + tail


def class_failures(table, floor, min_rows=DEFAULT_MIN_CLASS_ROWS):
    """The classes, "all" excepted, that ClassRate.status() finds BELOW floor.

    "all" is left to gate(), which already judges the aggregate. Counting it
    here as well would report one failure twice.
    """
    if not 0.0 <= floor <= 1.0:
        raise ValueError("the class rate floor must be between 0 and 1")
    if min_rows < 1:
        raise ValueError("a class needs at least 1 row to be judged")
    return [r for r in table
            if r.name != "all" and r.status(floor, min_rows) == "BELOW"]


def _rate_cells(rate, min_rows):
    """The rows, TRUST, rate and interval cells of one ClassRate.

    Under min_rows the rate is a dash but the interval is printed, because
    the interval is what judged the class.
    """
    if rate is None or not rate.rows:
        return "%5d %6s %7s  %-15s" % (0, "-", "-", "-")
    low, high = wilson_interval(rate.trusted, rate.rows)
    shown = "-" if rate.rows < min_rows else "%6.1f%%" % (rate.rate * 100.0)
    return ("%5d %6d %7s  %5.1f%% - %5.1f%%"
            % (rate.rows, rate.trusted, shown, low * 100.0, high * 100.0))


def describe_classes(table, floor, min_rows=DEFAULT_MIN_CLASS_ROWS,
                     control=None):
    """Render a class table, and a control file's table beside it, as lines.

    A class under min_rows prints its counts, a dash for the rate and its
    interval. Printing 67 percent for 2 of 3 would invite exactly the reading
    the refusal exists to stop. The gap is the control's rate minus this file's, in points, and
    is printed only when both sides have enough rows to be judged.
    """
    out = ["address classes (TRUST rate by class, floor %.1f%%, "
           "%d rows to judge a class):" % (floor * 100.0, min_rows)]
    head = "  %-16s %5s %6s %7s  %-15s  %-12s" % (
        "class", "rows", "TRUST", "rate", "95% interval", "verdict")
    if control is not None:
        head += " | %5s %6s %7s  %-15s  %s" % (
            "rows", "TRUST", "rate", "95% interval", "gap")
    out.append(head.rstrip())
    mine = dict((r.name, r) for r in table)
    others = dict((r.name, r) for r in control or [])
    names = [r.name for r in table]
    names += [r.name for r in control or [] if r.name not in mine]
    for name in names:
        rate = mine.get(name)
        if name == "all":
            verdict = "aggregate"
        elif rate is None:
            verdict = "absent"
        else:
            verdict = rate.status(floor, min_rows)
        line = "  %-16s %s  %-12s" % (name, _rate_cells(rate, min_rows),
                                     verdict)
        if control is not None:
            other = others.get(name)
            gap = ""
            if (rate is not None and other is not None
                    and rate.rows >= min_rows and other.rows >= min_rows):
                gap = "%+.1f" % ((other.rate - rate.rate) * 100.0)
            line += " | %s  %s" % (_rate_cells(other, min_rows), gap)
        out.append(line.rstrip())
    return out


# ------------------------------------------------------------------ self-test

class _Harness(object):
    """check() and raises() with a tally, and the footer that reports it.

    A class rather than two closures so that the self-test can run a second
    one and watch it fail. A harness whose red path never ran has not been
    shown to report red at all.
    """

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label):
        try:
            fn()
        except ValueError:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def summary(self):
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for f in self.failed:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


@contextlib.contextmanager
def _sandbox():
    """A temp directory, and the environment floor set aside, for one run.

    Both are undone in a finally. An exported GEOCODESIFT_MIN_TRUST_RATE moved
    the floors the checks expect and failed a correct install, and a crash
    left the directory and its CSVs behind.
    """
    saved = os.environ.pop("GEOCODESIFT_MIN_TRUST_RATE", None)
    tmp = tempfile.mkdtemp(prefix="geocodesift-selftest-")
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if saved is not None:
            os.environ["GEOCODESIFT_MIN_TRUST_RATE"] = saved


def self_test():
    """Assertions over the decision core. No file, no network, no credentials."""
    with _sandbox() as tmp:
        return _self_test(tmp)


def _self_test(tmp):
    harness = _Harness()
    check, raises = harness.check, harness.raises

    def verdict(score, mt, profile="esri"):
        return classify(score, mt, profile)[0]

    print("geocodesift self-test: no file, no network, no credentials")
    print("-" * 68)

    # ---- the match type overrides the score, the headline check
    check(verdict(100, "Zip5") == REJECT,
          "score 100 at match type Zip5 is REJECT  <-- pinned defect")
    check(verdict(98, "PostalExt") == REJECT, "a confident PostalExt is REJECT")
    check(verdict(99, "Locality") == REJECT,
          "a city centroid at score 99 is REJECT")
    check(verdict(100, "zip5") == REJECT, "the match type is read lowercase")
    check(verdict(100, "  ZIP5  ") == REJECT, "the match type is stripped first")
    check("AREA" in classify(100, "Zip5")[1],
          "the refusal says an area matched, not an address")

    # ---- the ordinary Esri verdicts
    check(verdict(98, "PointAddress") == TRUST, "a rooftop match at 98 is TRUST")
    check(verdict(100, "StreetAddress") == TRUST,
          "an interpolated street address at 100 is TRUST")
    check(verdict(100, "SubAddress") == TRUST, "a unit level match is TRUST")
    check(verdict(90, "PointAddress") == TRUST,
          "exactly the trust floor is TRUST, the floor is inclusive")
    check(verdict(89.9, "PointAddress") == SUSPECT,
          "a tenth under the trust floor is SUSPECT")
    check(verdict(83, "PointAddress") == SUSPECT,
          "a rooftop match at 83 is SUSPECT")
    check(verdict(61, "PointAddress") == REJECT,
          "a rooftop match at 61 is under the reject floor")
    check(verdict(100, "StreetName") == SUSPECT,
          "a street with no house number is SUSPECT at any score")
    check(verdict(100, "StreetInt") == SUSPECT, "an intersection match is SUSPECT")
    check(classify(100, "") == (SUSPECT, "no match type reported, so the "
                                "score cannot be read"),
          "a blank match type is SUSPECT, and says the type is missing")
    check(verdict(100, None) == SUSPECT, "a missing match type is SUSPECT")
    check(verdict(None, "PointAddress") == SUSPECT,
          "a missing score is SUSPECT even at a good match type")
    check(verdict(100, "Grid3") == SUSPECT,
          "a match type the profile does not know is SUSPECT")

    # ---- the Census profile, which carries no score at all
    check(verdict(None, "Exact", "census") == TRUST,
          "Census Exact is TRUST with no score present")
    check(verdict(None, "Non_Exact", "census") == SUSPECT,
          "Census Non_Exact is SUSPECT")
    check(verdict(None, "Tie", "census") == REJECT, "a Census tie is REJECT")
    check(verdict(None, "No_Match", "census") == REJECT,
          "a Census no-match is REJECT")

    # ---- the Esri Status column: M matched, T tied, U unmatched
    check(classify(100, "PointAddress", status="T")[0] == REJECT,
          "an Esri tie at PointAddress 100 is REJECT, as a census Tie is"
          "  <-- pinned defect")
    check("tie" in classify(100, "PointAddress", status="T")[1],
          "the refusal says the match was a tie")
    check(classify(0, "", status="U")[1]
          == "status U means the geocoder matched nothing",
          "an unmatched status is refused as unmatched")
    check(verdict(100, "PointAddress") == classify(
              100, "PointAddress", status="M")[0] == TRUST,
          "a matched status leaves the verdict to the score and match type")
    check(classify(100, "PointAddress", status=" t ")[0] == REJECT,
          "the status is read stripped and in upper case")
    check(classify(None, "Exact", "census", status="T")[0] == TRUST,
          "the census profile has no status codes, so a status is not read")

    # ---- the Nominatim profile, where importance is prominence not accuracy
    check(verdict(0.45, "house", "nominatim") == TRUST,
          "a Nominatim house at importance 0.45 is TRUST")
    check(verdict(0.9, "postcode", "nominatim") == REJECT,
          "a Nominatim postcode at importance 0.9 is REJECT")
    check(verdict(0.82, "city", "nominatim") == REJECT,
          "a prominent city is still REJECT")
    check(verdict(0.3, "road", "nominatim") == SUSPECT,
          "a Nominatim road is SUSPECT")
    check(verdict(0.05, "house", "nominatim") == REJECT,
          "a house under the Nominatim reject floor is REJECT")
    check(classify(0.45, "house", "nominatim", trust_score=60.0)[0] == SUSPECT,
          "an explicit trust floor overrides the profile default")

    raises(lambda: classify(90, "PointAddress", "esri-2"),
           "an unknown profile raises")
    check(classify(-1, "PointAddress")
          == (REJECT, "score -1 is negative, which no geocoder writes"),
          "a negative score cell is REJECT, not an error that stops the "
          "batch  <-- pinned defect")
    raises(lambda: classify(80, "PointAddress", trust_score=float("nan")),
           "a NaN trust floor raises instead of passing every row  <-- pinned "
           "defect")
    raises(lambda: classify(80, "PointAddress", suspect_score=float("nan")),
           "a NaN reject floor raises instead of switching the floor off"
           "  <-- pinned defect")
    raises(lambda: classify(80, "PointAddress", trust_score=float("inf")),
           "an infinite trust floor raises")
    raises(lambda: classify(float("nan"), "PointAddress"),
           "a NaN score raises instead of reaching TRUST  <-- pinned defect")
    raises(lambda: classify(float("inf"), "PointAddress"),
           "an infinite score raises")
    raises(lambda: classify("97.5", "PointAddress"),
           "a score that is still a string raises")
    raises(lambda: classify(90, "PointAddress", trust_score=50.0,
                            suspect_score=80.0),
           "a reject floor above the trust floor raises")
    raises(lambda: classify(90, "PointAddress", trust_score=90.0,
                            suspect_score=90.5),
           "a reject floor half a point above the trust floor raises")

    # ---- the floors at their exact boundary, and the units they report in
    check(verdict(75, "PointAddress") == SUSPECT,
          "exactly the reject floor of 75 is SUSPECT, the floor is inclusive")
    check(verdict(74.9, "PointAddress") == REJECT,
          "a tenth under the reject floor of 75 is REJECT")
    check(classify(60, "PointAddress")[1]
          == "score 60 is below the reject floor of 75",
          "the refusal names the reject floor in the score's own units")
    check(classify(83, "PointAddress")[1]
          == "score 83 is under the trust floor of 90",
          "the demotion names the trust floor in the score's own units")
    check(verdict(0, "PointAddress") == REJECT,
          "a score of 0 is a refusal, not an illegal score")
    check(classify(90, "PointAddress", trust_score=0.0,
                   suspect_score=0.0)[0] == TRUST,
          "floors of 0 are legal and let everything through")
    check(classify(80, "PointAddress", trust_score=80.0,
                   suspect_score=80.0)[0] == TRUST,
          "two equal floors are legal, they are not the wrong order")
    raises(lambda: classify(90, "PointAddress", suspect_score=-1.0),
           "a negative reject floor raises")
    check(classify(0.05, "house", "nominatim")[1]
          == "score 0.05 is below the reject floor of 0.1",
          "a nominatim floor is reported in importance, not in percent")
    check(classify(0.15, "house", "nominatim")[1]
          == "score 0.15 is under the trust floor of 0.2",
          "the nominatim trust floor is reported in importance too")
    check(verdict(0.2, "house", "nominatim") == TRUST,
          "an importance exactly at the nominatim trust floor is TRUST")
    check(verdict(90, "Exact", "census") == TRUST,
          "a score column named onto the census profile reads on its 0 to "
          "100 scale")
    check(worse(SUSPECT, REJECT) == REJECT and worse(REJECT, SUSPECT) == REJECT,
          "REJECT beats SUSPECT whichever order the two arrive in")
    check(worse(TRUST, SUSPECT) == SUSPECT and worse(SUSPECT, TRUST) == SUSPECT,
          "SUSPECT beats TRUST whichever order the two arrive in")

    # ---- ray casting against a concave C with an interior ring
    # The exterior is a C opening east: a notch is cut from x 4..10, y 4..6.
    # The hole is the square 1,1 to 3,3.
    outer = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 6), (10, 6), (10, 10), (0, 10)]
    hole = [(1, 1), (3, 1), (3, 3), (1, 3)]
    poly = [outer, hole]

    check(point_in_polygon(2, 5, poly) is True,
          "a point in the solid left bar is inside")
    check(point_in_polygon(7, 2, poly) is True,
          "a point in the lower arm is inside")
    check(point_in_polygon(7, 8, poly) is True,
          "a point in the upper arm is inside")
    check(point_in_polygon(5, 5, poly) is False,
          "a point in the concave notch is outside")
    check(point_in_polygon(-1, 5, poly) is False,
          "a point left of the shape is outside")
    check(point_in_polygon(11, 2, poly) is False,
          "a point right of the shape is outside")
    check(point_in_polygon(2, 2, poly) is False,
          "a point inside the hole is outside")
    check(point_in_polygon(1, 1, poly) is True,
          "a point on a hole vertex is inside")
    check(point_in_polygon(2, 1, poly) is True,
          "a point on a horizontal hole edge is inside")
    check(point_in_polygon(0, 0, poly) is True,
          "a point on an exterior vertex is inside")
    check(point_in_polygon(5, 0, poly) is True,
          "a point on the horizontal bottom edge is inside")
    check(point_in_polygon(2, 4, poly) is True,
          "a point whose ray passes through two vertices is still inside")
    check(point_in_polygon(12, 4, poly) is False,
          "a point right of the shape on that same vertex ray is outside")
    check(point_in_polygon(2, 6, poly) is True,
          "a point whose ray runs along a horizontal edge is inside")
    check(point_in_polygon(7, 6, poly) is True,
          "a point on the notch's horizontal edge is inside")
    check(point_in_polygon(4, 5, poly) is True,
          "a point on the vertical notch wall is inside")
    check(point_in_any(2, 5, [[hole], [outer]]) is True,
          "a point inside any polygon of a multipolygon is contained")
    check(point_in_any(50, 50, [[outer]]) is False,
          "a point in no polygon is not contained")
    raises(lambda: point_in_ring(0, 0, [(0, 0), (1, 1)]), "a two point ring raises")
    raises(lambda: point_in_polygon(0, 0, []), "a polygon with no ring raises")

    # ---- a slanted edge. Every ring above is axis aligned, and on those the
    # crossing x of an edge is just that edge's own x, so the interpolation is
    # never read. A real county line is full of diagonals.
    tri = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    check(point_in_ring(4.0, 4.0, tri) is True,
          "a point inside the hypotenuse is inside the triangle")
    check(point_in_ring(6.0, 6.0, tri) is False,
          "a point just past the hypotenuse is outside the triangle")
    check(point_in_ring(5.0, 5.0, tri) is True,
          "a point on the hypotenuse itself is inside the triangle")
    # Decimal degrees on a slanted line, where the cross product of a point
    # on the line comes out -2.8e-16 rather than 0. Without the tolerance in
    # _on_segment this parcel reads as outside the county. Every fixture
    # coordinate in this file sits in the open Atlantic, so none of them is
    # anybody's address.
    line_a, line_b = (-60.35231, 30.15085), (-59.69813, 30.07244)
    on_line = ((line_a[0] + line_b[0]) / 2.0, (line_a[1] + line_b[1]) / 2.0)
    check(point_in_ring(on_line[0], on_line[1],
                        [line_a, line_b, (-60.0, 29.5)]) is True,
          "a point on a slanted county line is inside, though the arithmetic "
          "misses zero")
    square = [(-5.0, -5.0), (15.0, -5.0), (15.0, 15.0), (-5.0, 15.0)]
    check(point_in_polygon(5.0, 5.0, [square, tri]) is True,
          "a point on a hole's slanted edge is inside the polygon")
    check(point_in_polygon(3.0, 3.0, [square, tri]) is False,
          "a point inside that slanted hole is outside the polygon")
    check(point_in_polygon(12.0, 12.0, [square, tri]) is True,
          "a point in the square and clear of the hole is inside")

    # ---- the GeoJSON shapes a county boundary actually arrives in
    bare = {"type": "Polygon", "coordinates": [outer, hole]}
    check(len(polygons_from_geojson(bare)) == 1, "a bare Polygon yields one polygon")
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": bare, "properties": {}},
        {"type": "Feature", "geometry": {"type": "MultiPolygon",
                                         "coordinates": [[outer], [hole]]},
         "properties": {}}]}
    check(len(polygons_from_geojson(fc)) == 3,
          "a FeatureCollection holding a MultiPolygon yields every ring set")
    raises(lambda: polygons_from_geojson({"type": "LineString",
                                          "coordinates": []}),
           "a GeoJSON with no polygon raises")
    nullgeom = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None, "properties": {}},
        {"type": "Feature", "geometry": bare, "properties": {}}]}
    check(len(polygons_from_geojson(nullgeom)) == 1,
          "a feature with a null geometry is skipped, not fatal  <-- pinned defect")
    check(polygons_from_geojson({"type": "FeatureCollection",
                                 "features": []}) == [],
          "a FeatureCollection with no feature yields no polygon")
    check(polygons_from_geojson({"type": "MultiPolygon"}) == [],
          "a MultiPolygon with no coordinates yields no polygon")
    for label, obj in (
            ("a GeoJSON that is a list", [1, 2]),
            ("a Polygon with no coordinates", {"type": "Polygon"}),
            ("a Polygon with no ring", {"type": "Polygon", "coordinates": []}),
            ("a Feature whose geometry is a string",
             {"type": "Feature", "geometry": "x"}),
            ("a ring of strings", {"type": "Polygon", "coordinates": [
                [["a", "b"], ["c", "d"], ["e", "f"]]]}),
            ("a ring of one-number positions", {"type": "Polygon",
                                                "coordinates": [[[0], [1], [2]]]}),
            ("a ring of two positions", {"type": "Polygon",
                                         "coordinates": [[[0, 0], [1, 1]]]}),
            ("a ring that is a number", {"type": "Polygon", "coordinates": [5]}),
            ("a NaN coordinate", {"type": "Polygon", "coordinates": [
                [[0, 0], [1, float("nan")], [1, 1]]]}),
            ("a true coordinate", {"type": "Polygon", "coordinates": [
                [[0, 0], [1, True], [1, 1]]]}),
            ("features that are 0",
             {"type": "FeatureCollection", "features": 0}),
            ("geometries that are a number",
             {"type": "GeometryCollection", "geometries": 5}),
            ("MultiPolygon coordinates that are a string",
             {"type": "MultiPolygon", "coordinates": "x"}),
            ("a MultiPolygon ring of two points",
             {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 1]]]]})):
        raises(lambda obj=obj: polygons_from_geojson(obj),
               "ValueError, not a crash, for %s  <-- pinned defect" % label)

    for obj in (["svc_user", "not-a-real-secret"],
                {"type": "FeatureCollection",
                 "features": {"token": "not-a-real-secret"}},
                {"type": "Polygon", "coordinates": "not-a-real-secret"}):
        msg = ""
        try:
            polygons_from_geojson(obj)
        except ValueError as exc:
            msg = str(exc)
        check(msg and "not-a-real-secret" not in msg,
              "a wrong boundary file is named by type, its values never "
              "echoed to stderr  <-- pinned defect")

    # ---- the pile-up detector over 30 synthetic rows
    stack = (-60.14000, 30.18700)      # the planted centroid stack
    complex4 = (-60.20000, 30.25000)   # a legitimate four unit complex
    coords = [stack] * 8 + [complex4] * 4
    coords += [(-60.30000 - i * 0.001, 30.30000 + i * 0.001) for i in range(18)]
    hits = find_pileups(coords, threshold=5)
    check(len(hits) == 1, "only the planted stack fires at threshold 5")
    check(hits[0][1] == 8, "the planted stack is reported with its count of 8")
    check(hits[0][0] == (-60.14, 30.187), "the pile-up reports its coordinate")
    check(all(k != pileup_key(*complex4) for k, _ in hits),
          "a legitimate complex of 4 does not fire at threshold 5")
    check(len(find_pileups(coords, threshold=4)) == 2,
          "lowering the threshold to 4 catches the complex as well")
    check(find_pileups(coords, threshold=8)[0][1] == 8,
          "exactly the threshold count fires, the threshold is inclusive")
    check(find_pileups(coords, threshold=9) == [],
          "a threshold above the stack size reports nothing")
    near = coords + [(-60.139999, 30.187001)]
    check(find_pileups(near, threshold=5)[0][1] == 9,
          "a coordinate inside the rounding tolerance joins the stack")
    check(find_pileups(near, threshold=5, precision=6)[0][1] == 8,
          "a finer precision splits that near coordinate back out")
    raises(lambda: find_pileups(coords, threshold=1),
           "a pile-up threshold below 2 raises")
    check(find_pileups([(1.0, 1.0)] * 2, threshold=2)[0][1] == 2,
          "a threshold of 2 is legal, two records are the smallest pile-up")
    check([n for _, n in find_pileups(coords, threshold=4)] == [8, 4],
          "pile-ups are reported worst first")
    check(find_pileups([(1.0, 1.0)] * 3 + [(0.0, 0.0)] * 3, threshold=3)
          == [((0.0, 0.0), 3), ((1.0, 1.0), 3)],
          "two pile-ups of one size are ordered by coordinate, so the report "
          "does not shuffle")

    # ---- house number sanity
    check(house_number("1234 NE 31ST ST") == 1234, "a leading house number is read")
    check(house_number("123-45 MAIN ST") == 123,
          "a hyphenated number reads its first part")
    check(house_number("123A NW 2ND AVE") == 123, "a unit letter is dropped")
    check(house_number("NE 31ST ST") is None, "a street with no number reads None")
    check(house_number("\u00b2345 MAIN ST") is None,
          "a superscript digit is not a house number  <-- pinned defect")
    check(house_number("\u0661\u0662\u0663 MAIN ST") is None,
          "an Arabic-Indic numeral is not a US house number")
    check(house_number("") is None, "an empty address reads None")
    check(house_number(None) is None, "a missing address reads None")
    check(house_number("9" * 5000 + " MAIN ST") is None,
          "a run of 5000 digits is not a house number, and raises nothing"
          "  <-- pinned defect")
    check(house_number("123456789 MAIN ST") == 123456789
          and house_number("1234567890 MAIN ST") is None,
          "nine digits are a house number and ten are not")
    check(house_number_problem("1234 NE 31ST ST", "1234 NE 31ST ST, ANYTOWN") is None,
          "an identical house number is no problem")
    check(house_number_problem("1234 NE 31ST ST", "1240 NE 31ST ST") is None,
          "an interpolation 6 numbers off is tolerated")
    check(house_number_problem("1234 NE 31ST ST", "4321 NE 31ST ST") is not None,
          "a house number on another block is flagged")
    check(house_number_problem("1234 NE 31ST ST", "NE 31ST ST") is None,
          "no number on the matched side is left to the match type")
    check(house_number_problem("1234 NE 31ST ST", "1250 NE 31ST ST",
                               tolerance=10) is not None,
          "a tighter tolerance flags what the default allowed")
    raises(lambda: house_number_problem("1 A", "9 A", tolerance=-1),
           "a negative house number tolerance raises")
    check(house_number_problem("100 MAIN ST", "120 MAIN ST") is None,
          "exactly 20 apart is inside the default tolerance")
    check(house_number_problem("100 MAIN ST", "121 MAIN ST") is not None,
          "21 apart is outside the default tolerance")
    check(house_number_problem("100 MAIN ST", "101 MAIN ST",
                               tolerance=0) is not None,
          "a tolerance of 0 is legal and demands the same house number")
    check(house_number_problem("100 MAIN ST", "100 MAIN ST, ANYTOWN",
                               tolerance=0) is None,
          "a tolerance of 0 still passes the same house number")
    check(house_number("12346, ANYTOWN, ANYSTATE") == 12346,
          "a matched address that leads with a ZIP code reads the ZIP as a "
          "house number, the limit the README names")

    # ---- field extraction off a raw CSV row
    fields = fields_for("esri")
    raw = {"Score": "97.5", "Addr_type": "PointAddress", "X": "-60.14",
           "Y": "30.187", "Match_addr": "1234 NE 31ST ST",
           "USER_address": "1234 NE 31ST ST"}
    row = extract_row(raw, fields)
    check(row["score"] == 97.5, "the score column is read as a number")
    check(row["match_type"] == "PointAddress", "the match type column is read")
    check(extract_row({"Score": ""}, fields)["score"] is None,
          "a blank score cell reads None, not zero")
    check(extract_row({"Score": "n/a"}, fields)["score"] is None,
          "a score cell that is not a number reads None")
    check(to_number("nan") is None,
          "a score cell reading nan is absent, not a number  <-- pinned defect")
    check(to_number("inf") is None, "a score cell reading inf is absent")
    check(to_number("-1e400") is None, "a score cell that overflows is absent")
    check(to_number("1e308") == 1e308, "a huge but finite score is still read")
    check(to_number("  97.5  ") == 97.5, "a padded score cell is still read")
    check(extract_row({"Score": "nan", "Addr_type": "PointAddress"},
                      fields)["score"] is None,
          "a NaN score cell cannot reach classify at all")
    check(extract_row({}, fields)["x"] is None, "a missing column reads None")
    check(extract_row({"Addr_type": None}, fields)["match_type"] is None,
          "a short row's empty cell stays absent, it does not read 'None'")
    check(fields_for("esri", {"score": "MyScore"})["score"] == "MyScore",
          "a field override replaces the profile column name")
    check(fields_for("esri", {"score": ""})["score"] == "Score",
          "an empty override leaves the profile column alone")
    check(fields_for("census")["score"] == "",
          "the census profile declares no score column")
    raises(lambda: fields_for("esri", {"nope": "x"}),
           "an unknown field override raises")

    # ---- the audit, end to end over the decision core
    good = {"score": 98.0, "match_type": "PointAddress", "x": -60.3, "y": 30.3,
            "in_addr": "100 MAIN ST", "matched_addr": "100 MAIN ST"}
    rep = audit([good])
    check(rep.counts[TRUST] == 1, "a clean row audits TRUST")
    check(rep.trust_rate == 1.0, "one clean row is a 100% trust rate")

    centroid = dict(good, match_type="Zip5")
    rep = audit([good] * 3 + [centroid])
    check(rep.counts[REJECT] == 1, "a centroid row audits REJECT")
    check(rep.trust_rate == 0.75, "three of four clean is a 75% trust rate")

    stacked_rows = [dict(good, x=stack[0], y=stack[1]) for _ in range(8)]
    rep = audit(stacked_rows + [good] * 2)
    check(rep.counts[TRUST] == 2,
          "eight stacked rows leave the TRUST count at 2  <-- pinned defect")
    check(len(rep.pileups) == 1 and rep.pileups[0][1] == 8,
          "the audit reports the stack it demoted")

    check(audit([dict(good, x=None, y=None)]).counts[REJECT] == 1,
          "a row with no coordinate is REJECT")
    nan = float("nan")
    check(audit([dict(good, x=nan, y=nan)]).counts[REJECT] == 1,
          "a NaN coordinate is REJECT, not a point on the map  <-- pinned defect")
    check(audit([dict(good, x=nan, y=nan) for _ in range(8)]
                ).counts[REJECT] == 8,
          "eight NaN coordinates cannot hide from the pile-up check as eight points")
    check(audit([dict(good, x=nan, y=nan) for _ in range(8)]).pileups == [],
          "a NaN pile-up reports no coordinate rather than a false one")
    check(audit([dict(good, x=float("inf"), y=30.3)]).counts[REJECT] == 1,
          "an infinite coordinate is REJECT")
    check(audit([dict(good, x="-60.3", y="30.3")]).counts[REJECT] == 1,
          "a coordinate left as text is REJECT, not a crash")
    raises(lambda: audit([good], polygons=[]),
           "a boundary holding no polygon raises rather than skipping the "
           "check  <-- pinned defect")
    check(audit([dict(good, x=2, y=5)], polygons=[poly]).counts[TRUST] == 1,
          "a row inside the boundary stays TRUST")
    check(audit([dict(good, x=5, y=5)], polygons=[poly]).counts[REJECT] == 1,
          "a row outside the boundary is REJECT")
    check(audit([dict(good, matched_addr="9100 MAIN ST")]).counts[SUSPECT] == 1,
          "a row with a far house number drops to SUSPECT")

    # ---- the gate, which is what a scheduled job reads
    def spread(n, template=good, start=0):
        """n rows on distinct coordinates, so the pile-up check stays quiet."""
        return [dict(template, x=-60.3 - i * 0.01, y=30.3 + i * 0.01)
                for i in range(start, start + n)]

    check(gate(audit(spread(10)), 0.90) == 0, "a batch at 100% passes the gate")
    check(gate(audit(spread(9) + spread(1, centroid, 9)), 0.90) == 0,
          "exactly the floor passes, the floor is inclusive")
    check(gate(audit(spread(8) + spread(2, centroid, 8)), 0.90) == 1,
          "a batch at 80% fails the gate")
    check(gate(audit([]), 0.90) == 1,
          "an empty batch fails the gate rather than scoring a perfect rate"
          "  <-- pinned defect")
    check(audit([]).trust_rate == 0.0, "an empty batch reports a 0% trust rate")
    check(gate(audit([]), 0.0) == 1,
          "an empty batch fails even a floor of 0, where 0.0 >= 0.0 passed it"
          "  <-- pinned defect")
    raises(lambda: gate(audit([good]), 1.5), "a trust rate floor above 1 raises")
    check(gate(audit(spread(10)), 1.0) == 0,
          "a floor of 1.0 is legal and a clean batch clears it")
    check(gate(audit(spread(9) + spread(1, centroid, 9)), 1.0) == 1,
          "one bad row fails a floor of 1.0")
    check(gate(audit(spread(8) + spread(2, centroid, 8)), 0.0) == 0,
          "a floor of 0.0 is legal and passes a batch at 80 percent")

    lines = describe(audit(spread(8) + spread(2, centroid, 8)), 0.90)
    check(any(l == "TRUST rate: 80.0% (floor 90.0%)" for l in lines),
          "the report prints the trust rate and the floor it is judged against")
    check(any("row 10 REJECT" in l for l in lines),
          "the report names a failing row by its line number in the CSV")

    # ---- argument handling
    a = _parse(["--csv", "g.csv"])
    check(a.apply is False, "--apply defaults to OFF")
    check(a.out is None, "--out defaults to nothing written")
    check(a.boundary is None, "--boundary defaults to off")
    # The values, not the constants the defaults are built from. Compared with
    # the constant, each of these passes whatever the constant is changed to,
    # and the README documents a number.
    check(a.profile == "esri", "--profile defaults to esri")
    check(a.min_trust_rate == 0.90, "--min-trust-rate defaults to 0.90")
    check(a.pileup_threshold == 5, "--pileup-threshold defaults to 5")
    check(a.pileup_precision == 5, "--pileup-precision defaults to 5")
    check(a.house_tolerance == 20, "--house-number-tolerance defaults to 20")
    check(a.trust_score is None and a.suspect_score is None,
          "the score floors default to the profile's own")
    check(a.score_field is None, "--score-field defaults to the profile column")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--csv", "g.csv", "--profile", "census"]).profile == "census",
          "--profile is read")
    check(_parse(["--csv", "g.csv", "--apply"]).apply is True, "--apply is read")

    # ---- the csv can arrive positionally, the way a script tool passes it
    check(_parse(["g.csv"]).csv == "g.csv",
          "a positional CSV is accepted, for an ArcGIS script tool")
    check(_parse(["--csv", "named.csv"]).csv == "named.csv",
          "a named CSV is accepted")
    check(_parse(["pos.csv", "--csv", "named.csv"]).csv == "named.csv",
          "the named CSV wins when both forms are given")
    check(_parse(["--self-test"]).csv is None,
          "no CSV is needed to run the self-test")

    # ---- an absent optional column is a note, an absent required one is a warning
    f = fields_for("esri")
    m = missing_columns(f, ["Score", "Addr_type", "X", "Y", "Match_addr"])
    check(m["required"] == [],
          "a valid Esri export is missing no required column")
    check(m["optional"] == [("Status", "a tied match cannot be caught"),
                            ("USER_address",
                             "the house-number check is skipped")],
          "a missing input-address column is optional, not a warning  <-- pinned defect")
    m = missing_columns(f, ["Addr_type", "X", "Y"])
    check("Score" in m["required"],
          "a missing score column is required, because every row then reads SUSPECT")
    m = missing_columns(f, ["Score", "Addr_type", "X", "Y", "Match_addr",
                            "USER_address", "Status"])
    check(m["required"] == [] and m["optional"] == [],
          "a complete header reports nothing missing")
    check(ambiguous_columns(f, ["Score", "Addr_type", "X", "Y", "Score"])
          == ["Score"], "a column this reads, named twice, is ambiguous")
    check(ambiguous_columns(f, ["Score", "Addr_type", "X", "Y", "", ""]) == [],
          "repeated names this never reads are not ambiguous")
    check(_parse(["--csv", "g.csv", "--boundary", "b.json"]).boundary == "b.json",
          "--boundary is read")
    check(_parse(["--csv", "g.csv", "--min-trust-rate", "0.5"]
                 ).min_trust_rate == 0.5, "--min-trust-rate is read")
    check(_parse(["--csv", "g.csv", "--pileup-threshold", "3"]
                 ).pileup_threshold == 3, "--pileup-threshold is read")
    check(_parse(["--csv", "g.csv", "--pileup-precision", "6"]
                 ).pileup_precision == 6, "--pileup-precision is read")
    check(_parse(["--csv", "g.csv", "--trust-score", "95"]).trust_score == 95.0,
          "--trust-score is read")
    check(_parse(["--csv", "g.csv", "--suspect-score", "60"]
                 ).suspect_score == 60.0, "--suspect-score is read")
    check(_parse(["--csv", "g.csv", "--house-number-tolerance", "5"]
                 ).house_tolerance == 5, "--house-number-tolerance is read")
    check(_parse(["--csv", "g.csv", "--score-field", "CONF"]
                 ).score_field == "CONF", "--score-field is read")
    check(_parse(["--csv", "g.csv", "--match-type-field", "T"]
                 ).match_type_field == "T", "--match-type-field is read")
    check(_parse(["--csv", "g.csv", "--x-field", "LON"]).x_field == "LON",
          "--x-field is read")
    check(_parse(["--csv", "g.csv", "--y-field", "LAT"]).y_field == "LAT",
          "--y-field is read")
    check(_parse(["--csv", "g.csv", "--address-field", "IN"]
                 ).address_field == "IN", "--address-field is read")
    check(_parse(["--csv", "g.csv", "--matched-address-field", "M"]
                 ).matched_address_field == "M",
          "--matched-address-field is read")
    check(_parse(["--csv", "g.csv", "--status-field", "S"]).status_field
          == "S", "--status-field is read")
    check(_parse(["--csv", "g.csv", "--out", "o.csv"]).out == "o.csv",
          "--out is read")
    check(a.by_class is False, "--by-class defaults to off")
    check(a.min_class_rows == 30, "--min-class-rows defaults to 30")
    check(a.compare is None and a.compare_profile is None,
          "--compare defaults to no control file")
    check(_parse(["--csv", "g.csv", "--by-class"]).by_class is True,
          "--by-class is read")
    check(_parse(["--csv", "g.csv", "--min-class-rows", "50"]
                 ).min_class_rows == 50, "--min-class-rows is read")
    check(_parse(["--csv", "g.csv", "--compare", "c.csv", "--compare-profile",
                  "census"]).compare_profile == "census",
          "--compare and --compare-profile are read")

    # ---- the pure gaps the sections above stepped over
    check(repr(audit([good]).results[0]).startswith("RowResult(0, TRUST"),
          "an audited row prints its index and verdict for debugging")
    raises(lambda: classify(90, "PointAddress", trust_score=-1.0),
           "a negative trust floor raises")
    check(point_in_ring(5, 0, outer) is True,
          "a point on a ring's own edge counts as on that ring")
    gcol = {"type": "GeometryCollection",
            "geometries": [bare, {"type": "MultiPolygon",
                                  "coordinates": [[outer]]}]}
    check(len(polygons_from_geojson(gcol)) == 2,
          "a GeometryCollection yields every polygon inside it")
    check(to_number(97.5) == 97.5, "a cell that is already a number is read")
    check(to_number(float("nan")) is None,
          "a NaN that is already a float is still absent")
    raises(lambda: fields_for("mystery"), "an unknown profile has no field map")

    # ---- the census profile's column map over a batch reshaped into it
    cf = fields_for("census")
    check((cf["x"], cf["y"], cf["match_type"]) == ("lon", "lat", "match_type"),
          "the census profile reads lon, lat and match_type")
    check((cf["in_addr"], cf["matched_addr"])
          == ("input_address", "matched_address"),
          "the census profile reads input_address and matched_address")
    census_raw = {"input_address": "1234 NE 31ST ST, ANYTOWN, ZZ, 12345",
                  "matched_address": "1234 NE 31ST ST, ANYTOWN, ZZ, 12345",
                  "lon": "-60.12650", "lat": "30.17860",
                  "match_type": "Exact"}
    crow = extract_row(census_raw, cf)
    check(crow["score"] is None,
          "a census row carries no score, because the profile names no column")
    check((crow["x"], crow["y"]) == (-60.1265, 30.1786),
          "the census lon and lat columns are read as the coordinate")
    check(crow["match_type"] == "Exact", "the census match type column is read")
    check(crow["in_addr"].startswith("1234 NE"),
          "the census input_address column is read")
    check("score" not in classify(None, "No_Match", "census")[1],
          "a scoreless refusal does not describe a score it never had"
          "  <-- pinned defect")
    census_rows = [crow,
                   extract_row(dict(census_raw, match_type="Non_Exact",
                                    lon="-60.14010", lat="30.18990"), cf),
                   extract_row(dict(census_raw, match_type="No_Match",
                                    lon="", lat="", matched_address=""), cf),
                   extract_row(dict(census_raw, match_type="Tie",
                                    lon="-60.13000", lat="30.18700"), cf)]
    crep = audit(census_rows, "census")
    check((crep.counts[TRUST], crep.counts[SUSPECT], crep.counts[REJECT])
          == (1, 1, 2),
          "a four row census batch audits 1 TRUST, 1 SUSPECT and 2 REJECT")
    check(crep.trust_rate == 0.25,
          "a profile with no score column still produces a gateable rate")
    check(gate(crep, 0.90) == 1, "that census batch fails the default gate")

    # ---- the Nominatim profile's real column map over a real Nominatim batch
    nf = fields_for("nominatim")
    check((nf["score"], nf["match_type"]) == ("importance", "addresstype"),
          "the nominatim profile reads importance and addresstype")
    check((nf["matched_addr"], nf["in_addr"]) == ("display_name", "query"),
          "the nominatim profile reads display_name and query")
    nrow = extract_row({"query": "1234 NE 31ST ST, Anytown",
                        "display_name": "1234, Northeast 31st Street, Anytown",
                        "lon": "-60.12650", "lat": "30.17860",
                        "addresstype": "house", "importance": "0.45"}, nf)
    check(nrow["score"] == 0.45,
          "the nominatim importance column is read as the score")
    check(nrow["matched_addr"].startswith("1234,"),
          "the nominatim display_name column is read as the matched address")
    nrep = audit([nrow,
                  dict(nrow, match_type="city", score=0.82,
                       x=-60.14010, y=30.18720),
                  dict(nrow, match_type="road", score=0.30,
                       x=-60.13000, y=30.17900),
                  dict(nrow, match_type="postcode", score=0.20,
                       x=-60.12000, y=30.16000)], "nominatim")
    check((nrep.counts[TRUST], nrep.counts[SUSPECT], nrep.counts[REJECT])
          == (1, 1, 2),
          "a four row nominatim batch audits 1 TRUST, 1 SUSPECT and 2 REJECT")
    check("AREA" in nrep.results[1].reasons[0],
          "a prominent city is refused on its match type, not its importance")

    # ---- describe() on the two reports it has to render
    many = []
    for i in range(6):
        many.extend([dict(good, x=-60.0 - i, y=30.0)] * 5)
    lines = describe(audit(many), 0.90, sample=5)
    check(any(l == "coordinate pile-ups:" for l in lines),
          "the report lists the pile-ups it found")
    check(any(l.startswith("  5 record(s) on") for l in lines),
          "each pile-up is printed with its count and its coordinate")
    check(any(l == "  ...and 1 more" for l in lines),
          "the report stops after the sample and counts the rest")
    check(sum(1 for l in describe(audit(many), 0.90)
              if l.startswith("  5 record(s) on")) == 5,
          "describe samples five pile-ups when it is given no sample size")
    five = []
    for i in range(5):
        five.extend([dict(good, x=-60.0 - i, y=30.0)] * 5)
    check(not any("...and" in l for l in describe(audit(five), 0.90)),
          "exactly the sample size prints no and-more line")
    lines = describe(audit(spread(3)), 0.90)
    check(not any("not TRUST" in l for l in lines),
          "a clean batch lists no failing rows at all")

    # ---- address classes. The rules are in address_classes(); every rule and
    # every exception to a rule has a case here.
    ac = address_classes
    check(ac("1234 NE 31ST ST") == ["directional", "numbered_street",
                                    "suffix:ST"],
          "a directional numbered street is in both classes at once")
    check(ac("100 MAIN ST") == ["plain", "suffix:ST"],
          "a house number, a name and a suffix is plain")
    check(ac("100 N MAIN ST") == ["directional", "suffix:ST"],
          "a directional before a street name is directional")
    check(ac("100 NORTH MAIN ST") == ["directional", "suffix:ST"],
          "a spelled out directional before a name is directional")
    check(ac("100 NORTH ST") == ["plain", "suffix:ST"],
          "a directional word followed by a suffix is the street's name")
    check(ac("100 N COURT ST") == ["directional", "suffix:ST"],
          "a directional before a name that is also a suffix word is "
          "directional  <-- pinned defect")
    check(ac("100 N ST JOHNS AVE") == ["directional", "suffix:AVE"],
          "North Saint Johns Avenue is directional  <-- pinned defect")
    check(all(ac(a) == ["directional", "suffix:" + x] for a, x in (
              ("100 W LOOP RD", "RD"), ("100 E TRAIL RD", "RD"),
              ("100 N CIRCLE DR", "DR"))),
          "LOOP, TRAIL and CIRCLE before a suffix are names after a "
          "directional")
    check(ac("100 N MAIN") == ["directional", "suffix:none"],
          "a directional before a name with no suffix is directional")
    check(ac("100 NORTH ST NW") == ["directional", "suffix:ST"],
          "a suffix word followed by a post-directional is the street's name")
    check(ac("100 NORTH ST APT 4") == ["unit", "suffix:ST"],
          "a suffix word followed by a unit is the street's name")
    check(ac("100 E ST") == ["plain", "suffix:ST"],
          "a street called E is not a directional")
    check(ac("100 N") == ["plain", "suffix:none"],
          "a directional with nothing after it is the street's name")
    check(ac("100 MAIN ST NW") == ["directional", "suffix:ST"],
          "a directional straight after the suffix is directional")
    check(ac("100 MAIN ST ANYTOWN") == ["plain", "suffix:ST"],
          "a word after the suffix that is not a directional changes nothing")
    check(ac("100 MAIN ST NW APT 4") == ["directional", "unit", "suffix:ST"],
          "a post-directional and a unit are both read")
    check(ac("100 SECOND AVE") == ["numbered_street", "suffix:AVE"],
          "a spelled ordinal is a numbered street")
    for addr in ("100 1ST ST", "100 62ND AVE", "100 3RD AVE", "100 4TH ST",
                 "100 10TH ST"):
        check(ac(addr)[0] == "numbered_street",
              "%s is a numbered street  <-- pinned defect" % addr)
    for addr in ("100 ELEVENTH AVE", "100 TWELFTH ST", "100 NINETEENTH ST",
                 "100 TWENTIETH ST", "100 HUNDREDTH ST", "100 TWENTY-FIRST ST",
                 "100 TWENTY FIRST ST", "100 NINETY-NINTH ST"):
        check(ac(addr)[0] == "numbered_street",
              "%s is a numbered street  <-- pinned defect" % addr)
    check(ac("100 TWENTY OAKS ST") == ["plain", "suffix:ST"]
          and ac("100 TWENTY-ONE ST") == ["plain", "suffix:ST"],
          "a tens word without an ordinal after it is a name")
    check(ac("100 TWENTY TENTH ST") == ["plain", "suffix:ST"],
          "a tens word before TENTH is a name, only FIRST to NINTH join it"
          "  <-- pinned defect")
    for word in ("NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST"):
        check(ac("100 %s 31ST ST" % word)
              == ["directional", "numbered_street", "suffix:ST"],
              "a spelled out %s is a directional  <-- pinned defect" % word)
    check(ac("100 22 ST") == ["numbered_street", "suffix:ST"],
          "a bare number before a suffix is a numbered street")
    check(ac("100 22") == ["plain", "suffix:none"],
          "a bare number with no suffix after it is not a numbered street")
    check(ac("100 31ST") == ["numbered_street", "suffix:none"],
          "an ordinal with no suffix is still a numbered street")
    check(ac("1200 COUNTY ROAD 900") == ["route"],
          "a county road number is a route, with no suffix class")
    check(ac("1200 US HWY 999") == ["route"], "a US highway number is a route")
    check(ac("100 SR 123 ANYTOWN") == ["route"],
          "a word after the route number that is not a directional is ignored")
    check(ac("1200 SE COUNTY ROAD 900") == ["directional", "route"],
          "a directional before a county road is directional")
    check(ac("100 N HWY 999") == ["directional", "route"],
          "a directional before HWY and a number is directional, though HWY "
          "is also a suffix")
    check(ac("100 CR 900 N") == ["directional", "route"],
          "a directional after the route number is directional")
    check(ac("100 CR 900A") == ["route"],
          "a route number is read as a house number, so 900A is a route"
          "  <-- pinned defect")
    check(ac("100 SR 123A N") == ["directional", "route"],
          "a directional after a lettered route number is directional"
          "  <-- pinned defect")
    check(ac("100 ROAD 5") == ["plain", "suffix:none"],
          "ROAD is a route word but not a route lead, so ROAD 5 is no route"
          "  <-- pinned defect")
    check(ac("100 STATE ST") == ["plain", "suffix:ST"],
          "STATE ST is a street, not a state road")
    check(ac("100 COUNTY RD") == ["plain", "suffix:RD"],
          "COUNTY RD with no number is a street called County")
    check(ac("100 OLD DIXIE HWY") == ["plain", "suffix:HWY"],
          "HWY after a street name is a suffix")
    check(ac("HIGHWAY 999") == ["route", "no_number"],
          "a highway with no house number is a route with no number")
    check(all(ac(a) == ["po_box"] for a in ("PO BOX 1234", "P.O. Box 12",
                                            "P O BOX 5", "POST OFFICE BOX 9")),
          "every spelling of a PO box is po_box and nothing else")
    check(all(ac(a) == ["rural_route"]
              for a in ("RR 2 BOX 15", "HC 1 BOX 7", "HCR 3 BOX 2",
                        "RURAL ROUTE 3 BOX 1", "HIGHWAY CONTRACT 2 BOX 4")),
          "every spelling of a rural route is rural_route and nothing else")
    check(ac("NE 31ST ST") == ["directional", "numbered_street", "no_number",
                               "suffix:ST"],
          "a street with no house number is no_number and keeps its classes")
    check(ac("\u00b2 MAIN ST") == ["no_number", "suffix:ST"],
          "a superscript digit is not a house number here either")
    check(all(ac(a) == ["blank"] for a in ("", None, "   ", ",")),
          "an empty, missing, blank or comma-only address is blank")
    check(ac("100") == ["plain", "suffix:none"],
          "a house number alone is plain with no suffix")
    check(ac("100 MAIN ST #4") == ["unit", "suffix:ST"],
          "a # unit is a unit")
    check(ac("100 MAIN ST # 4") == ac("100 MAIN ST #4"),
          "# 4 and #4 read the same")
    check(ac("100 MAIN ST, APT 4") == ["unit", "suffix:ST"],
          "a unit after a comma is still a unit")
    check(ac("100 MAIN ST STE 200 ANYTOWN CT") == ["unit", "suffix:ST"],
          "the suffix is the last one before the unit word, not a state "
          "after it  <-- pinned defect")
    check(ac("100 MAIN ST, ANYTOWN, ZZ") == ["plain", "suffix:ST"],
          "the city and state after a comma are not read")
    check(ac("100 MAIN ST, WEST ANYTOWN") == ["plain", "suffix:ST"],
          "a city after a comma that starts with a directional is not read")
    check(ac("100 MAIN ST, ") == ["plain", "suffix:ST"],
          "an empty part after a comma is not read")
    check(ac("100 MAIN ST APT") == ["plain", "suffix:ST"],
          "a unit word with nothing after it is not a unit")
    check(ac("123 1/2 MAIN ST") == ["plain", "suffix:ST"],
          "a fractional house number is still a house number")
    check(ac("123 1/2 N MAIN ST") == ["directional", "suffix:ST"],
          "the fraction is skipped, so the directional after it is read")
    check(ac("100 E/W 31ST ST") == ["plain", "suffix:ST"],
          "a slash token that is not a fraction is not skipped as one"
          "  <-- pinned defect")
    check(ac("100 SPACE COAST PKWY") == ["plain", "suffix:PKWY"],
          "a unit word that starts a street name is the name")
    check(ac("100 LAKE WAY DR") == ["plain", "suffix:DR"],
          "the last suffix wins, so LAKE WAY DR is a drive")
    check(ac("100 ST JOHNS AVE") == ["plain", "suffix:AVE"],
          "a leading ST is Saint, not the suffix")
    check(ac("100 COURT") == ["plain", "suffix:none"],
          "the first word of the street name is never the suffix")
    check(ac("100 MAIN STREET") == ac("100 MAIN ST"),
          "a spelled out suffix reads as its USPS abbreviation")
    check(ac("100 n main st") == ac("100 N MAIN ST"),
          "upper and lower case classify the same")
    check(address_tokens("P.O. Box 12") == ["PO", "BOX", "12"],
          "full stops are dropped before the tokens are read")
    # The limits the README names, held here so the README stays true.
    check(ac("100 LAKE WAY") == ["plain", "suffix:WAY"],
          "a name ending in a suffix word, with no suffix of its own, reads "
          "as that suffix")
    check(ac("100 ONE HUNDRED FIRST ST") == ["plain", "suffix:ST"]
          and ac("100 101ST ST")[0] == "numbered_street",
          "a spelled ordinal above HUNDREDTH is plain, and its number is not")
    check(ac("1234567890 MAIN ST") == ["no_number", "suffix:ST"],
          "a ten digit house number reads as no number")
    check(ac("100 ELM PARK") == ["plain", "suffix:none"],
          "PARK is left out of the suffix table, so it reads as no suffix")
    check(ac("100 MAIN ANYTOWN CT") == ["plain", "suffix:CT"],
          "with no comma, a state abbreviation that is a suffix is read as "
          "the suffix")
    check(ac("100 MAIN ST WEST ANYTOWN") == ["directional", "suffix:ST"],
          "with no comma, a city that starts with a directional adds "
          "directional")
    check(ac("100 NORTH ST ANYTOWN") == ["directional", "suffix:none"],
          "with no comma, a city after a street named for a direction reads "
          "as the rest of the street")

    # ---- the Wilson interval, against values worked by hand
    lo, hi = wilson_interval(50, 100)
    check((round(lo, 4), round(hi, 4)) == (0.4038, 0.5962),
          "the Wilson interval of 50 of 100 is 40.38 to 59.62 percent")
    lo, hi = wilson_interval(0, 10)
    check(lo == 0.0 and round(hi, 4) == 0.2775,
          "0 of 10 runs from exactly 0 to 27.75 percent, not a zero width "
          "interval")
    lo, hi = wilson_interval(10, 10)
    check(round(lo, 4) == 0.7225 and hi == 1.0,
          "10 of 10 runs from 72.25 percent to exactly 100")
    check(wilson_interval(0, 5)[0] == 0.0,
          "0 of 5 has a low of exactly 0, where the arithmetic gives "
          "-2.8e-17 and prints -0.0%  <-- pinned defect")
    check(wilson_interval(5, 5)[1] == 1.0,
          "5 of 5 has a high of exactly 1, where the arithmetic gives "
          "1.0000000000000002  <-- pinned defect")
    lo, hi = wilson_interval(3, 5)
    check((round(lo, 2), round(hi, 2)) == (0.23, 0.88),
          "3 of 5 runs from 23 to 88 percent, which is why a small class is "
          "refused")
    lo, hi = wilson_interval(7, 60)
    check(lo < 7 / 60.0 < hi, "the interval holds the rate it was built from")
    raises(lambda: wilson_interval(0, 0), "an interval over no rows raises")
    raises(lambda: wilson_interval(11, 10), "more successes than rows raises")
    raises(lambda: wilson_interval(-1, 10), "negative successes raise")

    # ---- the exact interval, against closed forms: 0 of n ends at
    # 1 - 0.025 ** (1 / n), and n of n starts at 0.025 ** (1 / n)
    lo, hi = exact_interval(0, 1)
    check(lo == 0.0 and round(hi, 6) == 0.975,
          "the exact interval of 0 of 1 runs from 0 to 97.5 percent")
    check(round(exact_interval(0, 2)[1], 4) == round(1 - 0.025 ** 0.5, 4),
          "0 of 2 ends at 84.19 percent")
    lo, hi = exact_interval(29, 29)
    check(round(lo, 6) == round(0.025 ** (1 / 29.0), 6) and hi == 1.0,
          "29 of 29 starts at 88.06 percent and ends at exactly 100")
    lo, hi = exact_interval(2, 20)
    check((round(lo, 4), round(hi, 4)) == (0.0123, 0.317),
          "2 of 20 runs from 1.23 to 31.70 percent")
    check(exact_interval(0, 1)[1] > wilson_interval(0, 1)[1],
          "the exact bound of 0 of 1 is wider than the Wilson bound of 79 "
          "percent")
    raises(lambda: exact_interval(3, 2), "more successes than rows raises")

    # ---- the verdict of one class
    check(ClassRate("x", 20, 2).status(0.9, 30) == "BELOW",
          "2 of 20 is under the row minimum but its interval ends at 32 "
          "percent, so it is BELOW, not refused  <-- pinned defect")
    check(ClassRate("x", 29, 0).status(0.9, 30) == "BELOW",
          "0 of 29 is BELOW one row under the minimum  <-- pinned defect")
    check(ClassRate("x", 1, 0).status(0.9, 30) == "too few rows",
          "0 of 1 is refused at a 90 percent floor, its exact interval ends "
          "at 97.5 percent  <-- pinned defect")
    check(ClassRate("x", 2, 0).status(0.9, 30) == "BELOW",
          "0 of 2 is BELOW a 90 percent floor, its exact interval ends at 84 "
          "percent")
    check(ClassRate("x", 20, 2).status(exact_interval(2, 20)[1], 30)
          == "too few rows",
          "a floor exactly at the interval's high is refused, not BELOW, "
          "because the interval holds it  <-- pinned defect")
    check(ClassRate("x", 29, 29).status(exact_interval(29, 29)[0], 30)
          == "ok",
          "a floor exactly at the interval's low is ok, because the whole "
          "interval is at or above it  <-- pinned defect")
    check(ClassRate("x", 3, 2).status(0.9, 30) == "too few rows",
          "2 of 3 is refused, because its interval reaches past the floor")
    check(ClassRate("x", 29, 29).status(0.5, 30) == "ok",
          "29 of 29 is ok under the minimum when its interval clears the "
          "floor")
    check(ClassRate("x").status(0.9, 30) == "too few rows",
          "a class with no rows is refused, not an interval error")
    check(ClassRate("x", 30, 27).status(0.9, 30) == "ok",
          "exactly the floor at exactly the row minimum passes, both are "
          "inclusive")
    check(ClassRate("x", 30, 26).status(0.9, 30) == "BELOW",
          "one row under the floor is BELOW")
    check(round(wilson_interval(26, 30)[1], 3) == 0.947,
          "though its interval reaches 94.7 percent: the gate reads the rate, "
          "not the interval")
    check(ClassRate("x").rate == 0.0,
          "a class with no rows reports 0, not a division error")
    check(repr(ClassRate("unit", 3, 2)) == "ClassRate('unit', 3, 2)",
          "a class prints its name and counts for debugging")

    # ---- the class table over an audited batch: 40 plain rows all TRUST and
    # 40 directional numbered rows with 4 TRUST. The aggregate is 55 percent.
    street = dict(good, match_type="StreetName")
    mixed = spread(40) + spread(4, good, 40) + spread(36, street, 44)
    addrs = (["%d MAIN ST" % (100 + i) for i in range(40)]
             + ["%d NE 31ST ST" % (100 + i) for i in range(40)])
    mrep = audit(mixed)
    table = class_table(addrs, mrep.results)
    check([r.name for r in table] == ["all", "directional", "numbered_street",
                                      "plain", "suffix:ST"],
          "the table prints all, then the classes in their fixed order, then "
          "the suffixes")
    check((table[0].rows, table[0].trusted) == (80, 44),
          "the all row counts every row once")
    check((table[1].rows, table[1].trusted) == (40, 4),
          "a class counts its own rows and their TRUST rows")
    check(table[-1].rows == 80,
          "a row counts once in every class it is in, so classes overlap")
    check(gate(mrep, 0.5) == 0,
          "an aggregate of 55 percent clears a floor of 50 percent")
    check([r.name for r in class_failures(table, 0.5)]
          == ["directional", "numbered_street"],
          "while two classes at 10 percent fail it  <-- pinned defect")
    check([r.name for r in class_failures(table, 0.5, min_rows=81)]
          == ["directional", "numbered_street"],
          "a row minimum above every class still fails the classes whose "
          "interval lies below the floor  <-- pinned defect")
    check(all(r.name != "all" for r in class_failures(table, 1.0)),
          "the all row is left to the aggregate gate, never failed twice")
    check(class_failures(table, 0.0) == [],
          "a floor of 0 fails no class")
    raises(lambda: class_failures(table, 1.5), "a class floor above 1 raises")
    raises(lambda: class_failures(table, 0.9, min_rows=0),
           "a row minimum of 0 raises")
    raises(lambda: class_table(addrs[:3], mrep.results),
           "a class table with fewer addresses than rows raises")
    raises(lambda: class_table(addrs + ["1 MAIN ST"], mrep.results),
           "a class table with more addresses than rows raises")
    sfx = class_table(["1 A AVE", "2 B CT", "3 C ST", "4 D CT", "5 E AVE",
                       "6 F ST", "7 G ST"], audit(spread(7)).results)
    check([r.name for r in sfx if r.name.startswith("suffix:")]
          == ["suffix:ST", "suffix:AVE", "suffix:CT"],
          "suffix classes print largest first, then by name")
    check(class_table([], [])[0].rows == 0,
          "an empty batch has an all row with no rows in it")

    # ---- the rendered class table
    lines = describe_classes(table, 0.5, 30)
    check(lines[0] == "address classes (TRUST rate by class, floor 50.0%, "
                      "30 rows to judge a class):",
          "the table names its floor and its row minimum")
    check(any(l.startswith("  directional") and "10.0%" in l
              and l.endswith("BELOW") for l in lines),
          "a failing class prints its rate and BELOW")
    check(any(l.startswith("  all") and l.endswith("aggregate") for l in lines),
          "the all row is marked as the aggregate, not judged here")
    small = describe_classes(class_table(addrs[:3], mrep.results[:3]), 0.5, 30)
    check(any(l.startswith("  plain") and l.endswith("too few rows")
              and l.split()[3] == "-" and "100.0%" in l for l in small),
          "a refused class prints its counts and interval, and no rate"
          "  <-- pinned defect")
    check(not any("|" in l for l in lines),
          "a table with no control has no control columns")
    ctl = class_table(addrs + ["PO BOX 1"], audit(spread(81)).results)
    both = describe_classes(table, 0.5, 30, ctl)
    check("gap" in both[1] and "|" in both[1],
          "a control adds its own columns and a gap to the header")
    check(any(l.startswith("  directional") and l.endswith("+90.0")
              for l in both),
          "the gap is the control's rate minus this file's, in points")
    check(any(l.startswith("  po_box") and "absent" in l for l in both),
          "a class only the control has is printed absent on this side")
    check(any(l.startswith("  po_box") and l.endswith("%") for l in both),
          "and gets no gap, because one side has too few rows")
    mine_only = describe_classes(ctl, 0.5, 30, table)
    check(any(l.startswith("  po_box") and "|     0" in l for l in mine_only),
          "a class the control lacks prints 0 rows on the control side")
    thin_gap = describe_classes([ClassRate("unit", 6, 3)], 0.5, 30,
                                [ClassRate("unit", 6, 6)])
    check(thin_gap[-1].startswith("  unit") and thin_gap[-1].endswith("%"),
          "a class on both sides but under the row minimum prints no gap")
    for mine_rate, ctl_rate, side in (
            (ClassRate("unit", 40, 5), ClassRate("unit", 5, 5), "the control"),
            (ClassRate("unit", 5, 0), ClassRate("unit", 40, 40), "this file")):
        one_side = describe_classes([mine_rate], 0.9, 30, [ctl_rate])
        check("+" not in one_side[-1].split("|")[1],
              "no gap when only %s is under the row minimum  <-- pinned defect"
              % side)

    # ---- real files on disk. Everything below writes into one temp directory,
    # which _sandbox() deletes again. No network, no database, no credentials.

    def tmpfile(name, text, encoding="utf-8"):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding=encoding) as handle:
            handle.write(text)
        return path

    def dicts(path):
        """A CSV on disk as a list of {column: cell} and its header."""
        rows, header, _ = read_csv(path)
        return [row_dict(header, r) for r in rows], header

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    # A ten row Esri batch: 5 TRUST, 3 SUSPECT, 2 REJECT, and one of the TRUST
    # rows sits outside the county boundary built further down.
    batch_text = (
        "Score,Addr_type,X,Y,Match_addr,USER_address,Status\n"
        "98,PointAddress,-60.30000,30.20000,100 MAIN ST,100 MAIN ST,M\n"
        "100,Zip5,-60.31000,30.21000,\"ANYTOWN, ZZ, 12345\",1234 NE 31ST ST,M\n"
        "97,PointAddress,-60.32000,30.22000,9100 NW 44TH ST,1234 NW 44TH ST,M\n"
        "88,PointAddress,-60.33000,30.23000,1300 SW 51ST AVE,1300 SW 51ST AVE,M\n"
        "61,PointAddress,-60.34000,30.24000,55 NE 62ND AVE,55 NE 62ND AVE,M\n"
        "98,StreetName,-60.35000,30.25000,NE 31ST ST,1234 NE 31ST ST,M\n"
        "99,PointAddress,-61.50000,30.20000,700 CEDAR ST,700 CEDAR ST,M\n"
        "98,PointAddress,-60.36000,30.26000,240 SW 9TH ST,240 SW 9TH ST,M\n"
        "96,PointAddress,-60.37000,30.27000,18 NE 12TH AVE,18 NE 12TH AVE,M\n"
        "95,PointAddress,-60.38000,30.28000,4110 NW 14TH ST,4110 NW 14TH ST,M\n")
    batch_csv = tmpfile("batch.csv", batch_text)

    code, out, err = run_cli(["--csv", batch_csv])
    check(code == 1, "the ten row batch fails the default 90% floor")
    check("TRUST rate: 50.0%" in out, "half of that batch is TRUST")
    check("FAIL: the batch is below" in out, "a failing batch says so in words")
    check(err == "", "a complete Esri header reports nothing missing")
    code, out, err = run_cli(["--csv", batch_csv, "--min-trust-rate", "0.5"])
    check(code == 0, "the same batch passes a floor it exactly meets")
    check("PASS: the batch clears" in out, "a passing batch says so in words")

    # ---- the UTF-8 BOM that Excel and Table To Table both write
    bom_csv = tmpfile("bom.csv", batch_text, encoding="utf-8-sig")
    bom_rows, bom_header, _ = read_csv(bom_csv)
    check(bom_header[0] == "Score",
          "a UTF-8 BOM is stripped from the first column name  <-- pinned defect")
    check(row_dict(bom_header, bom_rows[0])["Score"] == "98",
          "the score column of a BOM file is still addressable by name")
    check(run_cli(["--csv", bom_csv, "--min-trust-rate", "0.5"])[0] == 0,
          "a BOM file audits the same as one without  <-- pinned defect")

    # ---- a ragged CSV, written back out with the two audit columns
    ragged_csv = tmpfile(
        "ragged.csv",
        "Score,Addr_type,X,Y,Match_addr,USER_address\n"
        "98,PointAddress,-60.30000,30.20000,100 MAIN ST,100 MAIN ST,STRAY,MORE\n"
        "97,PointAddress,-60.31000\n")
    rag_rows, rag_header, _ = read_csv(ragged_csv)
    check(len(rag_rows[0]) == 8,
          "a row with surplus commas keeps every cell it holds")
    check("Y" not in row_dict(rag_header, rag_rows[1]),
          "a short row lacks its missing cells")
    esri = fields_for("esri")
    rag_results = audit([extract_row(row_dict(rag_header, r), esri)
                         for r in rag_rows]).results
    rag_out = os.path.join(tmp, "ragged-audited.csv")
    write_audited(rag_out, rag_rows, rag_header, rag_results)
    back_rows, back_header, _ = read_csv(rag_out)
    back = [row_dict(back_header, r) for r in back_rows]
    check(back_header == rag_header + list(ADDED_COLUMNS),
          "the audited copy keeps every input column and adds two")
    check(len(back_rows[0]) == len(back_header)
          and back[0]["gcs_verdict"] == TRUST,
          "the surplus cells of a ragged row do not reach the writer"
          "  <-- pinned defect")
    check(len(back_rows[1]) == len(back_header) and back[1]["Y"] == "",
          "a short row is padded, so its verdict stays in its own column")
    check(back[0]["gcs_verdict"] == TRUST and back[1]["gcs_verdict"] == REJECT,
          "each row is written with its own verdict")
    check("no usable coordinate" in back[1]["gcs_reasons"],
          "the reason for a verdict is written beside it")
    twice = os.path.join(tmp, "ragged-audited-twice.csv")
    write_audited(twice, back_rows, back_header,
                  audit([extract_row(r, esri) for r in back]).results)
    twice_rows, twice_header = dicts(twice)
    check(twice_header == back_header,
          "auditing an audited CSV does not add gcs_verdict twice"
          "  <-- pinned defect")
    check(twice_rows[0]["gcs_verdict"] == TRUST,
          "the second audit overwrites the first verdict instead of doubling it")

    # ---- --out and --apply
    out_path = os.path.join(tmp, "audited.csv")
    code, out, err = run_cli(["--csv", batch_csv, "--out", out_path,
                              "--min-trust-rate", "0.5"])
    check(not os.path.exists(out_path),
          "--out without --apply writes nothing at all  <-- pinned defect")
    check("was not written" in out, "--out without --apply says what it skipped")
    code, out, err = run_cli(["--csv", batch_csv, "--out", out_path, "--apply",
                              "--min-trust-rate", "0.5"])
    check(code == 0 and os.path.exists(out_path),
          "--apply writes the audited copy")
    check("wrote %s" % out_path in out, "the run names the file it wrote")
    written, written_header = dicts(out_path)
    check(len(written) == 10, "every input row is in the audited copy")
    check(written_header[-2:] == list(ADDED_COLUMNS),
          "the two audit columns are appended last")
    check([r["gcs_verdict"] for r in written].count(TRUST) == 5,
          "the verdicts in the file match the ones on the screen")
    check(written[1]["gcs_verdict"] == REJECT
          and "area fallback" in written[1]["gcs_reasons"],
          "the centroid row is written REJECT with its reason")
    check(written[0]["Match_addr"] == "100 MAIN ST",
          "the input columns are copied through unchanged")
    check(run_cli(["--csv", batch_csv, "--out", tmp, "--apply"])[0] == 2,
          "a write that fails exits 2, not the gate's 1")
    check("could not write" in run_cli(["--csv", batch_csv, "--out", tmp,
                                        "--apply"])[2],
          "and says which write failed, not that the tool crashed")

    # ---- a county boundary from a GeoJSON file on disk, lake included
    county = [[-60.40, 30.05], [-59.90, 30.05], [-59.90, 30.45],
              [-60.05, 30.45], [-60.05, 30.30], [-60.40, 30.30]]
    lake = [[-60.20, 30.15], [-60.10, 30.15], [-60.10, 30.22], [-60.20, 30.22]]
    county_path = tmpfile("county.geojson", json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"NAME": "Example"},
             "geometry": {"type": "Polygon",
                          "coordinates": [county, lake]}}]}),
        encoding="utf-8-sig")
    polys = read_boundary(county_path)
    check(len(polys) == 1 and len(polys[0]) == 2,
          "the boundary file reads back as one polygon with one hole")
    check(point_in_any(-60.30, 30.20, polys) is True,
          "a point in the body of the county is contained")
    check(point_in_any(-59.95, 30.40, polys) is True,
          "a point in the county's narrow northern arm is contained")
    check(point_in_any(-60.15, 30.18, polys) is False,
          "a point in the middle of the lake is not contained")
    check(point_in_any(-60.30, 30.40, polys) is False,
          "a point in the notch beside the arm is not contained")
    check(point_in_any(-61.50, 30.20, polys) is False,
          "a point in the next county is not contained")
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", county_path,
                              "--min-trust-rate", "0.4"])
    check(code == 0, "the batch clears a floor set for the boundary run")
    check("TRUST rate: 40.0%" in out,
          "the row outside the county drops the rate from 50% to 40%")
    outside_path = os.path.join(tmp, "boundary-audited.csv")
    run_cli(["--csv", batch_csv, "--boundary", county_path, "--out",
             outside_path, "--apply", "--min-trust-rate", "0.4"])
    bounded, _ = dicts(outside_path)
    check(bounded[6]["gcs_verdict"] == REJECT
          and "falls outside the boundary" in bounded[6]["gcs_reasons"],
          "the row that left the county is written REJECT with that reason")
    check(bounded[0]["gcs_verdict"] == TRUST,
          "a row inside the county keeps the verdict its score earned")
    bad_json = tmpfile("bad.geojson", "{not json at all")
    check(run_cli(["--csv", batch_csv, "--boundary", bad_json])[0] == 2,
          "a boundary that is not JSON exits 2, not the gate's 1")
    empty_fc = tmpfile("empty.geojson",
                       '{"type": "FeatureCollection", "features": []}')
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", empty_fc])
    check(code == 2 and "holds no polygon" in err,
          "a boundary file holding no polygon exits 2 as unreadable input, "
          "not 64  <-- pinned defect")
    null_geom = tmpfile("null.geojson",
                        '{"type": "FeatureCollection", "features": [{"type": '
                        '"Feature", "geometry": null, "properties": {}}]}')
    check(run_cli(["--csv", batch_csv, "--boundary", null_geom])[0] == 2,
          "a boundary whose only feature has no geometry exits 2 too"
          "  <-- pinned defect")

    # ---- the per-field overrides, end to end on a CSV that names nothing the
    # profile expects
    odd_csv = tmpfile(
        "odd.csv",
        "CONF,KIND,LON,LAT,OUT_ADDR,IN_ADDR,FLAG\n"
        "98,PointAddress,-60.30000,30.20000,100 MAIN ST,100 MAIN ST,M\n"
        "100,Zip5,-60.31000,30.21000,\"ANYTOWN, ZZ\",1234 NE 31ST ST,M\n")
    code, out, err = run_cli(["--csv", odd_csv, "--min-trust-rate", "0.5"])
    check(code == 2 and out == "",
          "a CSV with none of the required columns exits 2, not the gate's 1"
          "  <-- pinned defect")
    check("required columns not in the CSV: Addr_type, Score, X, Y" in err,
          "the required columns that are absent are named, not just counted")
    code, out, err = run_cli(["--csv", odd_csv, "--min-trust-rate", "0.5",
                              "--score-field", "CONF",
                              "--match-type-field", "KIND",
                              "--x-field", "LON", "--y-field", "LAT",
                              "--address-field", "IN_ADDR",
                              "--matched-address-field", "OUT_ADDR",
                              "--status-field", "FLAG"])
    check(code == 0, "the seven field overrides make the same CSV auditable")
    check("TRUST rate: 50.0%" in out,
          "the overridden columns produce the verdicts the data deserves")
    check(err == "", "nothing is reported missing once the overrides name it")

    # ---- the pile-up flags, over a batch that is mostly one point
    stack_csv = tmpfile("stacked.csv", "Score,Addr_type,X,Y\n"
                        + "98,PointAddress,-60.14000,30.18700\n" * 6)
    code, out, err = run_cli(["--csv", stack_csv])
    check(code == 1, "a batch stacked on one point fails the gate")
    check("note: Match_addr not in the CSV, so the house-number check is "
          "skipped" in err
          and "note: Status not in the CSV, so a tied match cannot be caught"
          in err,
          "each absent optional column is a note naming the check it skips")
    check("6 record(s) on -60.14, 30.187" in out,
          "the CLI names the pile-up coordinate and its count")
    check("coordinate pile-ups" not in run_cli(
              ["--csv", stack_csv, "--pileup-threshold", "7"])[1],
          "--pileup-threshold raises the count a pile-up needs")
    check("6 record(s) on" in run_cli(
              ["--csv", stack_csv, "--pileup-threshold", "2"])[1],
          "--pileup-threshold 2 is accepted, it is the smallest pile-up there "
          "is")
    near_csv = tmpfile("near.csv", "Score,Addr_type,X,Y\n"
                       + "98,PointAddress,-60.30000,30.20000\n" * 3
                       + "98,PointAddress,-60.3000001,30.2000001\n" * 3)
    check("coordinate pile-ups" in run_cli(["--csv", near_csv])[1],
          "coordinates inside the rounding tolerance are one pile-up")
    check("coordinate pile-ups" not in run_cli(
              ["--csv", near_csv, "--pileup-precision", "7"])[1],
          "--pileup-precision splits those coordinates back apart")

    # ---- the score and house number flags, end to end
    check("TRUST rate: 60.0%" in run_cli(
              ["--csv", batch_csv, "--trust-score", "85"])[1],
          "--trust-score lets the row at 88 through")
    check(("  %-8s %6d" % (REJECT, 1)) in run_cli(
              ["--csv", batch_csv, "--suspect-score", "60"])[1],
          "--suspect-score keeps the row at 61 out of REJECT")
    check("TRUST rate: 60.0%" in run_cli(
              ["--csv", batch_csv, "--house-number-tolerance", "9000"])[1],
          "--house-number-tolerance stops flagging a far house number")
    check("house number 1234 was matched to 9100" in run_cli(
              ["--csv", batch_csv])[1],
          "the default tolerance reports that house number difference")

    # ---- the other two profiles, through the CLI, on files in their shape
    census_csv = tmpfile(
        "census.csv",
        "input_address,matched_address,lon,lat,match_type\n"
        "1234 NE 31ST ST ANYTOWN ZZ,\"1234 NE 31ST ST, ANYTOWN, ZZ, 12345\","
        "-60.12650,30.17860,Exact\n"
        "55 NE 62ND AVE ANYTOWN ZZ,\"57 NE 62ND AVE, ANYTOWN, ZZ, 12346\","
        "-60.14010,30.18990,Non_Exact\n"
        "PO BOX 1234 ANYTOWN ZZ,,,,No_Match\n"
        "100 MAIN ST ANYTOWN ZZ,\"100 MAIN ST, ANYTOWN, ZZ\","
        "-60.13000,30.18700,Tie\n")
    code, out, err = run_cli(["--csv", census_csv, "--profile", "census",
                              "--min-trust-rate", "0.25"])
    check(code == 0, "a census-shaped batch audits through the CLI")
    check("TRUST rate: 25.0%" in out, "the census batch scores 25% TRUST")
    check(err == "", "the census profile asks for no score column")
    check("not an address-level match" in out,
          "the census refusal reads as a sentence with no score in it")
    raw_census = tmpfile(
        "raw-census.csv",
        "id,input_address,match,match_type,matched_address,coordinates,"
        "tiger_id,side\n"
        "\"1\",\"100 MAIN ST, ANYTOWN, ZZ\",\"Match\",\"Exact\","
        "\"100 MAIN ST, ANYTOWN, ZZ\",\"-60.31000,30.21000\",\"1\",\"L\"\n")
    code, out, err = run_cli(["--csv", raw_census, "--profile", "census"])
    check(code == 2 and "lat, lon" in err,
          "a Census file in its own layout, header added, exits 2 until it "
          "is reshaped  <-- pinned defect")
    nomi_csv = tmpfile(
        "nominatim.csv",
        "query,display_name,lon,lat,addresstype,importance\n"
        "1234 NE 31ST ST,\"1234, NE 31st Street, Anytown\","
        "-60.12650,30.17860,house,0.45\n"
        "ANYTOWN ZZ,\"Anytown, Example County, Anystate\","
        "-60.14010,30.18720,city,0.82\n"
        "NE 31ST ST,\"Northeast 31st Street, Anytown\","
        "-60.13000,30.17900,road,0.30\n"
        "12345,\"12345, Anytown, Anystate\",-60.12000,30.16000,postcode,0.20\n")
    code, out, err = run_cli(["--csv", nomi_csv, "--profile", "nominatim",
                              "--min-trust-rate", "0.25"])
    check(code == 0, "a real nominatim batch audits through the CLI")
    check("TRUST rate: 25.0%" in out, "the nominatim batch scores 25% TRUST")
    check("The score 0.82 says how well that AREA matched" in out,
          "importance is reported as what it measured, the city it matched")
    for floor, rate, label in (
            ("0.5", "0.0", "--trust-score 0.5 is an importance, so the house "
                           "at 0.45 is not TRUST  <-- pinned defect"),
            ("0.45", "25.0", "a house exactly on an importance floor of 0.45 "
                             "is TRUST")):
        out = run_cli(["--csv", nomi_csv, "--profile", "nominatim",
                       "--trust-score", floor, "--min-trust-rate", "0"])[1]
        check("TRUST rate: %s%%" % rate in out, label)
    code, out, err = run_cli(["--csv", nomi_csv, "--profile", "nominatim",
                              "--trust-score", "0.2", "--suspect-score", "0.5"])
    check(code == 64
          and "the reject floor 0.5 is above the trust floor 0.2" in err,
          "floors in the wrong order are named on the importance scale")

    # ---- the exits a scheduled job reads
    check(run_cli([])[0] == 64, "a run with no CSV is a usage error")
    check(run_cli(["--csv", batch_csv, "--apply"])[0] == 64,
          "--apply without --out is a usage error")
    code, out, err = run_cli(["--csv", batch_csv, "--pileup-threshold", "1"])
    check(code == 64 and "--pileup-threshold must be at least 2" in err,
          "a pile-up threshold of 1 is a usage error")
    check(run_cli(["--csv", batch_csv, "--min-trust-rate", "2"])[0] == 64,
          "a trust floor above 1 is a usage error")
    check(run_cli(["--csv", batch_csv, "--min-trust-rate", "1"])[0] == 1,
          "a floor of 1 is accepted and this batch does not clear it")
    check(run_cli(["--csv", batch_csv, "--min-trust-rate", "0"])[0] == 0,
          "a floor of 0 is accepted and nothing can fail it")
    argv_before = sys.argv
    try:
        sys.argv = ["geocodesift.py", batch_csv, "--min-trust-rate", "0.5"]
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before
    check(run_cli(["--csv", batch_csv, "--trust-score", "50",
                   "--suspect-score", "80"])[0] == 64,
          "floors in the wrong order are a usage error, not a traceback")
    check(run_cli(["--csv", os.path.join(tmp, "absent.csv")])[0] == 2,
          "a CSV that is not there exits 2, not the gate's 1")
    check(run_cli(["--csv", tmp])[0] == 2,
          "a directory where a CSV should be exits 2")
    huge_csv = tmpfile("huge.csv",
                       "Score,Addr_type,X,Y,Match_addr\n"
                       "98,PointAddress,-60.3,30.2,\"%s\"\n" % ("A" * 200000))
    check(run_cli(["--csv", huge_csv])[0] == 2,
          "a CSV field over the csv module's own limit exits 2, not 1"
          "  <-- pinned defect")
    empty_csv = tmpfile("empty.csv", "")
    code, out, err = run_cli(["--csv", empty_csv, "--min-trust-rate", "0"])
    check(code == 2 and "required columns" in err,
          "a CSV with no header row cannot be read and exits 2")
    header_only = tmpfile("header-only.csv", "Score,Addr_type,X,Y\n")
    code, out, err = run_cli(["--csv", header_only])
    check(code == 1 and "rows audited: 0" in out and "holds no rows" in out,
          "a CSV with a header and no rows fails the gate")
    code, out, err = run_cli(["--csv", header_only, "--min-trust-rate", "0"])
    check(code == 1 and "PASS" not in out,
          "and fails it at a floor of 0 too  <-- pinned defect")
    for argv, label in (
            ([os.path.join(tmp, "absent.csv")], "a CSV that is not there"),
            ([huge_csv], "a CSV field over the limit")):
        err = run_cli(["--csv"] + argv)[2]
        check(err.startswith("error: ") and "failure of the tool" not in err,
              "%s is reported as an input error, not a crash" % label)
    empty_ctl = tmpfile("empty-control.csv", batch_text.split("\n")[0] + "\n")
    code, out, err = run_cli(["--csv", batch_csv, "--compare", empty_ctl])
    check(code == 2 and "holds no rows, so there is nothing to compare" in err,
          "a --compare CSV with no rows exits 2, not a table of dashes"
          "  <-- pinned defect")
    blank_csv = tmpfile("blank-lines.csv",
                        batch_text.replace("\n98,StreetName",
                                           "\n\n98,StreetName") + "\n\n")
    code, out, err = run_cli(["--csv", blank_csv])
    check("rows audited: 10" in out,
          "blank lines in a CSV are not rows  <-- pinned defect")
    check("  row 7 SUSPECT" in run_cli(["--csv", batch_csv])[1]
          and "  row 8 SUSPECT" in out and "  row 7 SUSPECT" not in out,
          "a row is numbered as the file numbers it, blank lines counted"
          "  <-- pinned defect")

    # ---- --by-class and --compare, end to end. The vendor file holds 40
    # plain rows, all TRUST, and 40 directional numbered-street rows of which
    # 4 are TRUST: an aggregate of 55 percent over a class at 10 percent.
    def esri_line(i, addr, ok):
        return ("%s,%s,%.5f,%.5f,%s,%s\n"
                % ("98" if ok else "88",
                   "PointAddress" if ok else "StreetName",
                   -60.3 - i * 0.01, 30.3 + i * 0.01,
                   addr if ok else addr.split(" ", 1)[1], addr))
    head = "Score,Addr_type,X,Y,Match_addr,USER_address\n"
    vendor_text = head + "".join(
        esri_line(i, a, i < 44) for i, a in enumerate(addrs))
    vendor_csv = tmpfile("vendor.csv", vendor_text)
    code, out, err = run_cli(["--csv", vendor_csv, "--min-trust-rate", "0.5"])
    check(code == 0 and "address classes" not in out,
          "without --by-class the aggregate passes the batch the classes fail")
    code, out, err = run_cli(["--csv", vendor_csv, "--min-trust-rate", "0.5",
                              "--by-class"])
    check(code == 1, "with --by-class the same batch fails  <-- pinned defect")
    check("FAIL: the batch clears the trust floor, but 2 address class(es) do "
          "not: directional 10.0%, numbered_street 10.0%." in out,
          "the refusal says the aggregate cleared and names each class below")
    check("  directional         40      4   10.0%" in out,
          "the class table is printed with its counts and rate")
    code, out, err = run_cli(["--csv", vendor_csv, "--min-trust-rate", "0.5",
                              "--by-class", "--min-class-rows", "41"])
    check(code == 1 and "do not: directional 10.0%, numbered_street 10.0%."
          in out,
          "a class under --min-class-rows whose interval lies below the floor "
          "fails the batch, not a silent PASS  <-- pinned defect")
    code, out, err = run_cli(["--csv", vendor_csv, "--min-trust-rate", "0.05",
                              "--by-class", "--min-class-rows", "41"])
    check(code == 0 and "PASS: the batch clears the trust floor. 2 address "
          "class(es) had too few rows to judge: directional, "
          "numbered_street." in out,
          "a PASS names every class it refused to judge  <-- pinned defect")
    small_csv = tmpfile("small-class.csv", head + "".join(
        [esri_line(i, "%d MAIN ST" % (100 + i), True) for i in range(100)]
        + [esri_line(100 + i, "%d N OAK ST" % (100 + i), i < 8)
           for i in range(10)]))
    code, out, err = run_cli(["--csv", small_csv, "--by-class"])
    check(code == 0 and "too few rows to judge: directional" in out,
          "a class of 10 at 80 percent is refused under the default of 30")
    code, out, err = run_cli(["--csv", small_csv, "--by-class",
                              "--min-class-rows", "5"])
    check(code == 1 and "do not: directional 80.0%." in out,
          "--min-class-rows 5 reaches the gate and fails that class"
          "  <-- pinned defect")
    code, out, err = run_cli(["--csv", vendor_csv, "--by-class"])
    check(code == 1 and "are below it as well: directional 10.0%" in out,
          "a batch below the aggregate floor names its failing classes too")
    code, out, err = run_cli(["--csv", vendor_csv, "--by-class",
                              "--min-trust-rate", "0.05"])
    check(code == 0 and "PASS: the batch clears" in out,
          "a batch whose every class clears the floor passes --by-class")

    control_text = ("input_address,matched_address,lon,lat,match_type\n"
                    + "".join("\"%s, ANYTOWN, ZZ\",\"%s\",%.5f,%.5f,Exact\n"
                              % (a, a, -60.3 - i * 0.01, 30.3 + i * 0.01)
                              for i, a in enumerate(addrs)))
    control_csv = tmpfile("control.csv", control_text)
    code, out, err = run_cli(["--csv", vendor_csv, "--min-trust-rate", "0.5",
                              "--compare", control_csv,
                              "--compare-profile", "census"])
    check(code == 1 and "address classes" in out,
          "--compare implies --by-class and still gates this file")
    check("control file %s: 80 rows, TRUST rate 100.0%% (80 TRUST, 0 "
          "SUSPECT, 0 REJECT)" % control_csv in out,
          "the control is audited and its aggregate and counts printed")
    check(any(l.startswith("  directional") and l.endswith("+90.0")
              for l in out.splitlines()),
          "the control's class rates print beside this file's with the gap")
    code, out, err = run_cli(["--csv", vendor_csv, "--compare", vendor_csv,
                              "--min-trust-rate", "0.5"])
    check(any(l.startswith("  directional") and l.endswith("+0.0")
              for l in out.splitlines()),
          "without --compare-profile the control is read with --profile")
    check(run_cli(["--csv", vendor_csv, "--compare",
                   os.path.join(tmp, "absent.csv")])[0] == 2,
          "a --compare file that is not there exits 2")
    no_addr = tmpfile("no-address.csv", "match_type,lon,lat\nExact,1,2\n")
    code, out, err = run_cli(["--csv", vendor_csv, "--compare", no_addr,
                              "--compare-profile", "census"])
    check(code == 2 and "'input_address' column" in err,
          "a --compare file with no address column exits 2 and names it")
    code, out, err = run_cli(["--csv", no_addr, "--profile", "census",
                              "--by-class"])
    check(code == 2 and "--address-field" in err,
          "--by-class on a CSV with no address column exits 2 and names the "
          "override")
    code, out, err = run_cli(["--csv", no_addr, "--profile", "census"])
    check(code != 2, "the address column is only required by --by-class")
    code, out, err = run_cli(["--csv", odd_csv, "--by-class",
                              "--score-field", "CONF",
                              "--match-type-field", "KIND",
                              "--x-field", "LON", "--y-field", "LAT",
                              "--address-field", "IN_ADDR",
                              "--matched-address-field", "OUT_ADDR"])
    check("address classes" in out and "  plain" in out,
          "--address-field names the column --by-class classifies")
    thin = tmpfile("thin-control.csv", "input_address\n100 MAIN ST\n")
    code, out, err = run_cli(["--csv", vendor_csv, "--compare", thin,
                              "--compare-profile", "census"])
    check(code == 2 and "error: required columns not in the --compare CSV: "
          "lat, lon, match_type" in err and "PASS" not in out,
          "a --compare file missing required columns exits 2, not a control "
          "at 0 percent  <-- pinned defect")
    far_control = tmpfile(
        "far-control.csv",
        "input_address,matched_address,lon,lat,match_type\n"
        "100 MAIN ST,100 MAIN ST,-61.50000,30.20000,Exact\n")
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", county_path,
                              "--compare", far_control,
                              "--compare-profile", "census"])
    check("TRUST rate 0.0%" in out,
          "the control is held to the same boundary as this file")
    check(run_cli(["--csv", batch_csv, "--by-class",
                   "--min-class-rows", "0"])[0] == 64,
          "a --min-class-rows of 0 is a usage error")
    blank_addr = tmpfile("blank-address.csv", head + "".join(
        esri_line(i, " ", True) for i in range(40)))
    code, out, err = run_cli(["--csv", blank_addr, "--by-class"])
    check(code == 2 and "every submitted address in the CSV is blank" in err
          and "PASS" not in out,
          "--by-class on an address column that is blank in every row exits "
          "2, not PASS  <-- pinned defect")
    code, out, err = run_cli(["--csv", vendor_csv, "--compare", blank_addr])
    check(code == 2 and "in the --compare CSV is blank" in err,
          "a --compare file whose addresses are all blank exits 2 as well")
    check(run_cli(["--csv", blank_addr])[0] == 0,
          "a blank address column is only refused under --by-class")
    part_blank = tmpfile("part-blank.csv", head + "".join(
        esri_line(i, a, True) for i, a in enumerate(
            ["%d MAIN ST" % (100 + i) for i in range(40)] + [""])))
    code, out, err = run_cli(["--csv", part_blank, "--by-class"])
    check(code == 0 and "rows audited: 41" in out and "  blank" in out,
          "one blank address among good ones is a blank class, not a "
          "refused batch  <-- pinned defect")
    big_addr = tmpfile("big-address.csv", head + "".join(
        esri_line(i, a, True) for i, a in enumerate(
            ["%d MAIN ST" % (100 + i) for i in range(40)]
            + ["CR " + "9" * 5000])))
    code, out, err = run_cli(["--csv", big_addr, "--by-class"])
    check(code == 0 and "rows audited: 41" in out,
          "a cell of 5000 digits is classified, not a traceback that exits 1"
          "  <-- pinned defect")

    # ---- the environment default. _sandbox() set the operator's value aside
    # and puts it back at the end, so these checks start from no variable.
    os.environ["GEOCODESIFT_MIN_TRUST_RATE"] = "junk"
    inside = None
    try:
        with _sandbox() as box:
            inside = ("GEOCODESIFT_MIN_TRUST_RATE" in os.environ,
                      os.path.isdir(box))
            raise RuntimeError("planted")
    except RuntimeError:
        pass
    check(inside == (False, True) and not os.path.exists(box)
          and os.environ.get("GEOCODESIFT_MIN_TRUST_RATE") == "junk",
          "the self-test sets an exported floor aside, and removes its "
          "directory and restores the floor after a crash  <-- pinned defect")
    os.environ["GEOCODESIFT_MIN_TRUST_RATE"] = "0.10"
    check(_parse(["--csv", "g.csv"]).min_trust_rate == 0.10,
          "the environment sets the trust floor when no flag does")
    check(_parse(["--csv", "g.csv", "--min-trust-rate", "0.5"]
                 ).min_trust_rate == 0.5, "the flag wins over the environment")
    check(run_cli(["--csv", batch_csv])[0] == 0,
          "a batch that clears the environment's floor passes")
    os.environ["GEOCODESIFT_MIN_TRUST_RATE"] = "ninety percent"
    check(env_min_trust_rate() == DEFAULT_MIN_TRUST_RATE,
          "an unreadable floor in the environment does not crash the parser"
          "  <-- pinned defect")
    check(run_cli(["--csv", batch_csv])[0] == 64,
          "an unreadable floor in the environment is refused, not ignored"
          "  <-- pinned defect")
    os.environ.pop("GEOCODESIFT_MIN_TRUST_RATE", None)

    # ---- the control file is judged by its own score floors, and by the
    # boundary, pile-up and house-number settings of this run
    two_ctl = tmpfile(
        "floors-control.csv",
        "Score,Addr_type,X,Y,Match_addr,USER_address\n"
        "88,PointAddress,-60.30000,30.20000,1 MAIN ST,1 MAIN ST\n"
        "70,PointAddress,-60.31000,30.21000,2 MAIN ST,2 MAIN ST\n")
    code, out, err = run_cli(["--csv", batch_csv, "--compare", two_ctl,
                              "--trust-score", "85", "--suspect-score", "60"])
    check("TRUST rate: 60.0%" in out
          and "(0 TRUST, 1 SUSPECT, 1 REJECT)" in out,
          "--trust-score and --suspect-score move this file's verdicts and "
          "leave the control on its profile's floors")
    near_ctl = tmpfile(
        "settings-control.csv",
        "Score,Addr_type,X,Y,Match_addr,USER_address\n"
        + "98,PointAddress,-60.30000,30.20000,1 MAIN ST,1 MAIN ST\n" * 3
        + "98,PointAddress,-60.3000001,30.2000001,1 MAIN ST,1 MAIN ST\n" * 3
        + "98,PointAddress,-60.40000,30.30000,9100 MAIN ST,1234 MAIN ST\n")
    for flags, trusted, label in (
            ([], 0, "the control's pile-up and far house number are caught"),
            (["--pileup-threshold", "7"], 6,
             "--pileup-threshold applies to the control"),
            (["--pileup-precision", "7"], 6,
             "--pileup-precision applies to the control"),
            (["--house-number-tolerance", "9000"], 1,
             "--house-number-tolerance applies to the control")):
        code, out, err = run_cli(["--csv", batch_csv, "--compare", near_ctl]
                                 + flags)
        check("control file %s: 7 rows" % near_ctl in out
              and "(%d TRUST," % trusted in out, label)

    # ---- the score floors are real numbers or a usage error
    for flag in ("--trust-score", "--suspect-score"):
        code, out, err = run_cli(["--csv", batch_csv, flag, "nan",
                                  "--min-trust-rate", "0"])
        check(code == 64 and "PASS" not in out,
              "%s nan is a usage error, not a floor every row clears"
              "  <-- pinned defect" % flag)

    # ---- one bad cell is a refused row, not a stopped batch
    neg_csv = tmpfile("negative.csv", "Score,Addr_type,X,Y\n"
                      "98,PointAddress,-60.30000,30.20000\n"
                      "-1,PointAddress,-60.31000,30.21000\n")
    code, out, err = run_cli(["--csv", neg_csv])
    check(code == 1 and "rows audited: 2" in out
          and "row 3 REJECT: score -1 is negative" in out,
          "a negative score cell is REJECT on its row and the batch is still "
          "audited  <-- pinned defect")

    # ---- the Esri tie, end to end
    tie_csv = tmpfile("tie.csv", "Score,Addr_type,X,Y,Status\n"
                      "100,PointAddress,-60.30000,30.20000,T\n")
    code, out, err = run_cli(["--csv", tie_csv])
    check(code == 1 and "REJECT: status T is a tie" in out,
          "an Esri tie fails the batch that it passed before  <-- pinned "
          "defect")

    # ---- bytes that are not UTF-8, and repeated header names, survive --apply
    cp_path = os.path.join(tmp, "cp1252.csv")
    cp_row = u"98,PointAddress,-60.3,30.2,100 CAF\u00c9 ST,100 CAF\u00c9 ST"
    with open(cp_path, "wb") as handle:
        handle.write((u"Score,Addr_type,X,Y,Match_addr,USER_address\r\n"
                      + cp_row + u"\r\n").encode("cp1252"))
    cp_out = os.path.join(tmp, "cp1252-audited.csv")
    code, out, err = run_cli(["--csv", cp_path, "--out", cp_out, "--apply"])
    with open(cp_out, "rb") as handle:
        cp_bytes = handle.read()
    check(code == 0 and (cp_row.encode("cp1252") + b",TRUST,") in cp_bytes,
          "a cp1252 byte is written back as the same byte, not U+FFFD"
          "  <-- pinned defect")
    dup_csv = tmpfile("blank-headers.csv", "Score,Addr_type,X,Y,,\n"
                      "98,PointAddress,-60.3,30.2,keepme,other\n")
    dup_out = os.path.join(tmp, "blank-headers-audited.csv")
    run_cli(["--csv", dup_csv, "--out", dup_out, "--apply"])
    check(read_csv(dup_out)[0][0][4:6] == ["keepme", "other"],
          "two blank header cells keep their own values in the audited copy"
          "  <-- pinned defect")
    two_scores = tmpfile("two-scores.csv", "Score,Addr_type,X,Y,Score,USER_address\n"
                         "98,PointAddress,-60.3,30.2,40,1 MAIN ST\n")
    code, out, err = run_cli(["--csv", two_scores])
    check(code == 2 and "names Score more than once" in err,
          "a score column named twice exits 2 rather than reading the last"
          "  <-- pinned defect")
    code, out, err = run_cli(["--csv", batch_csv, "--compare", two_scores,
                              "--compare-profile", "esri"])
    check(code == 2 and "--compare CSV names Score more than once" in err,
          "a --compare file with a repeated column exits 2 as well")

    # ---- --out never names an input
    self_csv = tmpfile("self.csv", batch_text)
    for flags, label in (
            (["--csv", self_csv, "--out", self_csv],
             "--out naming the --csv file"),
            (["--csv", batch_csv, "--boundary", county_path, "--out",
              county_path], "--out naming the --boundary file"),
            (["--csv", batch_csv, "--compare", self_csv, "--out", self_csv],
             "--out naming the --compare file")):
        code, out, err = run_cli(flags + ["--apply", "--min-trust-rate", "0"])
        check(code == 64 and "--out names the" in err,
              "%s is a usage error  <-- pinned defect" % label)
    link_csv = os.path.join(tmp, "self-link.csv")
    os.link(self_csv, link_csv)
    code, out, err = run_cli(["--csv", self_csv, "--out", link_csv, "--apply"])
    check(code == 64 and "--out names the" in err,
          "--out naming a hard link to the --csv file is a usage error")
    with open(self_csv, "r", newline="", encoding="utf-8") as handle:
        check(handle.read() == batch_text,
              "and the input is left exactly as it was")

    # ---- inputs that cannot be read exit 2, never the gate's 1
    u16 = tmpfile("utf16.csv", "Score,Addr_type,X,Y\n98,PointAddress,1,1\n",
                  encoding="utf-16")
    check(run_cli(["--csv", u16])[0] == 2,
          "a UTF-16 CSV exits 2, not 1  <-- pinned defect")
    bin_path = os.path.join(tmp, "binary.csv")
    with open(bin_path, "wb") as handle:
        handle.write(bytes(bytearray(range(1, 256))) * 4)
    check(run_cli(["--csv", bin_path])[0] == 2,
          "a binary file exits 2, not 1  <-- pinned defect")
    no_coords = tmpfile("no-coords.geojson", '{"type": "Polygon"}')
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", no_coords])
    check(code == 2 and "needs a list of rings" in err,
          "a Polygon with no coordinates exits 2, not a traceback"
          "  <-- pinned defect")
    deep = tmpfile("deep.geojson",
                   '{"type": "GeometryCollection", "geometries": [' * 5000
                   + "]}" * 5000)
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", deep])
    check(code == 2 and "nests too deeply" in err,
          "a boundary nested 5000 deep exits 2, not a RecursionError"
          "  <-- pinned defect")

    # ---- a redirected stdout that is not UTF-8, as a scheduled job's is on
    # Windows. The match type is echoed in a reason through %r.
    omega = tmpfile("omega.csv", "Score,Addr_type,X,Y\n" + "".join(
        "98,PointAddress,%.5f,30.20000\n" % (-60.3 - i * 0.01)
        for i in range(20)) + u"98,Point\u03a9,-61.00000,30.20000\n")
    raw_out, err = io.BytesIO(), io.StringIO()
    cp_stdout = io.TextIOWrapper(raw_out, encoding="cp1252")
    with contextlib.redirect_stdout(cp_stdout):
        with contextlib.redirect_stderr(err):
            code = main(["--csv", omega])
    cp_stdout.flush()
    check(code == 0 and b"match type 'Point\\u03a9'" in raw_out.getvalue(),
          "a match type outside cp1252 is escaped on a cp1252 stdout, and the "
          "batch still passes  <-- pinned defect")

    # ---- argparse's own errors are usage errors, exit 64, not its exit 2
    for argv, label in ((["--pileup-threshold", "abc"], "a bad integer"),
                        (["--min-trust-rate", "abc"], "a bad number"),
                        (["--profile", "bogus"], "a bad --profile"),
                        (["--nonsense-flag", "x"], "an unknown flag")):
        err, got = io.StringIO(), None
        try:
            with contextlib.redirect_stderr(err):
                main(["--csv", batch_csv] + argv)
        except SystemExit as exc:
            got = exc.code
        check(got == 64 and "error:" in err.getvalue(),
              "%s exits 64, not argparse's 2  <-- pinned defect" % label)

    # ---- a crash inside the tool exits 2, never the gate's 1
    real_describe = describe
    def broken(*a, **k):
        raise RuntimeError("planted")
    globals()["describe"] = broken
    try:
        code, out, err = run_cli(["--csv", batch_csv])
    finally:
        globals()["describe"] = real_describe
    check(code == 2 and "RuntimeError: planted" in err
          and "failure of the tool" in err,
          "an unexpected error exits 2 and says it is the tool's failure"
          "  <-- pinned defect")

    # ---- the harness itself, on its red path. Every PASS above means only as
    # much as the harness's ability to print FAIL, so a second harness is fed
    # a false check, a call that raises nothing and a call that raises the
    # wrong error, and must count all three.
    scratch = _Harness()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scratch.check(True, "a true check")
        scratch.check(False, "a false check")
        scratch.raises(lambda: None, "a call that raises nothing")
        scratch.raises(lambda: {}["absent"], "a call that raises KeyError")
        scratch_code = scratch.summary()
    said = buf.getvalue()
    check(scratch_code == 1 and len(scratch.failed) == 3,
          "the harness counts a false check, a missing error and a wrong "
          "error as three failures")
    check("4 assertions, 3 failed" in said,
          "and its footer reports them, not 0 failed")
    check("FAILED: a call that raises nothing (no error raised)" in said
          and "a call that raises KeyError (wrong exception" in said,
          "and names each failure with the reason it failed")

    # ---- importing the module runs nothing. An ArcGIS script tool or another
    # script may import it for the core, and must not start an audit.
    spec = importlib.util.spec_from_file_location(
        "geocodesift_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    # No bytecode cache. The import would otherwise write a __pycache__
    # folder beside the script, and the self-test writes nothing outside its
    # own temporary directory.
    pyc = importlib.util.cache_from_source(os.path.abspath(__file__))
    pyc_before = os.path.exists(pyc)
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.address_classes("PO BOX 1")
          == ["po_box"],
          "importing the module prints nothing and exposes the core")
    check(os.path.exists(pyc) == pyc_before,
          "and writes no bytecode cache beside the script")

    return harness.summary()


# ----------------------------------------------------------------------- cli

# Bytes that are not UTF-8 survive a read and a write unchanged. Excel on
# Windows writes cp1252, and errors="replace" turned every accented byte into
# U+FFFD in the --apply copy, silently. surrogateescape carries each such byte
# through as a lone surrogate and writes the same byte back.
_BYTES = "surrogateescape"


def read_csv(path):
    """Rows, the header and the row numbers of a CSV. A row is a list of cells.

    Lists, not dicts. A header with a repeated name, such as the several
    blank cells an Excel export ends with, gave one dict key to two columns,
    and the --apply copy wrote the last column's value into both.
    """
    # utf-8-sig, because Esri's Table To Table and Excel both write a UTF-8
    # BOM. Read as plain UTF-8 the first header cell arrives as "\ufeffScore",
    # the score column reads as absent on every row, and a clean batch fails
    # the gate for a reason nothing in the output explains.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors=_BYTES) as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        # A blank line is skipped, as csv.DictReader skips it. Kept, it was a
        # row with no coordinate, REJECT, and trailing blank lines failed a
        # clean batch. Its number is still counted, so that "row 5" in the
        # output is the fifth row of the file, as a spreadsheet numbers it,
        # and not the fifth row that held something.
        rows, numbers = [], []
        for number, row in enumerate(reader, 2):
            if row:
                rows.append(row)
                numbers.append(number)
        return rows, header, numbers


def row_dict(header, row):
    """One row as {column: cell}. A short row lacks its missing columns."""
    return dict(zip(header, row))


def read_boundary(path):
    """Polygons from a GeoJSON file on disk. A file with none raises."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        try:
            polygons = polygons_from_geojson(json.load(handle))
        except RecursionError:
            # json, or the walk above, on a GeometryCollection nested
            # thousands deep. An input that could not be read, not a batch
            # that failed the gate.
            raise ValueError("%s nests too deeply to read" % path)
    if not polygons:
        # An empty FeatureCollection, or one of null geometries, is the same
        # unreadable input as a Point boundary. Raised here it exits 2 as the
        # Point does; left to audit() it exited 64, the usage-error code.
        raise ValueError("%s holds no polygon, so containment cannot be "
                         "checked" % path)
    return polygons


def write_audited(path, raw_rows, header, results):
    """Copy the input CSV with a verdict column and a reason column added.

    Written by position, so every column survives whatever its name. A short
    row is padded with empty cells. The surplus cells of a ragged row have
    no column, and they are dropped so that the verdict stays in its own.
    """
    # Dropping them first, because auditing an audited CSV is how an operator
    # re-checks a fixed batch. Appending blindly wrote gcs_verdict twice, and
    # a duplicated column name is a file no reader can address by name.
    keep = [i for i, h in enumerate(header) if h not in ADDED_COLUMNS]
    width = len(header)
    with open(path, "w", newline="", encoding="utf-8",
              errors=_BYTES) as handle:
        writer = csv.writer(handle)
        writer.writerow([header[i] for i in keep] + list(ADDED_COLUMNS))
        for raw, result in zip(raw_rows, results):
            cells = list(raw) + [""] * (width - len(raw))
            writer.writerow([cells[i] for i in keep]
                            + [result.verdict, "; ".join(result.reasons)])


def env_min_trust_rate():
    """The --min-trust-rate default, read from the environment.

    argparse evaluates this while it builds the parser, outside main()'s error
    handling, so float() on a typo used to exit 1 with a traceback. 1 is the
    code a scheduled job reads as "the batch failed the gate", which is a
    different fact. The default comes back instead and main() refuses the run
    with a usage error, so the typo is never silently ignored either.
    """
    value = to_number(os.environ.get("GEOCODESIFT_MIN_TRUST_RATE"))
    return DEFAULT_MIN_TRUST_RATE if value is None else value


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, and 2 here means the input was unreadable.

    A typo in a flag is a usage error, so it exits 64 like every other one.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        sys.exit(64)


def _parse(argv):
    ap = _Parser(
        prog="geocodesift.py",
        description="Audit a geocoded batch instead of believing its match rate.",
        epilog="Config precedence: flag > environment > the CONFIGURATION "
               "block. Nothing is written without --apply.",
    )
    ap.add_argument("csv_positional", nargs="?", metavar="CSV",
                    help="CSV of geocoded results to audit, as a positional "
                         "argument so an ArcGIS script tool can pass it")
    ap.add_argument("--csv", help="the same CSV, named. This wins over the "
                                  "positional form when both are given")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    choices=sorted(PROFILES),
                    help="column names and match types to expect "
                         "(default %s)" % DEFAULT_PROFILE)
    ap.add_argument("--boundary",
                    help="GeoJSON file every geocode must fall inside. Off by "
                         "default because most batches have no boundary handy.")
    ap.add_argument("--min-trust-rate", dest="min_trust_rate", type=float,
                    default=env_min_trust_rate(),
                    help="TRUST fraction the batch must reach (default %.2f). "
                         "Env: GEOCODESIFT_MIN_TRUST_RATE"
                         % DEFAULT_MIN_TRUST_RATE)
    ap.add_argument("--pileup-threshold", dest="pileup_threshold", type=int,
                    default=DEFAULT_PILEUP_THRESHOLD,
                    help="records on one coordinate before it counts as a "
                         "pile-up (default %d)" % DEFAULT_PILEUP_THRESHOLD)
    ap.add_argument("--pileup-precision", dest="pileup_precision", type=int,
                    default=DEFAULT_PILEUP_PRECISION,
                    help="decimal places coordinates round to before grouping "
                         "(default %d)" % DEFAULT_PILEUP_PRECISION)
    ap.add_argument("--trust-score", dest="trust_score", type=float,
                    help="score at or above which a row may be TRUST, on the "
                         "profile's own score scale: 0 to 100 for esri, "
                         "importance 0 to 1 for nominatim (default %.4g; "
                         "nominatim %.4g)"
                         % (DEFAULT_TRUST_SCORE,
                            PROFILES["nominatim"]["trust_score"] / 100.0))
    ap.add_argument("--suspect-score", dest="suspect_score", type=float,
                    help="score below which a row is REJECT outright, on the "
                         "same scale as --trust-score (default %.4g; "
                         "nominatim %.4g)"
                         % (DEFAULT_SUSPECT_SCORE,
                            PROFILES["nominatim"]["suspect_score"] / 100.0))
    ap.add_argument("--house-number-tolerance", dest="house_tolerance",
                    type=int, default=DEFAULT_HOUSE_TOLERANCE,
                    help="house number difference tolerated between the input "
                         "and the match (default %d)" % DEFAULT_HOUSE_TOLERANCE)
    ap.add_argument("--by-class", dest="by_class", action="store_true",
                    help="also report and gate the TRUST rate of every address "
                         "class, read from the submitted address")
    ap.add_argument("--min-class-rows", dest="min_class_rows", type=int,
                    default=DEFAULT_MIN_CLASS_ROWS,
                    help="rows a class needs before its rate is judged "
                         "(default %d)" % DEFAULT_MIN_CLASS_ROWS)
    ap.add_argument("--compare",
                    help="a second result file for the same addresses, such "
                         "as a control geocoder, printed beside this one class "
                         "by class. Implies --by-class.")
    ap.add_argument("--compare-profile", dest="compare_profile",
                    choices=sorted(PROFILES),
                    help="profile of the --compare file (default: --profile)")
    ap.add_argument("--score-field", dest="score_field",
                    help="column holding the match score")
    ap.add_argument("--match-type-field", dest="match_type_field",
                    help="column holding the match type")
    ap.add_argument("--x-field", dest="x_field", help="column holding X")
    ap.add_argument("--y-field", dest="y_field", help="column holding Y")
    ap.add_argument("--address-field", dest="address_field",
                    help="column holding the address that was submitted")
    ap.add_argument("--matched-address-field", dest="matched_address_field",
                    help="column holding the address the geocoder returned")
    ap.add_argument("--status-field", dest="status_field",
                    help="column holding the match status (esri: M, T or U)")
    ap.add_argument("--out", help="path for the audited copy of the CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    args = ap.parse_args(argv)
    # A script tool passes parameters positionally; a shell user usually names
    # them. Accept both and let the named one win, matching fullpull.
    if not args.csv and getattr(args, "csv_positional", None):
        args.csv = args.csv_positional
    return args


def _same_file(a, b):
    """True when two paths name one file on disk, links and case included."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        # Either path is not there yet, so they cannot be one file.
        return False


def main(argv=None):
    # A scheduled job redirects stdout, and on Windows that makes it cp1252.
    # A match type outside it, echoed in a reason, ended the run in a
    # UnicodeEncodeError. An escaped character is still readable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        return _main(argv)
    except Exception as exc:
        # Python exits 1 on an uncaught error, and 1 is the code a scheduled
        # job reads as "the batch failed the gate". A crash is not a finding.
        print("error: %s: %s. This is a failure of the tool, not a finding "
              "about the batch." % (type(exc).__name__, exc), file=sys.stderr)
        return 2


def _main(argv):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    env_rate = os.environ.get("GEOCODESIFT_MIN_TRUST_RATE")
    if env_rate is not None and to_number(env_rate) is None:
        print("error: GEOCODESIFT_MIN_TRUST_RATE is %r, which is not a "
              "number." % (env_rate,), file=sys.stderr)
        return 64
    if not args.csv:
        print("error: --csv is required. Use --self-test to verify the tool "
              "without a batch.", file=sys.stderr)
        return 64
    if args.pileup_threshold < 2:
        print("error: --pileup-threshold must be at least 2.", file=sys.stderr)
        return 64
    if not 0.0 <= args.min_trust_rate <= 1.0:
        print("error: --min-trust-rate must be between 0 and 1.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64
    if args.min_class_rows < 1:
        print("error: --min-class-rows must be at least 1.", file=sys.stderr)
        return 64
    for flag, path in (("--csv", args.csv), ("--boundary", args.boundary),
                       ("--compare", args.compare)):
        if args.out and path and _same_file(args.out, path):
            # --apply would overwrite the input, and a boundary GeoJSON would
            # come back as CSV text. Refused before anything is read.
            print("error: --out names the %s file %s. Write the audited copy "
                  "somewhere else." % (flag, path), file=sys.stderr)
            return 64
    by_class = args.by_class or bool(args.compare)
    control_profile = args.compare_profile or args.profile

    try:
        fields = fields_for(args.profile, {
            "score": args.score_field,
            "match_type": args.match_type_field,
            "x": args.x_field,
            "y": args.y_field,
            "in_addr": args.address_field,
            "matched_addr": args.matched_address_field,
            "status": args.status_field,
        })
        raw_rows, header, numbers = read_csv(args.csv)
        polygons = read_boundary(args.boundary) if args.boundary else None
        # The control file is read with its profile's own columns. The field
        # overrides name columns of the file under audit, not of this one.
        control_fields = fields_for(control_profile)
        control_raw, control_header, _ = (read_csv(args.compare)
                                          if args.compare else ([], [], []))
    except (IOError, OSError, ValueError, csv.Error) as exc:
        # csv.Error too. A field over csv's 131072 character limit, or a NUL
        # in the file, is an unreadable input, not a batch that failed the
        # gate, and only the exit code tells those apart.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if by_class and fields["in_addr"] not in header:
        # Without the submitted address every row would classify as blank,
        # and a table with one class in it would pass for a real one.
        print("error: --by-class reads the submitted address, and the CSV has "
              "no %r column. Name it with --address-field."
              % fields["in_addr"], file=sys.stderr)
        return 2
    if args.compare and control_fields["in_addr"] not in control_header:
        print("error: the --compare CSV has no %r column, which the %s "
              "profile reads the submitted address from."
              % (control_fields["in_addr"], control_profile), file=sys.stderr)
        return 2

    missing = missing_columns(fields, header)
    if missing["required"]:
        # Naming the columns matters, and so does the exit code. Without a
        # required column no row can be TRUST, so the audit has no finding
        # about the geocoder: the input could not be read. A UTF-16 export,
        # a binary file and a wrong --profile all land here. Exiting 1 blamed
        # the batch for what was a tool or input failure.
        print("error: required columns not in the CSV: %s. Check --profile "
              "or name them with the --*-field overrides."
              % ", ".join(missing["required"]), file=sys.stderr)
        return 2
    for name, check in missing["optional"]:
        # An optional column absent is normal, not a fault. Saying which CHECK
        # is skipped is useful; calling a valid export a warning is not.
        print("note: %s not in the CSV, so %s" % (name, check),
              file=sys.stderr)
    for label, used, names in (("CSV", fields, header),
                               ("--compare CSV", control_fields,
                                control_header)):
        repeated = ambiguous_columns(used, names)
        if repeated:
            print("error: the %s names %s more than once, so which column to "
                  "read is ambiguous." % (label, ", ".join(repeated)),
                  file=sys.stderr)
            return 2

    control_missing = missing_columns(control_fields, control_header)
    if args.compare and control_missing["required"]:
        # As for --csv: no control row could be TRUST, and a warning let a
        # control at 0 percent print as a finding about the control geocoder.
        print("error: required columns not in the --compare CSV: %s. Check "
              "--compare-profile." % ", ".join(control_missing["required"]),
              file=sys.stderr)
        return 2

    rows = [extract_row(row_dict(header, raw), fields) for raw in raw_rows]
    control_rows = [extract_row(row_dict(control_header, raw), control_fields)
                    for raw in control_raw]
    if args.compare and not control_rows:
        # A control job that broke. Its table is all dashes, and a PASS
        # beside it read as a comparison that never happened.
        print("error: the --compare CSV holds no rows, so there is nothing to "
              "compare.", file=sys.stderr)
        return 2
    for label, used, extracted in (("CSV", by_class, rows),
                                   ("--compare CSV", args.compare,
                                    control_rows)):
        if used and extracted and all(address_classes(r["in_addr"])
                                      == ["blank"] for r in extracted):
            # The column guard above, one step on: a column that is there
            # and blank in every row classifies nothing, and a table of
            # "all" and "blank" passed for a real one.
            print("error: every submitted address in the %s is blank, so "
                  "--by-class has nothing to classify." % label,
                  file=sys.stderr)
            return 2
    # The floors arrive on the score's own scale, an importance of 0.5 for
    # nominatim, and classify() compares percents of it. Passed through raw,
    # --trust-score 0.5 was half a percent and passed rows at 0.01.
    floors = [None if v is None else score_percent(v, args.profile)
              for v in (args.trust_score, args.suspect_score)]
    try:
        report = audit(rows, args.profile, floors[0], floors[1],
                       polygons, args.pileup_threshold, args.pileup_precision,
                       args.house_tolerance)
        # The control is judged by its own profile's score floors, because a
        # floor given for this file's score scale means nothing on another's.
        # The boundary, pile-up and house-number settings are shared.
        control_report = audit(control_rows, control_profile, None, None,
                               polygons, args.pileup_threshold,
                               args.pileup_precision, args.house_tolerance)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    for line in describe(report, args.min_trust_rate, numbers=numbers):
        print(line)

    failures = []
    unjudged = []
    if by_class:
        table = class_table([r["in_addr"] for r in rows], report.results)
        control = None
        if args.compare:
            control = class_table([r["in_addr"] for r in control_rows],
                                  control_report.results)
            counts = control_report.counts
            print("\ncontrol file %s: %d rows, TRUST rate %.1f%% "
                  "(%d TRUST, %d SUSPECT, %d REJECT)"
                  % (args.compare, control_report.total,
                     control_report.trust_rate * 100.0, counts[TRUST],
                     counts[SUSPECT], counts[REJECT]))
        print("")
        for line in describe_classes(table, args.min_trust_rate,
                                     args.min_class_rows, control):
            print(line)
        failures = class_failures(table, args.min_trust_rate,
                                  args.min_class_rows)
        unjudged = [r.name for r in table if r.name != "all"
                    and r.status(args.min_trust_rate, args.min_class_rows)
                    == "too few rows"]

    if args.out:
        if args.apply:
            # A traceback here exits 1, and 1 is the code a scheduled job
            # reads as "the batch failed the gate". A write that failed is a
            # different fact and gets its own exit code.
            try:
                write_audited(args.out, raw_rows, header, report.results)
            except (IOError, OSError, ValueError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)

    code = gate(report, args.min_trust_rate)
    below = ", ".join("%s %.1f%%" % (r.name, r.rate * 100.0) for r in failures)
    if not report.total:
        print("\nFAIL: the batch holds no rows, which means the job that made "
              "it broke. Do not publish it.")
    elif code == 0 and not failures:
        # A PASS that said nothing of a refused class read as a PASS for it.
        unsaid = ""
        if unjudged:
            unsaid = (" %d address class(es) had too few rows to judge: %s."
                      % (len(unjudged), ", ".join(unjudged)))
        print("\nPASS: the batch clears the trust floor.%s" % unsaid)
    elif code == 0:
        # The case --by-class exists for: a healthy aggregate over a class the
        # geocoder fails. Saying "clears" first and "but" second is deliberate.
        print("\nFAIL: the batch clears the trust floor, but %d address "
              "class(es) do not: %s. Do not publish it." % (len(failures), below))
    else:
        print("\nFAIL: the batch is below the trust floor. Do not publish it.")
        if failures:
            print("%d address class(es) are below it as well: %s."
                  % (len(failures), below))
    return 1 if failures else code


if __name__ == "__main__":
    sys.exit(main())

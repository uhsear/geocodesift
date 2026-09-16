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

    python geocodesift.py --self-test
    python geocodesift.py --csv geocoded.csv --profile esri
    python geocodesift.py --csv geocoded.csv --boundary marion.geojson --min-trust-rate 0.95
    python geocodesift.py --csv geocoded.csv --out audited.csv --apply

Exit codes: 0 the batch passed, 1 the batch failed the gate, 2 the input could
not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
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

# TRUST rate the batch must reach. Below it, main() returns 1.
DEFAULT_MIN_TRUST_RATE = 0.90

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
        },
        "score_max": 100.0,
        "score_optional": False,
        "trust_types": ("pointaddress", "subaddress", "streetaddress",
                        "streetaddressext", "buildingname", "parcel"),
        "suspect_types": ("streetname", "streetint", "streetmidpoint",
                          "distancemarker", "poi"),
        "reject_types": ("postal", "postalext", "postalloc", "zip5", "zip4",
                         "locality", "city", "county", "subregion", "region",
                         "state", "country", "admin", "block", "sector"),
    },
    # The Census batch geocoder returns no score at all, only Exact or
    # Non_Exact, so the match type carries the entire decision. It also returns
    # a headerless CSV; add a header row or pass the --*-field overrides.
    "census": {
        "fields": {
            "score": "",
            "match_type": "match_type",
            "x": "lon",
            "y": "lat",
            "matched_addr": "matched_address",
            "in_addr": "input_address",
        },
        "score_max": 100.0,
        "score_optional": True,
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
        },
        "score_max": 1.0,
        "score_optional": False,
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


def classify(score, match_type, profile=DEFAULT_PROFILE, trust_score=None,
             suspect_score=None):
    """Classify one row from its score and its match type together.

    Score alone is the check everybody writes, and it is the one that publishes
    the wrong map. A ZIP centroid fallback scores in the high nineties because
    the geocoder matched the ZIP code well. The match type is the field that
    says what was matched, so it is read first and it overrides the score.
    """
    if profile not in PROFILES:
        raise ValueError("unknown profile %r, expected one of %s"
                         % (profile, ", ".join(sorted(PROFILES))))
    prof = PROFILES[profile]
    if trust_score is None:
        trust_score = prof.get("trust_score", DEFAULT_TRUST_SCORE)
    if suspect_score is None:
        suspect_score = prof.get("suspect_score", DEFAULT_SUSPECT_SCORE)
    if trust_score < 0 or suspect_score < 0:
        raise ValueError("score floors cannot be negative")
    if suspect_score > trust_score:
        raise ValueError("the reject floor %r is above the trust floor %r"
                         % (suspect_score, trust_score))
    if score is not None and not _is_finite(score):
        raise ValueError("score must be a real number, got %r" % (score,))
    if score is not None and score < 0:
        raise ValueError("score cannot be negative, got %r" % (score,))

    mt = (match_type or "").strip().lower()
    pct = None if score is None else 100.0 * float(score) / prof["score_max"]

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
    if not digits:
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


def polygons_from_geojson(obj):
    """Every Polygon and MultiPolygon in a GeoJSON object, as ring lists.

    Accepts a FeatureCollection, a Feature, a GeometryCollection or a bare
    geometry, because county boundaries arrive as all four and re-exporting
    one is not the operator's job.
    """
    kind = (obj or {}).get("type")
    if kind == "FeatureCollection":
        out = []
        for feat in obj.get("features") or []:
            out.extend(polygons_from_geojson(feat))
        return out
    if kind == "Feature":
        # An exported county boundary routinely carries one feature with a
        # null geometry. Refusing the whole file over it helps nobody.
        geom = obj.get("geometry")
        return polygons_from_geojson(geom) if geom else []
    if kind == "GeometryCollection":
        out = []
        for geom in obj.get("geometries") or []:
            out.extend(polygons_from_geojson(geom))
        return out
    if kind == "Polygon":
        return [obj["coordinates"]]
    if kind == "MultiPolygon":
        return list(obj["coordinates"])
    raise ValueError("no polygon found in GeoJSON of type %r" % (kind,))


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

# The two columns --out adds to a copy of the input.
ADDED_COLUMNS = ("gcs_verdict", "gcs_reasons")


def missing_columns(fields, header):
    """Split absent columns into the ones that break the audit and the rest.

    Reporting both as one "warning" made a valid Esri export, which simply has
    no USER_address column, look broken.
    """
    absent = [key for key, name in fields.items() if name and name not in header]
    return {
        "required": sorted(fields[k] for k in absent if k in REQUIRED_FIELDS),
        "optional": sorted(fields[k] for k in absent if k not in REQUIRED_FIELDS),
    }


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
    """Pull the six values this tool reasons about out of one CSV row."""
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
                                   profile, trust_score, suspect_score)
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
    """Exit code for a report: 0 when the TRUST rate clears the floor, else 1."""
    if not 0.0 <= min_trust_rate <= 1.0:
        raise ValueError("the trust rate floor must be between 0 and 1")
    return 0 if report.trust_rate >= min_trust_rate else 1


def describe(report, min_trust_rate=DEFAULT_MIN_TRUST_RATE, sample=5):
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
            # +2 turns a 0-based index into the line number in the CSV, which
            # is what the operator has open in front of them.
            out.append("  row %d %s: %s" % (r.index + 2, r.verdict,
                                            "; ".join(r.reasons)))
    return out


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core. No file, no network, no credentials."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

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
    check(verdict(100, "") == SUSPECT, "a blank match type is SUSPECT")
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
    raises(lambda: classify(-1, "PointAddress"), "a negative score raises")
    raises(lambda: classify(float("nan"), "PointAddress"),
           "a NaN score raises instead of reaching TRUST  <-- pinned defect")
    raises(lambda: classify(float("inf"), "PointAddress"),
           "an infinite score raises")
    raises(lambda: classify("97.5", "PointAddress"),
           "a score that is still a string raises")
    raises(lambda: classify(90, "PointAddress", trust_score=50.0,
                            suspect_score=80.0),
           "a reject floor above the trust floor raises")

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
    # Decimal degrees off a real boundary, where the cross product of a point
    # on the line comes out 5.55e-16 rather than 0. Without the tolerance in
    # _on_segment this parcel reads as outside the county.
    line_a, line_b = (-82.35233, 29.15085), (-81.69813, 29.07244)
    on_line = ((line_a[0] + line_b[0]) / 2.0, (line_a[1] + line_b[1]) / 2.0)
    check(point_in_ring(on_line[0], on_line[1],
                        [line_a, line_b, (-82.0, 28.5)]) is True,
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

    # ---- the pile-up detector over 30 synthetic rows
    stack = (-82.14000, 29.18700)      # the planted centroid stack
    complex4 = (-82.20000, 29.25000)   # a real four unit apartment complex
    coords = [stack] * 8 + [complex4] * 4
    coords += [(-82.30000 - i * 0.001, 29.30000 + i * 0.001) for i in range(18)]
    hits = find_pileups(coords, threshold=5)
    check(len(hits) == 1, "only the planted stack fires at threshold 5")
    check(hits[0][1] == 8, "the planted stack is reported with its count of 8")
    check(hits[0][0] == (-82.14, 29.187), "the pile-up reports its coordinate")
    check(all(k != pileup_key(*complex4) for k, _ in hits),
          "a legitimate complex of 4 does not fire at threshold 5")
    check(len(find_pileups(coords, threshold=4)) == 2,
          "lowering the threshold to 4 catches the complex as well")
    check(find_pileups(coords, threshold=8)[0][1] == 8,
          "exactly the threshold count fires, the threshold is inclusive")
    check(find_pileups(coords, threshold=9) == [],
          "a threshold above the stack size reports nothing")
    near = coords + [(-82.139999, 29.187001)]
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
    check(house_number("1234 SE 17TH ST") == 1234, "a leading house number is read")
    check(house_number("123-45 MAIN ST") == 123,
          "a hyphenated number reads its first part")
    check(house_number("123A NW 2ND AVE") == 123, "a unit letter is dropped")
    check(house_number("SE 17TH ST") is None, "a street with no number reads None")
    check(house_number("\u00b2345 MAIN ST") is None,
          "a superscript digit is not a house number  <-- pinned defect")
    check(house_number("\u0661\u0662\u0663 MAIN ST") is None,
          "an Arabic-Indic numeral is not a US house number")
    check(house_number("") is None, "an empty address reads None")
    check(house_number(None) is None, "a missing address reads None")
    check(house_number_problem("1234 SE 17TH ST", "1234 SE 17TH ST, OCALA") is None,
          "an identical house number is no problem")
    check(house_number_problem("1234 SE 17TH ST", "1240 SE 17TH ST") is None,
          "an interpolation 6 numbers off is tolerated")
    check(house_number_problem("1234 SE 17TH ST", "4321 SE 17TH ST") is not None,
          "a house number on another block is flagged")
    check(house_number_problem("1234 SE 17TH ST", "SE 17TH ST") is None,
          "no number on the matched side is left to the match type")
    check(house_number_problem("1234 SE 17TH ST", "1250 SE 17TH ST",
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
    check(house_number_problem("100 MAIN ST", "100 MAIN ST, OCALA",
                               tolerance=0) is None,
          "a tolerance of 0 still passes the same house number")

    # ---- field extraction off a raw CSV row
    fields = fields_for("esri")
    raw = {"Score": "97.5", "Addr_type": "PointAddress", "X": "-82.14",
           "Y": "29.187", "Match_addr": "1234 SE 17TH ST",
           "USER_address": "1234 SE 17TH ST"}
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
    good = {"score": 98.0, "match_type": "PointAddress", "x": -82.3, "y": 29.3,
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
    check(audit([dict(good, x=float("inf"), y=29.3)]).counts[REJECT] == 1,
          "an infinite coordinate is REJECT")
    check(audit([dict(good, x="-82.3", y="29.3")]).counts[REJECT] == 1,
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
        return [dict(template, x=-82.3 - i * 0.01, y=29.3 + i * 0.01)
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
    check(m["optional"] == ["USER_address"],
          "a missing input-address column is optional, not a warning  <-- pinned defect")
    m = missing_columns(f, ["Addr_type", "X", "Y"])
    check("Score" in m["required"],
          "a missing score column is required, because every row then reads SUSPECT")
    m = missing_columns(f, ["Score", "Addr_type", "X", "Y", "Match_addr", "USER_address"])
    check(m["required"] == [] and m["optional"] == [],
          "a complete header reports nothing missing")
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
    check(_parse(["--csv", "g.csv", "--out", "o.csv"]).out == "o.csv",
          "--out is read")

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

    # ---- the Census profile's real column map over a real Census batch
    cf = fields_for("census")
    check((cf["x"], cf["y"], cf["match_type"]) == ("lon", "lat", "match_type"),
          "the census profile reads lon, lat and match_type")
    check((cf["in_addr"], cf["matched_addr"])
          == ("input_address", "matched_address"),
          "the census profile reads input_address and matched_address")
    census_raw = {"input_address": "1234 SE 17TH ST, OCALA, FL, 34471",
                  "matched_address": "1234 SE 17TH ST, OCALA, FL, 34471",
                  "lon": "-82.12650", "lat": "29.17860",
                  "match_type": "Exact"}
    crow = extract_row(census_raw, cf)
    check(crow["score"] is None,
          "a census row carries no score, because the profile names no column")
    check((crow["x"], crow["y"]) == (-82.1265, 29.1786),
          "the census lon and lat columns are read as the coordinate")
    check(crow["match_type"] == "Exact", "the census match type column is read")
    check(crow["in_addr"].startswith("1234 SE"),
          "the census input_address column is read")
    check("score" not in classify(None, "No_Match", "census")[1],
          "a scoreless refusal does not describe a score it never had"
          "  <-- pinned defect")
    census_rows = [crow,
                   extract_row(dict(census_raw, match_type="Non_Exact",
                                    lon="-82.14010", lat="29.18990"), cf),
                   extract_row(dict(census_raw, match_type="No_Match",
                                    lon="", lat="", matched_address=""), cf),
                   extract_row(dict(census_raw, match_type="Tie",
                                    lon="-82.13000", lat="29.18700"), cf)]
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
    nrow = extract_row({"query": "1234 SE 17TH ST, Ocala",
                        "display_name": "1234, Southeast 17th Street, Ocala",
                        "lon": "-82.12650", "lat": "29.17860",
                        "addresstype": "house", "importance": "0.45"}, nf)
    check(nrow["score"] == 0.45,
          "the nominatim importance column is read as the score")
    check(nrow["matched_addr"].startswith("1234,"),
          "the nominatim display_name column is read as the matched address")
    nrep = audit([nrow,
                  dict(nrow, match_type="city", score=0.82,
                       x=-82.14010, y=29.18720),
                  dict(nrow, match_type="road", score=0.30,
                       x=-82.13000, y=29.17900),
                  dict(nrow, match_type="postcode", score=0.20,
                       x=-82.12000, y=29.16000)], "nominatim")
    check((nrep.counts[TRUST], nrep.counts[SUSPECT], nrep.counts[REJECT])
          == (1, 1, 2),
          "a four row nominatim batch audits 1 TRUST, 1 SUSPECT and 2 REJECT")
    check("AREA" in nrep.results[1].reasons[0],
          "a prominent city is refused on its match type, not its importance")

    # ---- describe() on the two reports it has to render
    many = []
    for i in range(6):
        many.extend([dict(good, x=-82.0 - i, y=29.0)] * 5)
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
        five.extend([dict(good, x=-82.0 - i, y=29.0)] * 5)
    check(not any("...and" in l for l in describe(audit(five), 0.90)),
          "exactly the sample size prints no and-more line")
    lines = describe(audit(spread(3)), 0.90)
    check(not any("not TRUST" in l for l in lines),
          "a clean batch lists no failing rows at all")

    # ---- real files on disk. Everything below writes into one temp directory
    # and deletes it again. No network, no database, no credentials.
    tmp = tempfile.mkdtemp(prefix="geocodesift-selftest-")

    def tmpfile(name, text, encoding="utf-8"):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding=encoding) as handle:
            handle.write(text)
        return path

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    # A ten row Esri batch: 5 TRUST, 3 SUSPECT, 2 REJECT, and one of the TRUST
    # rows sits outside the county boundary built further down.
    batch_text = (
        "Score,Addr_type,X,Y,Match_addr,USER_address\n"
        "98,PointAddress,-82.30000,29.20000,100 MAIN ST,100 MAIN ST\n"
        "100,Zip5,-82.31000,29.21000,\"OCALA, FL, 34471\",1234 SE 17TH ST\n"
        "97,PointAddress,-82.32000,29.22000,9100 SW 20TH ST,1234 SW 20TH ST\n"
        "88,PointAddress,-82.33000,29.23000,1300 NE 8TH AVE,1300 NE 8TH AVE\n"
        "61,PointAddress,-82.34000,29.24000,55 NW 10TH AVE,55 NW 10TH AVE\n"
        "98,StreetName,-82.35000,29.25000,SE 17TH ST,1234 SE 17TH ST\n"
        "99,PointAddress,-83.50000,29.20000,700 CEDAR ST,700 CEDAR ST\n"
        "98,PointAddress,-82.36000,29.26000,240 SE 5TH ST,240 SE 5TH ST\n"
        "96,PointAddress,-82.37000,29.27000,18 NW 3RD AVE,18 NW 3RD AVE\n"
        "95,PointAddress,-82.38000,29.28000,4110 SW 7TH ST,4110 SW 7TH ST\n")
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
    bom_rows, bom_header = read_csv(bom_csv)
    check(bom_header[0] == "Score",
          "a UTF-8 BOM is stripped from the first column name  <-- pinned defect")
    check(bom_rows[0]["Score"] == "98",
          "the score column of a BOM file is still addressable by name")
    check(run_cli(["--csv", bom_csv, "--min-trust-rate", "0.5"])[0] == 0,
          "a BOM file audits the same as one without  <-- pinned defect")

    # ---- a ragged CSV, written back out with the two audit columns
    ragged_csv = tmpfile(
        "ragged.csv",
        "Score,Addr_type,X,Y,Match_addr,USER_address\n"
        "98,PointAddress,-82.30000,29.20000,100 MAIN ST,100 MAIN ST,STRAY,MORE\n"
        "97,PointAddress,-82.31000\n")
    rag_rows, rag_header = read_csv(ragged_csv)
    check(None in rag_rows[0],
          "a row with surplus commas parks the extra cells under the None key")
    check(rag_rows[1]["Y"] is None, "a short row leaves its missing cells empty")
    esri = fields_for("esri")
    rag_results = audit([extract_row(r, esri) for r in rag_rows]).results
    rag_out = os.path.join(tmp, "ragged-audited.csv")
    write_audited(rag_out, rag_rows, rag_header, rag_results)
    back, back_header = read_csv(rag_out)
    check(back_header == rag_header + list(ADDED_COLUMNS),
          "the audited copy keeps every input column and adds two")
    check(None not in back[0],
          "the surplus cells of a ragged row do not reach the writer"
          "  <-- pinned defect")
    check(back[0]["gcs_verdict"] == TRUST and back[1]["gcs_verdict"] == REJECT,
          "each row is written with its own verdict")
    check("no usable coordinate" in back[1]["gcs_reasons"],
          "the reason for a verdict is written beside it")
    twice = os.path.join(tmp, "ragged-audited-twice.csv")
    write_audited(twice, back, back_header,
                  audit([extract_row(r, esri) for r in back]).results)
    twice_rows, twice_header = read_csv(twice)
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
    written, written_header = read_csv(out_path)
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

    # ---- a county boundary from a GeoJSON file on disk, lake included
    county = [[-82.40, 29.05], [-81.90, 29.05], [-81.90, 29.45],
              [-82.05, 29.45], [-82.05, 29.30], [-82.40, 29.30]]
    lake = [[-82.20, 29.15], [-82.10, 29.15], [-82.10, 29.22], [-82.20, 29.22]]
    county_path = tmpfile("county.geojson", json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"NAME": "Marion"},
             "geometry": {"type": "Polygon",
                          "coordinates": [county, lake]}}]}),
        encoding="utf-8-sig")
    polys = read_boundary(county_path)
    check(len(polys) == 1 and len(polys[0]) == 2,
          "the boundary file reads back as one polygon with one hole")
    check(point_in_any(-82.30, 29.20, polys) is True,
          "a point in the body of the county is contained")
    check(point_in_any(-81.95, 29.40, polys) is True,
          "a point in the county's narrow northern arm is contained")
    check(point_in_any(-82.15, 29.18, polys) is False,
          "a point in the middle of the lake is not contained")
    check(point_in_any(-82.30, 29.40, polys) is False,
          "a point in the notch beside the arm is not contained")
    check(point_in_any(-83.50, 29.20, polys) is False,
          "a point in the next county is not contained")
    code, out, err = run_cli(["--csv", batch_csv, "--boundary", county_path,
                              "--min-trust-rate", "0.4"])
    check(code == 0, "the batch clears a floor set for the boundary run")
    check("TRUST rate: 40.0%" in out,
          "the row outside the county drops the rate from 50% to 40%")
    outside_path = os.path.join(tmp, "boundary-audited.csv")
    run_cli(["--csv", batch_csv, "--boundary", county_path, "--out",
             outside_path, "--apply", "--min-trust-rate", "0.4"])
    bounded, _ = read_csv(outside_path)
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
    check(run_cli(["--csv", batch_csv, "--boundary", empty_fc])[0] == 64,
          "a boundary file holding no polygon refuses the run  <-- pinned defect")

    # ---- the per-field overrides, end to end on a CSV that names nothing the
    # profile expects
    odd_csv = tmpfile(
        "odd.csv",
        "CONF,KIND,LON,LAT,OUT_ADDR,IN_ADDR\n"
        "98,PointAddress,-82.30000,29.20000,100 MAIN ST,100 MAIN ST\n"
        "100,Zip5,-82.31000,29.21000,\"OCALA, FL\",1234 SE 17TH ST\n")
    code, out, err = run_cli(["--csv", odd_csv, "--min-trust-rate", "0.5"])
    check(code == 1, "a CSV with its own column names audits as unusable")
    check("required columns not in the CSV: Addr_type, Score, X, Y" in err,
          "the required columns that are absent are named, not just counted")
    check("USER_address not in the CSV" in err
          and "house-number check is skipped" in err,
          "an absent optional column is a note naming the check it skips")
    code, out, err = run_cli(["--csv", odd_csv, "--min-trust-rate", "0.5",
                              "--score-field", "CONF",
                              "--match-type-field", "KIND",
                              "--x-field", "LON", "--y-field", "LAT",
                              "--address-field", "IN_ADDR",
                              "--matched-address-field", "OUT_ADDR"])
    check(code == 0, "the six field overrides make the same CSV auditable")
    check("TRUST rate: 50.0%" in out,
          "the overridden columns produce the verdicts the data deserves")
    check(err == "", "nothing is reported missing once the overrides name it")

    # ---- the pile-up flags, over a batch that is mostly one point
    stack_csv = tmpfile("stacked.csv", "Score,Addr_type,X,Y\n"
                        + "98,PointAddress,-82.14000,29.18700\n" * 6)
    code, out, err = run_cli(["--csv", stack_csv])
    check(code == 1, "a batch stacked on one point fails the gate")
    check("6 record(s) on -82.14, 29.187" in out,
          "the CLI names the pile-up coordinate and its count")
    check("coordinate pile-ups" not in run_cli(
              ["--csv", stack_csv, "--pileup-threshold", "7"])[1],
          "--pileup-threshold raises the count a pile-up needs")
    check("6 record(s) on" in run_cli(
              ["--csv", stack_csv, "--pileup-threshold", "2"])[1],
          "--pileup-threshold 2 is accepted, it is the smallest pile-up there "
          "is")
    near_csv = tmpfile("near.csv", "Score,Addr_type,X,Y\n"
                       + "98,PointAddress,-82.30000,29.20000\n" * 3
                       + "98,PointAddress,-82.3000001,29.2000001\n" * 3)
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

    # ---- the other two profiles, through the CLI, on real batch output
    census_csv = tmpfile(
        "census.csv",
        "input_address,matched_address,lon,lat,match_type\n"
        "1234 SE 17TH ST OCALA FL,\"1234 SE 17TH ST, OCALA, FL, 34471\","
        "-82.12650,29.17860,Exact\n"
        "55 NW 10TH AVE OCALA FL,\"57 NW 10TH AVE, OCALA, FL, 34475\","
        "-82.14010,29.18990,Non_Exact\n"
        "PO BOX 1234 OCALA FL,,,,No_Match\n"
        "100 MAIN ST OCALA FL,\"100 MAIN ST, OCALA, FL\","
        "-82.13000,29.18700,Tie\n")
    code, out, err = run_cli(["--csv", census_csv, "--profile", "census",
                              "--min-trust-rate", "0.25"])
    check(code == 0, "a real census batch audits through the CLI")
    check("TRUST rate: 25.0%" in out, "the census batch scores 25% TRUST")
    check(err == "", "the census profile asks for no score column")
    check("not an address-level match" in out,
          "the census refusal reads as a sentence with no score in it")
    nomi_csv = tmpfile(
        "nominatim.csv",
        "query,display_name,lon,lat,addresstype,importance\n"
        "1234 SE 17TH ST,\"1234, SE 17th Street, Ocala\","
        "-82.12650,29.17860,house,0.45\n"
        "OCALA FL,\"Ocala, Marion County, Florida\","
        "-82.14010,29.18720,city,0.82\n"
        "SE 17TH ST,\"Southeast 17th Street, Ocala\","
        "-82.13000,29.17900,road,0.30\n"
        "34471,\"34471, Ocala, Florida\",-82.12000,29.16000,postcode,0.20\n")
    code, out, err = run_cli(["--csv", nomi_csv, "--profile", "nominatim",
                              "--min-trust-rate", "0.25"])
    check(code == 0, "a real nominatim batch audits through the CLI")
    check("TRUST rate: 25.0%" in out, "the nominatim batch scores 25% TRUST")
    check("The score 0.82 says how well that AREA matched" in out,
          "importance is reported as what it measured, the city it matched")

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
                       "98,PointAddress,-82.3,29.2,\"%s\"\n" % ("A" * 200000))
    check(run_cli(["--csv", huge_csv])[0] == 2,
          "a CSV field over the csv module's own limit exits 2, not 1"
          "  <-- pinned defect")
    empty_csv = tmpfile("empty.csv", "")
    code, out, err = run_cli(["--csv", empty_csv])
    check(code == 1 and "rows audited: 0" in out,
          "an empty CSV fails the gate instead of scoring a perfect rate")

    # ---- the environment default. The variable is removed afterwards rather
    # than restored: the self-test owns the rest of this process, and putting
    # an empty string back would itself be a usage error.
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

    shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def read_csv(path):
    """Raw rows and the header of a CSV, as dicts."""
    # utf-8-sig, because Esri's Table To Table and Excel both write a UTF-8
    # BOM. Read as plain UTF-8 the first header cell arrives as "\ufeffScore",
    # the score column reads as absent on every row, and a clean batch fails
    # the gate for a reason nothing in the output explains.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def read_boundary(path):
    """Polygons from a GeoJSON file on disk."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return polygons_from_geojson(json.load(handle))


def write_audited(path, raw_rows, header, results):
    """Copy the input CSV with a verdict column and a reason column added."""
    # Dropping them first, because auditing an audited CSV is how an operator
    # re-checks a fixed batch. Appending blindly wrote gcs_verdict twice, and
    # a duplicated column name is a file no reader can address by name.
    out_header = [h for h in header if h not in ADDED_COLUMNS]
    out_header += list(ADDED_COLUMNS)
    # extrasaction="ignore" because DictReader parks the surplus values of a
    # ragged row under the key None, and DictWriter treats that key as a fatal
    # unknown field. One stray comma inside an address would otherwise kill
    # the run after the audit had already printed its verdict.
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=out_header,
                                extrasaction="ignore")
        writer.writeheader()
        for raw, result in zip(raw_rows, results):
            row = dict(raw)
            row[ADDED_COLUMNS[0]] = result.verdict
            row[ADDED_COLUMNS[1]] = "; ".join(result.reasons)
            writer.writerow(row)


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


def _parse(argv):
    ap = argparse.ArgumentParser(
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
                    help="score at or above which a row may be TRUST "
                         "(default %.0f, or the profile's own floor)"
                         % DEFAULT_TRUST_SCORE)
    ap.add_argument("--suspect-score", dest="suspect_score", type=float,
                    help="score below which a row is REJECT outright "
                         "(default %.0f, or the profile's own floor)"
                         % DEFAULT_SUSPECT_SCORE)
    ap.add_argument("--house-number-tolerance", dest="house_tolerance",
                    type=int, default=DEFAULT_HOUSE_TOLERANCE,
                    help="house number difference tolerated between the input "
                         "and the match (default %d)" % DEFAULT_HOUSE_TOLERANCE)
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


def main(argv=None):
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

    try:
        fields = fields_for(args.profile, {
            "score": args.score_field,
            "match_type": args.match_type_field,
            "x": args.x_field,
            "y": args.y_field,
            "in_addr": args.address_field,
            "matched_addr": args.matched_address_field,
        })
        raw_rows, header = read_csv(args.csv)
        polygons = read_boundary(args.boundary) if args.boundary else None
    except (IOError, OSError, ValueError, csv.Error) as exc:
        # csv.Error too. A field over csv's 131072 character limit, or a NUL
        # in the file, is an unreadable input, not a batch that failed the
        # gate, and only the exit code tells those apart.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    missing = missing_columns(fields, header)
    if missing["required"]:
        # Naming the columns matters. A score column that is silently absent
        # reads as None on every row and quietly turns the whole batch SUSPECT,
        # which looks like a data problem rather than a wrong --profile.
        print("warning: required columns not in the CSV: %s"
              % ", ".join(missing["required"]), file=sys.stderr)
    if missing["optional"]:
        # An optional column absent is normal, not a fault. Saying which CHECK
        # is skipped is useful; calling a valid export a warning is not.
        print("note: %s not in the CSV, so the house-number check is skipped"
              % ", ".join(missing["optional"]), file=sys.stderr)

    rows = [extract_row(raw, fields) for raw in raw_rows]
    try:
        report = audit(rows, args.profile, args.trust_score, args.suspect_score,
                       polygons, args.pileup_threshold, args.pileup_precision,
                       args.house_tolerance)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    for line in describe(report, args.min_trust_rate):
        print(line)

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
    print("\n%s" % ("PASS: the batch clears the trust floor."
                    if code == 0 else
                    "FAIL: the batch is below the trust floor. Do not publish it."))
    return code


if __name__ == "__main__":
    sys.exit(main())

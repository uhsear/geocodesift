# geocodesift

Audit a geocoded batch instead of believing its match rate. Exits non-zero before the scheduled
job publishes the wrong map.

Two thousand code-enforcement cases go through a geocoder for a commission heat map. The report
says 96 percent matched. Three hundred and eighty records sit stacked on one downtown coordinate,
because the geocoder fell back to the city centroid and scored that fallback 98.

The heat map shows a violation hot spot in the historic district, where nothing happened. Nothing
in the output file is wrong, either. The geocoder said `Addr_type` was `Zip5` on every one of
those rows, which means it matched a ZIP code and not an address, and the score describes how well
it matched that ZIP code. The match rate counted them anyway, because a match rate counts the rows
the geocoder was willing to answer.

```
$ python geocodesift.py --self-test
geocodesift self-test: no file, no network, no credentials
--------------------------------------------------------------------
PASS  score 100 at match type Zip5 is REJECT  <-- pinned defect
PASS  a confident PostalExt is REJECT
PASS  a city centroid at score 99 is REJECT
...
PASS  a point whose ray passes through two vertices is still inside
PASS  a point on a horizontal hole edge is inside
...
PASS  only the planted stack fires at threshold 5
PASS  a legitimate complex of 4 does not fire at threshold 5
...
PASS  eight stacked rows leave the TRUST count at 2  <-- pinned defect
PASS  an empty batch fails the gate rather than scoring a perfect rate  <-- pinned defect
...
PASS  a NaN coordinate is REJECT, not a point on the map  <-- pinned defect
PASS  a boundary holding no polygon raises rather than skipping the check  <-- pinned defect
...
PASS  --apply defaults to OFF
...
--------------------------------------------------------------------
159 assertions, 0 failed
```

## Requirements

Python 3.9 or later. Standard library only: `csv`, `json`, `math`, `argparse`. No `arcpy`, no
`shapely`, no `pandas`, no network and no credentials. It runs the same on ArcGIS Pro's Python
and on a plain `python3`.

```
git clone https://github.com/uhsear/geocodesift.git
python geocodesift.py --self-test
```

## Usage

The CSV can be named or positional. A script tool passes its parameters
positionally, so both forms work and the named one wins when both appear.

```
python geocodesift.py results.csv
python geocodesift.py --csv results.csv --profile esri
```


The audit is read-only. The audited copy of the CSV is written only with `--apply`.

```
python geocodesift.py --csv geocoded.csv
python geocodesift.py --csv geocoded.csv --boundary marion.geojson --min-trust-rate 0.95
python geocodesift.py --csv geocoded.csv --out audited.csv --apply
```

A real run against 200 rows, 30 of them a planted ZIP centroid stack and 5 of them outside the
county:

```
$ python geocodesift.py --csv geocoded.csv --boundary marion.geojson --out audited.csv
rows audited: 200
  TRUST       150
  SUSPECT      15
  REJECT       35
TRUST rate: 75.0% (floor 90.0%)

coordinate pile-ups:
  30 record(s) on -82.14, 29.187
  5 record(s) on -81.2, 30.3

first 5 row(s) that are not TRUST:
  row 152 REJECT: match type 'Zip5' is an area fallback, not an address. The score 98 says how well that AREA matched.; house number 500 was matched to 34475, 33975 apart; shares coordinate (-82.14, 29.187) with 30 record(s)
  ...

Check only. audited.csv was not written. Re-run with --apply.

FAIL: the batch is below the trust floor. Do not publish it.
```

| Flag | Default | What it does |
|---|---|---|
| `--csv` | none | Geocoded results to audit. Required. |
| `--profile` | `esri` | Column names and match types to expect: `esri`, `census` or `nominatim`. |
| `--boundary` | off | GeoJSON file every geocode must fall inside. |
| `--min-trust-rate` | `0.90` | TRUST fraction the batch must reach. Env: `GEOCODESIFT_MIN_TRUST_RATE` |
| `--pileup-threshold` | `5` | Records on one coordinate before it counts as a pile-up. |
| `--pileup-precision` | `5` | Decimal places coordinates round to before grouping. |
| `--trust-score` | profile | Score at or above which a row may be TRUST. |
| `--suspect-score` | profile | Score below which a row is REJECT outright. |
| `--house-number-tolerance` | `20` | House number difference tolerated between input and match. |
| `--score-field` and five more | profile | Per-field column overrides, for a CSV that was renamed. |
| `--out` | none | Path for the audited copy of the CSV. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

The other overrides are `--match-type-field`, `--x-field`, `--y-field`, `--address-field` and
`--matched-address-field`. Each one replaces a single column name from the profile.

Exit codes: 0 the batch passed, 1 the batch failed the gate, 2 the input could not be read,
64 usage error.

## What it checks

- **The match type, before the score.** A row is classified by `(score, match_type)` together.
  `Zip5`, `PostalExt`, `Locality` and the other area types are REJECT at any score, including 100.
  This is the whole point: a score of 100 at `Zip5` is the geocoder saying, plainly, that it
  matched a ZIP code.
- **Coordinate pile-ups.** Records sharing one coordinate, rounded to `--pileup-precision`, are
  counted. A group at or above `--pileup-threshold` is reported with its coordinate and count, and
  every row in it drops to SUSPECT. No per-row field can catch this, because each row in a
  centroid stack looks correct on its own line.
- **Containment.** With `--boundary`, every coordinate is tested against a GeoJSON polygon by ray
  casting. Points on a vertex, on a horizontal edge and on the boundary of an interior ring all
  count as inside. A boundary file that holds no polygon is refused, because an empty boundary
  would otherwise switch the containment check off without saying so.
- **House numbers.** When both the submitted address and the matched address carry a house number,
  a difference above `--house-number-tolerance` is flagged. A row with no number on either side is
  left alone, because the match type already reports that.
- **The aggregate.** The TRUST rate is compared with `--min-trust-rate`, and the process exits 1
  below it. An empty CSV scores 0 percent and fails, rather than passing on a vacuous 100 percent.

## Why not the geocoder's own report

Esri's Geocode Addresses already writes `Status`, `Score` and `Addr_type`, and the ArcGIS Pro
rematch pane shows all three per row. That is the right data, and the rematch pane is the right
tool for fixing twenty addresses by hand. Nobody rematches two thousand rows by eye, and the pane
has no exit code.

The gap is the aggregate refusal. A scheduled job needs one number it can fail on, and it needs
the pile-up count, which is a property of the batch rather than of any row. No per-row field can
express "these 380 records are the same point".

The `esri` profile exists so this reads that same output file unchanged. It does not re-geocode
anything and it has no opinion about which geocoder you used.

## Limits

- It never fixes a row. It classifies, reports and exits. Rematching is still the rematch pane's
  job.
- TRUST means the geocoder's own fields are self-consistent, not that the point is correct. A
  confident rooftop match on the wrong street passes.
- The house number check reads the first run of digits in the address. A matched address that
  leads with a ZIP code, such as `34475, Ocala, Florida`, reports a huge house number difference.
  That row is already REJECT on its match type, so the noise lands only in the reason text.
- Ray casting treats coordinates as plane geometry. A boundary in decimal degrees is tested as if
  it were flat, which is accurate at county scale and wrong for a continent.
- Both the CSV and the boundary are read into memory. A few hundred thousand rows are fine; a
  national file is not.
- No projection handling. The coordinates in the CSV and the coordinates in the GeoJSON must
  already be in the same system, and nothing checks that they are.
- The match type lists are per profile and are not exhaustive. An unknown type is SUSPECT, never
  TRUST, so a locale-specific type that this does not know will hold the batch instead of passing
  it.
- A cell reading `nan`, `inf` or `1e400` is treated as an absent value, not a number. `float()`
  accepts all three, and a NaN score clears every floor because every comparison against NaN is
  false. The same rule applies to coordinates: a NaN coordinate is REJECT, never a point.
- The Census batch geocoder returns a headerless CSV with no score column. Add a header row, or
  pass the `--*-field` overrides.
- Nominatim's `importance` is a prominence measure, not a match confidence. The `nominatim`
  profile sets its floors low on purpose and lets the match type decide.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

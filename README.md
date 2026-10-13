# geocodesift

Audit a geocoded batch instead of believing its match rate. Exits non-zero before the scheduled
job publishes the wrong map. With `--by-class`, it also judges each kind of address on its own, so
a healthy aggregate cannot hide a kind of address the geocoder fails. A class with fewer than 30
rows is judged by its exact 95 percent interval, and is refused only when that interval is too
wide to tell. A PASS names every class it refused. `--profile tiers` reads the tier ladder of a
locator that joins a key instead of parsing an address, and `--key-field` catches a join that
put one record on several rows.

Two thousand code-enforcement cases go through a geocoder for a commission heat map. (The numbers
in this story are illustrative.) The report says 96 percent matched. Three hundred and eighty records sit stacked on one downtown coordinate,
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
...
PASS  a tens word before TENTH is a name, only FIRST to NINTH join it  <-- pinned defect
...
PASS  a slash token that is not a fraction is not skipped as one  <-- pinned defect
...
PASS  2 of 20 is under the row minimum but its interval ends at 32 percent, so it is BELOW, not refused  <-- pinned defect
...
PASS  0 of 1 is refused at a 90 percent floor, its exact interval ends at 97.5 percent  <-- pinned defect
...
PASS  a refused class prints its counts and interval, and no rate  <-- pinned defect
...
PASS  a Census file in its own layout, header added, exits 2 until it is reshaped  <-- pinned defect
...
PASS  a class under --min-class-rows whose interval lies below the floor fails the batch, not a silent PASS  <-- pinned defect
PASS  a PASS names every class it refused to judge  <-- pinned defect
PASS  a class of 10 at 80 percent is refused under the default of 30
PASS  --min-class-rows 5 reaches the gate and fails that class  <-- pinned defect
...
PASS  one blank address among good ones is a blank class, not a refused batch  <-- pinned defect
...
PASS  the self-test sets an exported floor aside, and removes its directory and restores the floor after a crash  <-- pinned defect
...
PASS  a tier in none of the three lists raises rather than defaulting to SUSPECT  <-- pinned defect
...
PASS  an unlocated row that still carries a point is caught, and the one without a point is not  <-- pinned defect
...
PASS  a fanned-out batch has no pile-up, every row sits on its own point, and the key count still finds it  <-- pinned defect
...
PASS  a tiers batch at 95 percent fails on one unlocated row with an old point  <-- pinned defect
...
PASS  the same tier in two lists is refused before the CSV is read  <-- pinned defect
...
PASS  with --key-field the fan-out fails the batch, with no pile-up in sight  <-- pinned defect
...
PASS  a --key-field column that is blank on every row exits 2, not PASS  <-- pinned defect
...
PASS  --boundary with an empty value exits 64, not a PASS with the check off  <-- pinned defect
...
PASS  a padded, mixed case non-located tier with a point is caught  <-- pinned defect
...
PASS  a UTF-16 CSV exits 2, not 1  <-- pinned defect
...
PASS  a bad integer exits 64, not argparse's 2  <-- pinned defect
...
PASS  a unique prefix of --apply, --ap, is refused with exit 64  <-- pinned defect
...
PASS  an unexpected error exits 2 and says it is the tool's failure  <-- pinned defect
PASS  the harness counts a false check, a missing error and a wrong error as three failures
...
PASS  and writes no bytecode cache beside the script
--------------------------------------------------------------------
616 assertions, 0 failed
```

The same 616 assertions pass on Windows with Python 3.13, 3.12 and 3.9, and on Linux with Python
3.12. The runs print the same lines; Windows ends each line with CR LF where Linux ends it with LF. The self-test reaches every line and every branch of the script:
`coverage run --branch geocodesift.py --self-test` reports 100 percent. The last checks feed the
harness a false check, a call that raises nothing and a call that raises the wrong error, and
confirm that it counts all three as failures.

## Requirements

Python 3.9 or later. Standard library only: `csv`, `json`, `math`, `argparse`, `os` and `sys`, and
`tempfile`, `shutil`, `io`, `contextlib` and `importlib` for the self-test's own temporary files
and its import check. No `arcpy`, no `shapely`, no `pandas`, no network and no credentials.
Nothing in it needs `arcpy`, so it should run on the Python that ships with ArcGIS Pro. This
repository did not run it there. The self-test ran on CPython 3.9, 3.12 and 3.13.

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
python geocodesift.py --csv geocoded.csv --boundary county.geojson --min-trust-rate 0.95
python geocodesift.py --csv geocoded.csv --out audited.csv --apply
python geocodesift.py --csv geocoded.csv --by-class
python geocodesift.py --csv vendor.csv --compare control.csv --compare-profile census
python geocodesift.py --csv records.csv --profile tiers --key-field record_id
```

A real run against 200 synthetic rows, 30 of them a planted ZIP centroid stack and 5 of them
outside the county. The synthetic county sits in the open Atlantic, so no coordinate in this README
or in the self-test is anybody's address:

```
$ python geocodesift.py --csv geocoded.csv --boundary county.geojson --out audited.csv
note: Status not in the CSV, so a tied match cannot be caught
rows audited: 200
  TRUST       150
  SUSPECT      15
  REJECT       35
TRUST rate: 75.0% (floor 90.0%)

coordinate pile-ups:
  30 record(s) on -60.14, 30.187

first 5 row(s) that are not TRUST:
  row 152 REJECT: match type 'Zip5' is an area fallback, not an address. The score 98 says how well that AREA matched.; shares coordinate (-60.14, 30.187) with 30 record(s)
  ...

Check only. audited.csv was not written. Re-run with --apply.

FAIL: the batch is below the trust floor. Do not publish it.
```

A row number in the output counts the rows of the CSV, with the header as row 1. A blank line is
skipped, not audited, but it still counts, so a blank line above a row moves that row's number
down by one.

| Flag | Default | What it does |
|---|---|---|
| `--csv` | none | Geocoded results to audit. Required. |
| `--profile` | `esri` | Column names and match types to expect: `esri`, `census`, `nominatim` or `tiers`. |
| `--boundary` | off | GeoJSON file every geocode must fall inside. |
| `--min-trust-rate` | `0.90` | TRUST fraction the batch must reach. Env: `GEOCODESIFT_MIN_TRUST_RATE` |
| `--pileup-threshold` | `5` | Records on one coordinate before it counts as a pile-up. |
| `--pileup-precision` | `5` | Decimal places coordinates round to before grouping. |
| `--trust-score` | profile | Score at or above which a row may be TRUST, on the profile's own scale. |
| `--suspect-score` | profile | Score below which a row is REJECT outright, on the same scale. |
| `--house-number-tolerance` | `20` | House number difference tolerated between input and match. |
| `--by-class` | off | Also report and gate the TRUST rate of every address class. |
| `--min-class-rows` | `30` | Rows a class needs before its rate alone is judged. A smaller class is judged by its exact interval. |
| `--compare` | none | A second result file for the same addresses, printed beside this one. Implies `--by-class`. |
| `--compare-profile` | `--profile` | Profile of the `--compare` file. |
| `--score-field` and six more | profile | Per-field column overrides, for a CSV that was renamed. |
| `--key-field` | off | Column holding the record key. A key on more than one row fails the batch. |
| `--trust-tiers` | `address_point,parcel_point` | Comma separated TRUST tiers. Needs `--profile tiers`. |
| `--suspect-tiers` | `parcel_centroid` | Comma separated SUSPECT tiers. Needs `--profile tiers`. |
| `--reject-tiers` | `parcel_not_found,no_key,unlocated` | Comma separated tiers that mean "no location". Needs `--profile tiers`. |
| `--out` | none | Path for the audited copy of the CSV. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

The two score floors use the scale of the profile's score column. For `esri` the scale is 0 to
100, and the defaults are 90 and 75. For `nominatim` the scale is an importance from 0 to 1, and
the defaults are 0.2 and 0.1. `--trust-score 0.5` with `--profile nominatim` is therefore an
importance of 0.5, not half a percent.

The other overrides are `--match-type-field`, `--x-field`, `--y-field`, `--address-field`,
`--matched-address-field` and `--status-field`. Each one replaces a single column name from the
profile.

Each profile is a set of column names and a set of match types:

| Profile | score | match type | x | y | matched address | input address | status |
|---|---|---|---|---|---|---|---|
| `esri` | `Score` | `Addr_type` | `X` | `Y` | `Match_addr` | `USER_address` | `Status` |
| `census` | none | `match_type` | `lon` | `lat` | `matched_address` | `input_address` | none |
| `nominatim` | `importance` | `addresstype` | `lon` | `lat` | `display_name` | `query` | none |
| `tiers` | none | `tier` | `x` | `y` | none | `address` | none |

The `census` profile reads no score column, because the Census batch geocoder returns none. The
match type then carries the whole decision: `Exact` is TRUST, `Non_Exact` is SUSPECT, and `Tie` and
`No_Match` are REJECT. A refusal under that profile names the match type and says nothing about a
score, because there was never a score to report.

The `census` profile does not read the Census batch geocoder's own file. For text and data files
the Census returns CSV with no header. It puts the interpolated longitude and latitude in one
column, and it splits the address range match indicator (`Match` or `No_Match` in the guide's
example) from the match type (`Exact` or `Non_Exact`) across two columns
([Census Geocoder User Guide, section 1.1.8](https://www2.census.gov/geo/pdfs/maps-data/data/Census_Geocoder_User_Guide.pdf),
read 2026-10-13). The guide's example shows no `Tie`, so the `Tie` value of this profile is not
checked against Census documentation. Reshape a Census result into the columns in the table first,
with one `match_type` column that holds `Exact`, `Non_Exact`, `Tie` or `No_Match`. A Census file
in its own layout, with a header added, exits 2 because `lon` and `lat` are missing. The
`--*-field` overrides cannot fix it either, because no override splits one cell into two.

The `esri` profile reads the `Status` column that Geocode Addresses writes. The tool page names
`Status` and `Addr_type` as fields of its Minimal output
([Geocode Addresses](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/geocoding/geocode-addresses.html),
read 2026-10-13). That page does not list the codes or the `Addr_type` values, so the codes below
(`M`, `T`, `U`) and the area types named in this README (`Zip5`, `PostalExt`, `Locality`) are
the ones this tool's fixtures use. This repository did not check them against an Esri page.
`T` is a tie: the
geocoder chose one candidate from several with the same best score, so the point may be the wrong
one. A tie is REJECT, as a census `Tie` is, and `U` (unmatched) is REJECT too. Without a `Status`
column a note says that a tied match cannot be caught.

Exit codes: 0 the batch passed, 1 the batch failed the gate, 2 the input could not be read or
the tool itself failed, 64 usage error. With `--by-class`, a class below the floor also exits 1.
A row in a non-located tier that carries a point, and a record key on more than one row, exit 1
at any TRUST rate. Under `--profile tiers`, a tier in no list and a tier in two lists exit 64. An empty value for
`--boundary`, `--compare`, `--key-field` or `--out` exits 64 as well, because an empty string
would switch that check off in silence. These exit 2:

- a CSV without a required column (score, match type, x or y), because no row could be TRUST. A
  UTF-16 export, a binary file and a wrong `--profile` all land here. A `--compare` CSV without
  one exits 2 as well, rather than printing a control at 0 percent.
- a CSV whose header names a column this reads more than once, because either one could be the
  score. The `--key-field` column counts as one this reads.
- a `--key-field` that names no column of the CSV, or a `--key-field` column that is blank on every
  row, because the fan-out check would have nothing to count.
- a boundary file that is not GeoJSON, holds a malformed polygon or no polygon at all, or nests
  too deeply to read. An empty FeatureCollection and one whose features have no geometry exit 2,
  as a boundary of type `Point` does.
- with `--by-class`, a CSV with no submitted-address column, or one where that column is blank in
  every row, because nothing can be classified. The same applies to a `--compare` CSV.
- a `--compare` CSV with a header and no rows, because there is nothing to compare. A table of
  dashes beside a PASS looked like a comparison that happened.
- an unexpected error inside the tool. Python exits 1 on an uncaught error, and 1 means the batch
  failed the gate, so the tool catches it, names it and exits 2.

An `--out` that names the `--csv`, `--boundary` or `--compare` file is a usage error, so `--apply`
cannot overwrite an input. A hard link to the input counts as the input too. An unknown flag, or a flag value of the wrong type, is a usage error
too: argparse would exit 2, and the tool makes it 64. A score cell that is negative is REJECT on
its own row, and the other rows are still audited. A character that the console cannot show,
such as a match type outside cp1252 on a redirected Windows stdout, is printed as an escape such
as `\u03a9`.

## What it checks

- **The status, before everything.** An Esri tie or unmatched status is REJECT at any score.
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
  below it. A CSV with no rows fails at any floor, 0 included, rather than passing on a vacuous
  rate. An empty batch means the job that made it broke.

## By address class

A 911 center tested a vendor geocoder against a sample of live 911 traffic. The figures in this
paragraph are rounded and illustrative, not a measurement that anyone can check from this
repository. The geocoder matched only 11 to 14 percent of the directional and numbered-street addresses, such as
`1234 NE 31ST ST`. Those addresses were 75 percent of that sample. A control locator matched 99
percent of the same addresses. On that sample the aggregate was low too. The danger is a batch
where such addresses are a minority: a geocoder that fails them can still clear an aggregate
floor, because the aggregate is one number over every kind of address.

`--by-class` sorts every row by the class of its submitted address and judges each class against
the same floor as the batch. The run below is synthetic: 1,000 addresses, 880 of them plain and 80
of them directional, from a geocoder that fails the directional ones. The aggregate clears 90
percent, and the tool still refuses the batch:

```
$ python geocodesift.py --csv vendor.csv --by-class
note: Status not in the CSV, so a tied match cannot be caught
rows audited: 1000
  TRUST       914
  SUSPECT      86
  REJECT        0
TRUST rate: 91.4% (floor 90.0%)
...

address classes (TRUST rate by class, floor 90.0%, 30 rows to judge a class):
  class             rows  TRUST    rate  95% interval     verdict
  all               1000    914   91.4%   89.5% -  93.0%  aggregate
  directional         80     10   12.5%    6.9% -  21.5%  BELOW
  numbered_street     60      7   11.7%    5.8% -  22.2%  BELOW
  unit                30     29   96.7%   83.3% -  99.4%  ok
  po_box               6      0       -    0.0% -  39.0%  BELOW
  rural_route          4      0       -    0.0% -  49.0%  BELOW
  plain              880    875   99.4%   98.7% -  99.8%  ok
  suffix:ST          148     94   63.5%   55.5% -  70.8%  BELOW
  suffix:DR          118    116   98.3%   94.0% -  99.5%  ok
  suffix:AVE         108     91   84.3%   76.2% -  89.9%  BELOW
  ...

FAIL: the batch clears the trust floor, but 6 address class(es) do not: directional 12.5%, numbered_street 11.7%, po_box 0.0%, rural_route 0.0%, suffix:ST 63.5%, suffix:AVE 84.3%. Do not publish it.
```

Without `--by-class`, the same file prints `PASS: the batch clears the trust floor.` and exits 0.

`--compare` reads a second result file for the same addresses, such as a control geocoder, and
prints its class table beside this one. The gap is the control's rate minus this file's rate, in
points. Only the file named by `--csv` is gated. The `control.csv` below is synthetic and already
in the `census` profile's columns; a real Census result must be reshaped into them first, as the
profile section says:

```
$ python geocodesift.py --csv vendor.csv --compare control.csv --compare-profile census
...
control file control.csv: 1000 rows, TRUST rate 97.9% (979 TRUST, 0 SUSPECT, 21 REJECT)

address classes (TRUST rate by class, floor 90.0%, 30 rows to judge a class):
  class             rows  TRUST    rate  95% interval     verdict      |  rows  TRUST    rate  95% interval     gap
  all               1000    914   91.4%   89.5% -  93.0%  aggregate    |  1000    979   97.9%   96.8% -  98.6%  +6.5
  directional         80     10   12.5%    6.9% -  21.5%  BELOW        |    80     80  100.0%   95.4% - 100.0%  +87.5
  numbered_street     60      7   11.7%    5.8% -  22.2%  BELOW        |    60     60  100.0%   94.0% - 100.0%  +88.3
  unit                30     29   96.7%   83.3% -  99.4%  ok           |    30     29   96.7%   83.3% -  99.4%  +0.0
  po_box               6      0       -    0.0% -  39.0%  BELOW        |     6      0       -    0.0% -  39.0%
  ...
```

How the table is built:

- **A match is a TRUST row.** The class rate uses the same test as the aggregate TRUST rate. A
  class cannot pass on rows that the audit already refused, such as a ZIP centroid.
- **A row is in every class that applies to it.** `1234 NE 31ST ST` counts in `directional`,
  `numbered_street` and `suffix:ST`. The class rows therefore do not add up to the total.
- **Each rate has a 95 percent Wilson interval.** The normal approximation reports 0 of 10 as
  exactly 0 with no uncertainty. The Wilson interval of 0 of 10 runs to 27.75 percent.
- **A small class is judged by its exact interval.** Under `--min-class-rows` (default 30), a
  class prints its counts, a dash for the rate and its Wilson interval. The verdict comes from the
  exact (Clopper-Pearson) 95 percent interval, which is wider at the ends. The class is `BELOW`
  when the whole exact interval is under the floor, and `ok` when the whole of it is at or above
  the floor. Only an interval that holds the floor is refused as `too few rows`. 2 of 3 runs from
  9 to 99 percent, which says nothing about a 90 percent floor. 2 of 20 runs from 1 to 32
  percent, which is a failing class, and the gate fails it. 0 of 1 runs to 97.5 percent and is
  refused, though its Wilson interval printed beside it ends at 79 percent. Judged by the Wilson
  bound, one missed row in a rare class failed a batch at 99.9 percent.
- **A PASS names what it did not judge.** When a refused class sits under a PASS, the PASS line
  adds `N address class(es) had too few rows to judge:` and their names.
- **The floor is `--min-trust-rate`.** From `--min-class-rows` up, a class fails when its rate is
  below that floor. The gate then uses the rate, not the interval, and the interval is printed so
  that a reader can judge a narrow miss.
- **The control has its own columns and floors.** `--compare` reads the control with the columns
  and the score floors of `--compare-profile`. The `--*-field` overrides and `--trust-score` and
  `--suspect-score` apply only to the `--csv` file. The boundary, pile-up and house-number
  settings apply to both files. The control line prints its TRUST, SUSPECT and REJECT counts, so
  the floors it was judged by can be checked.

The classifier reads only the submitted address column. It is deterministic, and these are all
of its rules:

| Class | Rule | Example |
|---|---|---|
| `blank` | The address has no token. | an empty cell |
| `po_box` | Starts `PO BOX`, `P O BOX` or `POST OFFICE BOX`. No other class applies. | `P.O. Box 12` |
| `rural_route` | Starts `RR`, `HC`, `HCR`, `RURAL ROUTE` or `HIGHWAY CONTRACT`. No other class applies. | `RR 2 BOX 15` |
| `no_number` | The first token has no leading house number. | `NE 31ST ST` |
| `directional` | A directional before the street name, with a token after it. The exception is a suffix word straight after the directional that ends the street, with nothing after it but a directional or a unit word: the directional is then the street's name, and the suffix word stays the suffix. A directional straight after the last suffix or after a route number also counts. | `100 N MAIN ST`, `100 N COURT ST`, `100 MAIN ST NW` |
| `route` | A route lead (`CR`, `SR`, `US`, `HWY`, `COUNTY` and others), any further route words such as `ROAD`, then a house number such as `900` or `900A`. `ROAD` and `RD` never start a route, so `100 ROAD 5` is plain. | `1200 COUNTY ROAD 900`, `100 CR 900A` |
| `numbered_street` | The street name is an ordinal, or a bare number before a suffix. An ordinal is a number that ends `ST`, `ND`, `RD` or `TH`, or a spelled word from `FIRST` to `NINETEENTH`, a tens word (`TWENTIETH` to `NINETIETH`), `HUNDREDTH`, or a tens word and `FIRST` to `NINTH` (`TWENTY-FIRST` or `TWENTY FIRST`). | `55 NE 62ND AVE`, `100 SECOND AVE`, `100 TWENTY-FIRST ST` |
| `unit` | A unit word (`APT`, `UNIT`, `STE`, `#`, `LOT` and others) after the first word of the street name, with a token after it. | `100 MAIN ST #4` |
| `plain` | A house number and none of `directional`, `route`, `numbered_street` or `unit`. | `100 MAIN ST` |
| `suffix:XX` | The last suffix before any unit word, as its USPS abbreviation, or `suffix:none`. The first word of the street name is never the suffix. A route has no suffix class. | `suffix:ST` |

The tokens are the text before the first comma, in upper case, with full stops removed. A second
comma part that starts with a unit word is kept, so `100 MAIN ST, APT 4` is a unit. A fraction
after the house number, such as `123 1/2`, is skipped. `100 NORTH ST` is plain, because `ST` ends
the street and `NORTH` is its name. `100 N COURT ST` is directional, because `COURT` has `ST` after
it. `100 N HWY 999` is directional, because the route number follows `HWY`. `100 ST JOHNS AVE` is
an avenue and `100 COURT` has no suffix, because the first word of the name is never the suffix.
The self-test holds a case for each rule and for each exception.

## A tier ladder: the tiers profile

Some records have no clean address, so their locator joins a key, such as a parcel number,
instead of parsing an address. Each record gets a tier: it matched an address point, it matched a
parcel point, it fell back to the parcel's centroid, the parcel was not found, or it had no key.
The dashboard shows the top tiers only. Then a row labelled `unlocated` turns out to carry a
point. The point is left over from an earlier run, because the rebuild wrote the new tier and
kept the old geometry. That row is below every dashboard filter and on the map at the same time,
and no count shows it.

`--profile tiers` reads that ladder. It has no score column, as the `census` profile has none, so
the tier carries the whole verdict. These are the default lists:

| Verdict | Tiers |
|---|---|
| TRUST | `address_point`, `parcel_point` |
| SUSPECT | `parcel_centroid` |
| REJECT | `parcel_not_found`, `no_key`, `unlocated` |

A reject tier means "this row has no location". A reject row with no coordinate is an ordinary
REJECT. A reject row that carries a coordinate is REJECT with its own reason, and it fails the
batch at any TRUST rate, a floor of 0 included. Put a tier that carries a point but must not be
trusted, such as a centroid, in `--suspect-tiers`, not in `--reject-tiers`.

The tier lists are closed. A tier in none of the three lists stops the run with exit 64 and
names the tier. The other profiles read an unknown match type as SUSPECT. Here an unknown tier
could hide a new fallback that the locator started to write. A tier named in two lists exits 64 before the
CSV is read, because one list would win in silence. Tier names are read stripped and in lower
case. A blank tier is SUSPECT, as a blank match type is in the other profiles.

A real run against 200 synthetic rows: 170 address points, 18 parcel points, 4 centroids,
6 unlocated rows with no point, and 2 unlocated rows that kept an old point:

```
$ python geocodesift.py --csv records.csv --profile tiers
rows audited: 200
  TRUST       188
  SUSPECT       4
  REJECT        8
TRUST rate: 94.0% (floor 90.0%)

rows in a non-located tier that still carry a point: 2 (row 200, 201)

first 5 row(s) that are not TRUST:
  row 190 SUSPECT: match type 'parcel_centroid' is not a house-level match
  ...

FAIL: the batch clears the trust floor, but 2 row(s) in a non-located tier carry a point, so they sit below every filter and on the map at once. Do not publish it.
```

The same file with those two points blanked passes. With `--compare`, a control file whose
profile is also `tiers` is read with the same tier lists.

## One record, many rows: the fan-out

A dashboard counts permits by area. To give each permit a location, a job joins the permits to
address points on the parcel number. A parcel often holds several address points, so the join
emits one row for each point, and every count on the dashboard is inflated. No row is wrong on
its own line. Every row sits on a real address point, so the pile-up check sees nothing either.

`--key-field` names the column that must be unique for each record. A key on more than one row
fails the batch at any TRUST rate. Every row of that key drops to SUSPECT with the reason, and
the report lists the repeated keys and counts the extra rows. A blank key names no record, so it
is not counted. A real run against 40 synthetic records, 10 of which sit on parcels with three
address points each:

```
$ python geocodesift.py --csv joined.csv --profile tiers
rows audited: 60
...
PASS: the batch clears the trust floor.

$ python geocodesift.py --csv joined.csv --profile tiers --key-field record_id --min-trust-rate 0
rows audited: 60
  TRUST        30
  SUSPECT      30
  REJECT        0
TRUST rate: 50.0% (floor 0.0%)

record keys on more than one row (20 extra row(s)):
  key 'R031' on 3 rows
  ...
FAIL: the batch clears the trust floor, but 10 record key(s) are on more than one row, so a join fanned out and every count is inflated. Do not publish it.
```

geocodesift finds the fan-out. The fix belongs in the query that made it: give each record at
most one location inside the join, rather than remove duplicates afterwards. A de-duplication
step after the join must still choose a row, and it hides the fact that the join was wrong. In
T-SQL, the usual form is `OUTER APPLY` with `TOP (1)`. The table and column names are synthetic:

```sql
SELECT r.record_id, pt.x, pt.y
FROM records AS r
OUTER APPLY (
    SELECT TOP (1) a.x, a.y
    FROM address_points AS a
    WHERE a.parcel_key = r.parcel_key
    ORDER BY a.is_primary DESC, a.point_id
) AS pt;
```

This repository did not run that query against SQL Server. It relies on documented behaviour:

- An `INNER` join returns all matching pairs of rows, so one record and three points give three
  rows ([FROM clause, JOIN and APPLY](https://learn.microsoft.com/en-us/sql/t-sql/queries/from-transact-sql)).
- `OUTER APPLY` produces one row for each row of the left table, even when the right side
  returns nothing. A record with no point survives with NULLs, and can fall to a lower tier
  (same page).
- `ORDER BY` with `TOP` is the only way to say predictably which rows `TOP` returns
  ([TOP](https://learn.microsoft.com/en-us/sql/t-sql/queries/top-transact-sql)). An `ORDER BY`
  that can tie does not name one row, so end it with a unique column, as `a.point_id` above.

**Materializing the locator.** An indexed view cannot hold this lookup. Its definition must not
contain `SELECT TOP`, `OUTER APPLY`, `CROSS APPLY` or `LEFT OUTER JOIN`
([Create indexed views](https://learn.microsoft.com/en-us/sql/relational-databases/views/create-indexed-views),
checked 2026-10-13). So a workable design is a plain table that a scheduled job rebuilds from the query. Run
geocodesift on an export of that table before it is published, and publish only on exit 0. Two
traps belong to the rebuild itself. A fan-out in the query multiplies the table, and
`--key-field` catches it. A rebuild that pulls only part of its source still writes a table.
geocodesift fails an empty table, but a short table of good rows passes, so guard the swap with
a row-count check such as [safe-republish](https://github.com/uhsear/safe-republish).

dbt does both checks well inside a dbt project. Its built-in `unique` test returns every key with
more than one row, and its `accepted_values` test refuses a value outside a declared list
([dbt data tests](https://docs.getdbt.com/docs/build/data-tests)). They need the table in a
warehouse that dbt builds. geocodesift reads the exported CSV that a publish job already has, and
puts the key count and the location checks behind one exit code.

## Why not the geocoder's own report

Esri's Geocode Addresses already writes `Status`, `Score` and `Addr_type` in its Minimal output.
That is the right data. ArcGIS Pro also has a rematch interface, which is the right tool for
fixing twenty addresses by hand. Nobody rematches two thousand rows by eye, and the interface
gives a scheduled job no exit code.

The gap is the aggregate refusal. A scheduled job needs one number it can fail on, and it needs
the pile-up count, which is a property of the batch rather than of any row. No per-row field can
express "these 380 records are the same point".

The `esri` profile exists so this reads that same output file unchanged. It does not re-geocode
anything and it has no opinion about which geocoder you used.

For the class table, a pivot table does the same job once. Tag each address with a class, then
pivot the match status by that tag. That gives the right rates for one batch, and it is quick. It
gives no interval, no refusal for a small class and no exit code, and someone must tag the next
batch again. Geocoder benchmark scripts do the other half well: they send one address list to
several live geocoding services and measure how far each returned point is from a reference point.
That measures positional accuracy, which this tool does not. It needs network access to each
service, and it cannot audit a result file that is already on disk.

## Limits

- It never fixes a row. It classifies, reports and exits. Rematching is still the rematch pane's
  job.
- TRUST means the geocoder's own fields are self-consistent, not that the point is correct. A
  confident rooftop match on the wrong street passes.
- The house number check reads the first run of digits in the address. A matched address that
  leads with a ZIP code, such as `12346, Anytown, Anystate`, reports a huge house number difference.
  That row is already REJECT on its match type, so the noise lands only in the reason text. A run
  of ten digits or more is not read as a house number. That row has no house number check, and
  the classifier puts it in `no_number`.
- Ray casting treats coordinates as plane geometry. A boundary in decimal degrees is tested as if
  it were flat, which is accurate at county scale and wrong for a continent.
- Both the CSV and the boundary are read into memory. This repository ran the audit on batches of
  a few hundred rows only, not on a large file, so it states no row limit. A national file is
  likely to be too large.
- No projection handling. The coordinates in the CSV and the coordinates in the GeoJSON must
  already be in the same system, and nothing checks that they are.
- The match type lists are per profile and are not exhaustive. An unknown type is SUSPECT, never
  TRUST, so a locale-specific type that this does not know will hold the batch instead of passing
  it.
- A cell reading `nan`, `inf` or `1e400` is treated as an absent value, not a number. `float()`
  accepts all three, and a NaN score clears every floor because every comparison against NaN is
  false. The same rule applies to coordinates: a NaN coordinate is REJECT, never a point.
- A Census batch result does not fit the `census` profile as it comes. It has no header, one
  `lon,lat` cell and a two-column match verdict, so it must be reshaped before the audit. A
  Census file in its own layout exits 2.
- Nominatim's `importance` is a prominence measure, not a match confidence. The `nominatim`
  profile sets its floors low on purpose and lets the match type decide.
- The address classifier is for US-style addresses in English. It reads only the street line,
  which is the text before the first comma. In a single-line address with no commas, it also
  reads the city and state. A state abbreviation that is also a suffix, such as `CT`, is then
  read as the suffix, and a city that starts with a directional word adds `directional`. A city
  after a street named for a direction, as in `100 NORTH ST ANYTOWN`, makes the directional word
  read as a directional.
- A spelled ordinal above `HUNDREDTH`, such as `ONE HUNDRED FIRST`, is not read as one. That
  street lands in `plain`. Written as a number, `101ST`, it is a numbered street.
- The last suffix wins. `100 LAKE WAY DR` is a drive, which is right. A street name that ends in
  a suffix word and has no suffix of its own reads as that suffix.
- The suffix table is the common part of USPS Publication 28, not the whole appendix. Words that
  are also common street names, such as `PARK`, `HILL` and `LAKE`, are left out. A street with only
  one of those reads as `suffix:none`.
- `--compare` does not join the two files row by row. It builds a class table for each file and
  prints them side by side, so the two files must hold the same address list for the gap to mean
  anything. Nothing checks that they do.
- `--compare` reads the control with the default columns of `--compare-profile`. A control file
  with renamed columns must be renamed back first, and a Census control must be reshaped first.
- A small class fails on its exact interval alone, and a very small class can fail on two rows.
  At a 90 percent floor, 0 of 2 fails, because its exact interval runs only to 84 percent. Two
  blank addresses that did not geocode therefore fail a `--by-class` run. A class whose interval
  holds the floor is refused and passes, and the PASS line names it.
- `--apply` copies every cell value unchanged, including bytes that are not UTF-8, such as a cp1252
  export from Excel. The surplus cells of a row that has more cells than the header has columns
  are dropped, because they have no column to go in.
- From `--min-class-rows` up, the class gate uses the rate, not the interval. A class of 30 rows
  at 86.7 percent fails a 90 percent floor, though its interval reaches 94.7 percent. Raise
  `--min-class-rows` when that is too strict for the batch size.
- The printed interval is the Wilson interval, and a small class is judged by the exact one. At
  0 of 1 the row prints a Wilson high of 79.3 percent beside `too few rows` at a 90 percent floor,
  because the exact high is 97.5 percent. The exact interval is conservative: it is often wider
  than a 95 percent interval needs to be, so a small class is refused a little more often.
- Under `--profile tiers`, the records of one parcel can share its point. Five or more on one
  point are a pile-up and drop to SUSPECT. Raise `--pileup-threshold` for a key-joined batch.
- A reject tier is read as "no location". A tier that carries a point by design, such as a
  centroid, belongs in `--suspect-tiers`, or every row in it fails the batch.
- `--key-field` finds a repeated key. It cannot tell which row of that key is the right one, so
  every row of the key drops to SUSPECT. It reads only the `--csv` file, not the `--compare` file.
- Keys are compared as text, case sensitive, with leading and trailing spaces trimmed. `R001` and
  `r001` are two keys, and so are `0123` and `123`, so a fan-out that changes the case of a key or
  drops its leading zeros is not found.
- A stale point and a repeated key are checked in the `--csv` file only. A `--compare` control
  file is audited for verdicts, and an old point or a repeated key in it does not fail the run.
- A reject-tier row is caught as carrying an old point only when both X and Y hold a number. A
  row with one of the two filled is REJECT as having no usable coordinate, and it is not counted
  as a point, so the batch can pass.
- A coordinate cell that is not a number counts as absent: `<Null>`, and a point written with a
  comma for the decimal such as `-82,3001`. A reject-tier row holding one is not stale, and a
  trust-tier row holding one is REJECT.
- A coordinate of 0, 0 counts as a point. A reject-tier row at 0, 0 fails the batch, though 0, 0
  is often a pipeline's stand-in for no location. Blank the columns of a row that has no
  location.
- Exit 64 also means an unlisted tier in the data, such as the text `NULL` or `None` that a SQL
  export writes. The cause is the data, not the command line, and the message names the tier. A
  scheduler that treats 64 as a bad command line will misread it.
- A blank tier is SUSPECT even when the row carries no point.
- A row number in a message counts CSV records, not physical lines, so a cell with a line break
  inside quotes makes later numbers differ from an editor's line numbers. A line holding only
  spaces is not blank to the reader: it becomes one REJECT row.
- `--apply` does not copy the file byte for byte. It drops the UTF-8 byte order mark and the
  blank lines of the input. It also writes straight to `--out`, so a write that dies midway leaves
  a truncated file, and it writes on a failing batch too. Publish only on exit 0.
- A boundary FeatureCollection that holds a `Point` feature beside its polygons exits 2.
- The printed reasons and the `--out` reasons column echo tier text and coordinates from the
  input, so a scheduled job's log holds row-level data.
- A partial pull is not caught. An empty batch fails, but a batch of one good row passes. Check
  the row count against the source before a rebuilt table replaces the old one.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [arcpy-nullscan](https://github.com/uhsear/arcpy-nullscan) - the NULLs a geocoded table also carries
- [tzrot](https://github.com/uhsear/tzrot) - another way a valid-looking column is wrong
- [ringwind](https://github.com/uhsear/ringwind) - a ring wound the wrong way makes every point-in-polygon test answer backwards
- [safe-republish](https://github.com/uhsear/safe-republish) - refuses to replace a table with a staged copy that came back short
- [nalmatch](https://github.com/uhsear/nalmatch) - the parcel key a join matches on, checked against the roll before anything joins on it

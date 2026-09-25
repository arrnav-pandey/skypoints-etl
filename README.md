# SkyPoints — Member & Redemption Ingestion

[![tests](https://github.com/arrnav-pandey/skypoints-etl/actions/workflows/tests.yml/badge.svg)](https://github.com/arrnav-pandey/skypoints-etl/actions/workflows/tests.yml)

Incubyte Data Engineer technical assessment.

A daily batch that ingests member profile feeds and a partner redemption feed,
validates and conforms them, and routes members into per-country target tables.

```
USA.csv  ─┐
IND.csv  ─┼─▶ raw (immutable) ─▶ staging (typed, derived, judged) ─┬─▶ TABLE_USA, TABLE_INDIA, …
AUS.xlsx ─┤                                                        ├─▶ REDEMPTION_TXN
members.dat ─┤                                                     └─▶ quarantine + run report
redemptions.json ─┘
```

---

## Run it

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

.venv/bin/python -m pytest -q          # 104 tests
```

**The supplied country feeds** (`USA.csv`, `IND.csv`, `AUS.xlsx`):

```bash
.venv/bin/skypoints ingest --dir data/incoming --as-of 2022-12-31 --out out
```

**The pipe-delimited feed + JSON redemptions** described in the PDF:

```bash
.venv/bin/skypoints run \
  --members     data/sample/SKYPOINTS_MEMBERS_20240115_10300000.dat \
  --redemptions data/sample/SKYPOINTS_REDEMPTIONS_20240115_10300000.json \
  --as-of       2024-01-15 --out out
```

Outputs: one CSV per country table, `redemption_txn.csv`, `quarantine.csv` and
`run_report.json`.

---

## Deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | DDL for raw, staging and per-country target tables | [sql/01_raw_ddl.sql](sql/01_raw_ddl.sql), [sql/02_staging_ddl.sql](sql/02_staging_ddl.sql), [sql/03_target_ddl.sql](sql/03_target_ddl.sql), [sql/08_country_source_ingestion.sql](sql/08_country_source_ingestion.sql) |
| 2 | Staging load with `Age` and `Stale_Member` | [sql/04_stage_load.sql](sql/04_stage_load.sql), [src/skypoints/transform.py](src/skypoints/transform.py) |
| 3 | Split into per-country tables, latest record wins | [sql/05_country_split.sql](sql/05_country_split.sql), [src/skypoints/transform.py](src/skypoints/transform.py) |
| 4 | Flatten the JSON feed + join back to members | [sql/06_redemption_flatten.sql](sql/06_redemption_flatten.sql), [src/skypoints/redemptions.py](src/skypoints/redemptions.py) |
| 5 | Data validations | [sql/07_validations.sql](sql/07_validations.sql), [src/skypoints/validation.py](src/skypoints/validation.py), [src/skypoints/sources.py](src/skypoints/sources.py) |
| 6 | Live demonstration | the CLI above |

Both a SQL and a Python implementation are provided. The SQL is the production
path on Snowflake; the Python is the same logic as a dependency-light, testable
pipeline so the behaviour can be demonstrated and unit-tested without a
warehouse attached.

---

## What the supplied data actually contains

The assessment ships three member files. No two agree on schema, format or date
encoding, and each one carries a different defect. **Every issue below was found
by running the files, not by reading them.**

### The three contracts

| File | Format | Columns | Dates |
|---|---|---|---|
| `USA.csv` | CSV | `ID, Name, TierCode, EnrollmentDate, FlightDate` | `MDYYYY` as a **number** |
| `IND.csv` | CSV | `ID, Name, DOB, TierCode, EnrollmentDate, Individual or Corporate, Flight Date` | `M/D/YYYY` text |
| `AUS.xlsx` | XLSX | `Unique ID, Member Name, Tier Type, Date of Birth, Date of Enrollment, Date of Flight` | Excel datetimes + text |

### The defects

**1. Country is carried only by the filename.** No file has a country column.
An unresolvable filename is refused rather than defaulted — guessing would route
real members into the wrong country's table.

**2. `1052022` is 5 January, not 5 October.** USA dates are `MDYYYY` written as
numbers, so `6152022` has lost the month's leading zero while `12282021` in the
same column kept all eight digits. The subtle part: `strptime` matches greedily
and *does not backtrack when the greedy read is valid* — `%m` takes `10`, `%d`
takes `5`, and October is returned with no error raised. But 5 October would
have arrived as `10052022`, eight digits. A 7-digit value has lost exactly one
leading zero, so only the zero-padded reading is admissible. **My first test
missed this; running the real file caught it.**

**3. USA ships no date of birth at all.** `Age` is therefore `NULL` for an
entire market — never `0`, never estimated. A fabricated age would silently
corrupt every age-based segment. This is a `WARNING` (`age_underivable`), not a
rejection: the member is perfectly valid, the attribute simply does not exist.

**4. `AUS` sends the literal string `"NULL"`.** Four characters, not an empty
cell. Loaded naively it becomes the text `'NULL'`, which is not null and defeats
every `IS NULL` check downstream.

**5. `AUS` contains `2021-13-13` — month 13.** There is no defensible repair, so
the record is quarantined with its reason rather than silently shifted into 2022.
The columns that *did* parse are preserved, so the row can be fixed and replayed.

**6. `IND` carries `Individual or Corporate`**, a column no specification
mentions. It is retained in an `extras` map rather than dropped — silently
discarding a column the source chose to send is how real attributes get lost for
months.

**7. `ID` 1 is Sam in USA, Vikas in IND and Mike in AUS.** Three different
people, one identifier — see below.

### The identifier contradiction

This is the most consequential finding, and it is a genuine contradiction in the
assessment rather than something to quietly pick a side on.

The brief requires *"latest record wins when a member has moved countries"*,
which needs an identifier that is **stable across countries**. The supplied data
provides no such identifier: IDs 1, 2 and 3 each appear in all three files, with
different names every time.

Both readings cannot be satisfied at once:

- Key on `member_id` alone → latest-record-wins **merges three different people
  into one**. Unrecoverable.
- Key on `(country, member_id)` → distinct people stay distinct, but a genuine
  relocation looks like two members.

The pipeline takes the second, because the failure mode is recoverable and the
first is not. Collisions are **reported, never auto-resolved** — `run_report.json`
lists them and `V_ID_COLLISIONS` flags whether the names differ, which is
near-certain evidence of independent numbering rather than relocation. If the
source can supply a global member ID, the key becomes that ID and relocation
handling works exactly as the brief describes.

### Discrepancies in the PDF itself

The pipe-delimited spec in the PDF also disagrees with its own examples: the
layout declares 11 columns but the sample sends 10 (no `Post Code`); it names
`Member Name` as the key column, which cannot be unique; and `DOB` `03051985`
is `DDMMYYYY`, not the stated `YYYYMMDD` — as `YYYYMMDD` it would be month 19,
day 85. All three are handled explicitly and documented in
[src/skypoints/spec.py](src/skypoints/spec.py).

---

## Design decisions

**Source differences are data, not code.** Each country's schema, format and
date encoding is a `SourceContract` ([src/skypoints/sources.py](src/skypoints/sources.py))
and `REF_SOURCE_CONTRACT` in SQL. One reader serves CSV and XLSX; onboarding a
market is a new contract, not a new module. Three bespoke readers would have
tripled the code and guaranteed drift.

**Per-country tables are the requested design, not the one I would choose
unprompted.** On Snowflake, a single table clustered by `COUNTRY_CODE` prunes
partitions just as well, with no DDL per market, no N-way `UNION` for global
reporting and no schema drift. What *does* justify physical separation is data
residency and access control — if India's data must sit in an India-region
account, or country teams must be structurally unable to read each other's
members. Those are good reasons; storage layout is not. The brief's design is
implemented, and `V_MEMBER_GLOBAL` re-unifies it so reporting is not punished.

**Relocation is the subtle part of "latest record wins."** A `MERGE` into
`TABLE_INDIA` cannot see the stale row still in `TABLE_USA`, so the member
silently exists twice and every country-level count is wrong. Moves are detected
during resolution and the old table reported. The delete runs *after* the insert:
a duplicate is recoverable, a deleted-and-never-inserted member is data loss.

**Redemptions are deliberately not split by country.** Transactions are global —
a member in India redeems on a US partner — and a member's country can change,
which would force transactions to migrate and silently rewrite history.

**Severity is a product decision.** Blocking on every anomaly means the pipeline
halts nightly and people start ignoring it; blocking on nothing means bad data
reaches the business. `ERROR` quarantines, `WARNING` annotates. An unknown tier
code warns — a new tier is a plausible marketing decision. An unmappable country
errors — the record cannot be routed at all.

**Nothing is dropped silently.** Rejected rows go to quarantine with reasons.
A row that fails today is often the evidence that fixes the source tomorrow.

**Derived values are anchored on the batch date, never `CURRENT_DATE()`.**
Otherwise re-running last quarter's batch produces different numbers and no
reconciliation can be trusted. There is a test asserting byte-identical output
across runs.

**"Never flown" is not "stale."** `Stale_Member` is `NULL`, not `TRUE`, when
`Last_Flight_Date` is absent. Merging the two would mis-target re-engagement.

---

## Designing for billions of rows a day

- **Streaming.** Parsers are generators; memory is flat whether the file has 10
  rows or 10 billion.
- **Set-based SQL, no row-by-row UDFs.** At this volume that is the difference
  between minutes and hours.
- **Clustering matched to access.** Staging clusters on `(BATCH_DATE,
  COUNTRY_CODE)` — exactly what routing filters on.
- **VARIANT rather than pre-parsed JSON.** Snowflake shreds VARIANT columnar, so
  flattening touches only the attributes it reads.
- **JSON Lines** is the form to insist on from partners: splittable and
  streamable. Object and array forms are supported so the PDF sample runs as-is.
- **Bounded-memory uniqueness.** The in-flight tracker stores key hashes, not
  rows. The authoritative check stays in the warehouse.
- **Reconciliation, not just success.** `raw = staged + quarantined` catches the
  dropped micro-batch no row-level rule can see. Volume-anomaly detection
  catches a truncated delivery, because a batch that is 10% of yesterday is far
  more likely to be a broken file than a collapse in enrolment.

---

## Project layout

```
src/skypoints/
  sources.py       per-country source contracts, CSV + XLSX readers
  spec.py          the pipe-delimited record layout, declared as data
  config.py        country conformance, routing, tunables
  parser.py        streaming flat-file reader
  coerce.py        type casting that reports instead of raising
  validation.py    record / row / batch rules with severities
  transform.py     derived columns, latest-record-wins, routing
  redemptions.py   JSON flattening and current-state dedup
  pipeline.py      orchestration and run report
  cli.py           entry point
sql/               Snowflake DDL and transformation logic
tests/             104 tests
data/incoming/     the files supplied with the assessment, unmodified
data/sample/       feeds matching the PDF's pipe-delimited spec
```

---

## On the use of AI

AI was used throughout, as the brief requires, and the division of labour was
deliberate. It was fast at mechanical breadth — Snowflake `MERGE` and `LATERAL
FLATTEN` syntax, parametrised test scaffolding, boilerplate DDL — and I kept
that output.

It was not the source of the decisions that matter, and twice it was actively
wrong in ways that would have shipped:

- An AI review of the SQL flagged `REGEXP_SUBSTR(..., 'e', 1)` as invalid in
  Snowflake. It is valid — `e` means "extract submatches". I checked the
  documentation rather than applying the "fix", which would have broken working
  code. The same review correctly caught a real defect: a session variable
  inside a view definition, which makes the view non-reproducible.
- The `1052022` → 5 October bug came from trusting `strptime` to fail loudly on
  an ambiguous parse. It does not. Only running the supplied file exposed it,
  and only reasoning about field width resolved it.

The `DDMMYYYY` date of birth in the PDF was found the same way — by running the
brief's own sample and reading the failure. A model reproduces what the document
says, confidently, because that is what the document says.

Three tests failed on first run and in all three cases my assertion was wrong
and the implementation right; those were corrected in the tests, not by bending
the behaviour. That is the discipline that makes AI-accelerated work safe: the
generated code is a proposal, and the tests plus the real data decide.

The source-contract work was done test-first — see the `[RED]` / `[GREEN]`
commit pairs in the history.

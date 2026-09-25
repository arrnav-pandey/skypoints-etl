# SkyPoints — Member & Redemption Ingestion

Incubyte Data Engineer technical assessment.

A daily batch that takes two source feeds — a pipe-delimited flat file of member
profiles and a semi-structured JSON feed of partner redemptions — validates and
conforms them, and routes members into per-country target tables.

```
flat file ─┐
           ├─▶ raw (immutable) ─▶ staging (typed, derived, judged) ─┬─▶ TABLE_INDIA, TABLE_USA, …
JSON feed ─┘                                                        ├─▶ REDEMPTION_TXN
                                                                    └─▶ quarantine + run report
```

---

## Run it

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

.venv/bin/python -m pytest -q          # 79 tests

.venv/bin/skypoints run \
  --members    data/sample/SKYPOINTS_MEMBERS_20240115_10300000.dat \
  --redemptions data/sample/SKYPOINTS_REDEMPTIONS_20240115_10300000.json \
  --as-of      2024-01-15 \
  --out        out
```

Outputs land in `out/`: one CSV per country table, `redemption_txn.csv`,
`quarantine.csv` and `run_report.json`.

To see the failure paths, run the second sample file — it carries a member who
relocates mid-batch, a truncated date, an unmappable country, an over-length
field, a missing mandatory name and a future date:

```bash
.venv/bin/skypoints run \
  --members data/sample/SKYPOINTS_MEMBERS_20240116_10300000.dat \
  --as-of   2024-01-16 --out out-dirty
```

---

## Deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | DDL for raw, staging and per-country target tables | [sql/01_raw_ddl.sql](sql/01_raw_ddl.sql), [sql/02_staging_ddl.sql](sql/02_staging_ddl.sql), [sql/03_target_ddl.sql](sql/03_target_ddl.sql) |
| 2 | Staging load with `Age` and `Stale_Member` | [sql/04_stage_load.sql](sql/04_stage_load.sql), [src/skypoints/transform.py](src/skypoints/transform.py) |
| 3 | Split into per-country tables, latest record wins | [sql/05_country_split.sql](sql/05_country_split.sql), [src/skypoints/transform.py](src/skypoints/transform.py) |
| 4 | Flatten the JSON feed + join back to members | [sql/06_redemption_flatten.sql](sql/06_redemption_flatten.sql), [src/skypoints/redemptions.py](src/skypoints/redemptions.py) |
| 5 | Data validations | [sql/07_validations.sql](sql/07_validations.sql), [src/skypoints/validation.py](src/skypoints/validation.py) |
| 6 | Live demonstration | the CLI above |

Both a SQL and a Python implementation are provided. The SQL is the production
path on Snowflake; the Python is the same logic expressed as a dependency-free,
testable pipeline so the behaviour can be demonstrated and unit-tested without a
warehouse attached.

---

## What the sample data actually contains

Reading the brief carefully, then running its own sample through the parser,
surfaced five discrepancies. Each is handled explicitly rather than smoothed
over, because in a real engagement each one is a conversation with the source
system owner.

**1. The layout declares 11 columns; the file sends 10.**
`Post Code` sits at file position 9 in the design document but appears in
neither the header nor the detail records. Binding columns by ordinal position
would shift `DOB` and `Is_Active` by one and corrupt every row. Fields are bound
by *header name*, and the absent column is reported as schema drift.

**2. `Member Name` is declared the key column.**
Two different members can share a name; deduplicating on it would merge distinct
people and lose one of them. `Member ID` is used as the deduplication key, and
the divergence is carried in the run report rather than being silently resolved.

**3. `DOB` is not in the stated date format.**
`03051985` is not a valid `YYYYMMDD` value — that would be month 19, day 85. It
is `DDMMYYYY`. Date format is declared per column, with alternates tried in
order and a warning raised when a fallback fires, so a drifting source is
visible without rejecting otherwise-valid members.

**4. The intermediate table shows `3051985`.**
The leading zero is gone: something upstream handled an 8-digit date as a
number. This gets its own rule (`date_leading_zero_lost`) because the fix is in
the source system's cast, not in this data — a generic "bad date" error would
send someone looking in the wrong place.

**5. Country codes are inconsistent.**
`USA`, `IND` and `CAN` are ISO alpha-3; `PHIL` and `AU` are not. Since country
determines the target table, an unconformed code is a *routing* failure — the
member reaches no table at all. Values are conformed to ISO alpha-3 up front, so
a source later tidying `AU` to `AUS` does not look like every Australian member
relocating.

The intermediate table also misspells `Country` as `County` and drops
`Agent_Name` for four of five members; both are handled (alias, completeness
warning).

---

## Design decisions

**Per-country tables are the requested design, not the one I would choose
unprompted.** On Snowflake, a single `CURRENT_MEMBER` table clustered by
`COUNTRY_CODE` prunes partitions just as effectively, with none of the costs: no
DDL per new market, no N-way `UNION` for global reporting, no schema drift
between countries, and a relocation becomes an `UPDATE` rather than a
cross-table `DELETE` + `INSERT`. The arguments that *do* justify physical
separation are data residency and access control — if India's data must sit in
an India-region account, or country teams must be structurally unable to read
each other's members. Those are good reasons; storage layout is not. The brief's
design is implemented, and `V_MEMBER_GLOBAL` re-unifies it so reporting is not
punished for the split.

**Relocation is the subtle part of "latest record wins."** A `MERGE` into
`TABLE_INDIA` resolves which version of the member survives, but it cannot see
the stale row still sitting in `TABLE_USA` — so the member silently exists in
two countries and every country-level count is wrong. The pipeline detects moves
during resolution and reports the table to delete from. The delete runs *after*
the insert: a duplicate is a recoverable inconsistency, a deleted-and-never-
inserted member is data loss.

**Redemptions are deliberately not split by country.** Transactions are global —
a member in India redeems on a US partner — and a member's country can change,
which would force transactions to migrate between tables and silently rewrite
history. Country is reached by joining to the member.

**Severity is a product decision, not a technical one.** Blocking on every
anomaly means the pipeline halts nightly and people start ignoring it; blocking
on nothing means bad data reaches the business. `ERROR` means the record cannot
be loaded correctly, so it is quarantined with its reasons. `WARNING` means it
is loadable but something is degrading. An unknown tier code is a warning — a
new tier is a plausible marketing decision. An unmappable country is an error —
the record cannot be routed at all.

**Nothing is ever dropped silently.** Rejected rows go to quarantine with their
reasons, never to `/dev/null`. A row that fails today is often the evidence that
fixes the source tomorrow, and a quarantine table makes the cost of bad data
visible instead of hiding it in a row-count discrepancy.

**Derived values are anchored on the batch date, never `CURRENT_DATE()`.**
`Age` and `Stale_Member` computed from the wall clock make the table
non-deterministic: re-running last quarter's batch would produce different
numbers and no downstream reconciliation could be trusted. There is an
end-to-end test asserting byte-identical output across runs.

**"Never flown" is not "stale."** `Stale_Member` is `NULL`, not `TRUE`, when
`Last_Flight_Date` is absent. A member who never flew and a member who has not
flown in six months need different treatment, and merging them would mis-target
re-engagement campaigns.

---

## Designing for billions of rows a day

*The brief specifies scale, so these are choices, not afterthoughts.*

- **Streaming everywhere.** Nothing materialises the input. Parsers are
  generators, so memory is flat whether the file has 10 rows or 10 billion, and
  the sample and production paths are the same code.
- **Set-based SQL, no row-by-row UDFs.** At this volume the difference between a
  vectorised expression and a per-row function call is the difference between
  minutes and hours.
- **Clustering matched to access.** Staging clusters on `(BATCH_DATE,
  COUNTRY_CODE)` — exactly what the routing step filters on, so each country
  reads its own micro-partitions. `REDEMPTION_TXN` clusters on `(TXN_DATE,
  MEMBER_ID)`.
- **VARIANT rather than pre-parsed JSON.** Snowflake shreds VARIANT columnar, so
  flattening touches only the attributes it reads instead of re-parsing whole
  documents.
- **JSON Lines is the form to insist on** from partners: splittable across
  workers and streamable. Single-object and array forms are supported so the
  brief's sample runs unmodified.
- **Bounded-memory uniqueness.** The in-flight tracker stores key hashes, not
  rows, so its footprint scales with distinct members rather than volume. The
  authoritative uniqueness check stays in the warehouse.
- **Reconciliation, not just success.** `raw = staged + quarantined` catches the
  dropped micro-batch that no row-level rule can see. Volume anomaly detection
  catches a truncated delivery before it lands, because at this scale a batch
  that is 10% of yesterday is far more likely to be a broken file than a
  collapse in enrolment.

**Next steps beyond a batch:** Snowpipe for continuous landing, streams and
tasks to make the staging load incremental, and a `MEMBER_ID` hash to
horizontally partition the routing step across warehouses.

---

## Project layout

```
src/skypoints/
  spec.py          source record layout, declared once as data
  config.py        country conformance, routing, tunables
  parser.py        streaming flat-file reader
  coerce.py        type casting that reports instead of raising
  validation.py    record / row / batch rules with severities
  transform.py     derived columns, latest-record-wins, routing
  redemptions.py   JSON flattening and current-state dedup
  pipeline.py      orchestration and run report
  cli.py           entry point
sql/               Snowflake DDL and transformation logic
tests/             79 tests
data/sample/       clean and defect-carrying sample feeds
```

---

## On the use of AI

AI was used throughout, as the brief requires, and the division of labour was
deliberate. It was fast at mechanical breadth — Snowflake `MERGE` and `LATERAL
FLATTEN` syntax, parametrised test scaffolding, boilerplate DDL — and I kept
that output.

It was not the source of the decisions that matter. The `DDMMYYYY` date of birth
was found by running the brief's own sample through the parser and reading the
failure, not by inspection; the assumption that one stated format applied to
every column is exactly the kind of thing a model will reproduce confidently
because it is what the document says. The same applies to binding columns by
name rather than ordinal, treating an unmappable country as a routing failure
rather than a cosmetic one, ordering the relocation `DELETE` after the `INSERT`,
and arguing against the per-country split while still implementing it.

Three tests failed on first run and in all three cases my assertion was wrong
and the implementation was right — the 90-day boundary is inclusive, and the
clean sample has two orphan redemption members rather than one. Those were
corrected in the tests rather than by bending the behaviour to match, which is
the discipline that makes AI-accelerated work safe: the generated code is a
proposal, and the tests plus the sample data are what decide.

The commit history shows the order this was built in.

# Architecture

This document describes the system as built through phase 3, and where later phases attach.

## Context

The product is a daily acquisition brief for a Dallas spec builder. Each morning by 7 a.m. it lists new teardowns and lots that fit a buy box. Each candidate gets a pro-forma computed in code and a short LLM risk narrative.

The thesis is **LLM for judgment, code for math.** The model reads listing text and writes narrative. Every figure in a pro-forma is computed by deterministic code that can be tested.

Phase 1 lays the foundation the later phases build on:

- the Postgres schema and a Postgres job queue
- source adapters: county appraisal CSV, RentCast, and an MLS stub
- mock and live modes, with a committed synthetic snapshot
- a market pack per county, with Dallas first
- a read-only API and a Docker Compose stack

## Stack

| Part | Choice |
| --- | --- |
| Language | Python 3.12 |
| API | FastAPI, Pydantic v2, pydantic-settings |
| Database | Postgres 16 through SQLAlchemy 2 Core (no ORM), Alembic migrations, psycopg 3 (for `COPY`) |
| HTTP client | httpx |
| CLI | Typer |
| Tooling | uv lockfile, ruff (lint and format), mypy strict on `src/`, pytest with respx |

## Module layout

All code is under `src/feasibility/`.

```
config.py            Settings: DATABASE_URL, DATA_MODE, RENTCAST_API_KEY (SecretStr), budget, TTLs, MARKET
db.py                engine helpers, upgrade_to_head, current_schema_version
logging.py           log setup and SecretRedactingFilter
tables.py            table definitions
listings.py          upsert of listing batches; sync_listings (fetch and store every enabled source)
domain/              canonical models every adapter maps into: Address, Listing (with nullable
                     remarks), PropertyRecord, ValueEstimate, SaleComp
sources/
  base.py            ParcelSource, ListingSource, PropertyRecordSource, ValuationSource protocols;
                     ListingBatch; NotConfiguredError
  cad_csv/           reader.py (streaming), importer.py (staging, aggregation, upsert)
  rentcast/          models.py, scrub.py, transport.py, client.py, cache.py, budget.py,
                     adapter.py, verify.py
  mls/stub.py        ListingSource that raises NotConfiguredError; maps RESO fields in its docstring
markets/
  schema.py          MarketPack model (extra="forbid" everywhere)
  loader.py          load, validate and register packs
  packs/dallas.toml
jobs/
  queue.py           enqueue, claim, complete, fail, reclaim_expired
  worker.py          poll loop; run_job() is the one seam around job execution
  handlers.py        job kind -> (payload model, handler)
snapshot/
  cad_layout.py     DCAD column layout and CSV formatting
  days.py            the snapshot's days: which dates mock mode can source
  load.py            load the committed snapshot into the database (the seed)
sourcing/
  keys.py            address keys: street number, half, canonical name, stem, unit, property key
  matching.py        listing-to-parcel matching (pure) and the parcel index
  filters.py         the buy box: listing-level and parcel-level filters (pure)
  scoring.py         the teardown score and its breakdown (pure)
  diff.py            the daily diff classification (pure)
  counts.py          RunCounts
  store.py           every SQL statement of a run
  estimate_store.py  every SQL statement of value estimates: targets, the stored answer, the billed-call count
  estimates.py       the value-estimate spend: the plan (pure) and the call loop
  run.py             orchestration: date, sync, diff, match, filter, score, rank, write, price the
                     top, compute the pro-formas
  errors.py          SourcingError and its subclasses
proforma/            the pro-forma: the engine (pure) and its database edge
  money.py           the one place rounding modes live
  model.py           ProformaInputs, ProformaResult and the models inside them
  sizing.py          the buildable size from the lot and the zoning rule (pure)
  arv.py             the after-repair value from the estimate's sale comps (pure)
  chain.py           the cost, finance, hold and sell lines for one price (pure)
  offer.py           the most that earns the target margin, in closed form (pure)
  sensitivity.py     the ARV x hard cost x hold grid (pure)
  engine.py          build_proforma: statuses, reasons and flags (pure)
  gather.py          reads the database and builds the inputs of a run's ranked candidates
  store.py           every SQL statement of stored pro-formas
  run.py             stage 5 of a run: a pro-forma for every ranked candidate
  render.py          the terminal views behind `proforma list` and `show`
api/                 app factory, identity, schemas, and routers: health, markets, parcels,
                     listings, jobs, budget, sourcing, proforma
cli.py               the feasibility command
migrations/          Alembic environment and versions/ (0001 initial schema, 0002 sourcing,
                     0003 per-run match, 0004 candidate estimates, 0005 pro-formas)
```

Outside the package: `scripts/generate_snapshot.py`, `scripts/check_rentcast_docs.py`, `data/snapshot/`, `tests/`.

### Phase slots reserved for later

No empty modules exist. These are the planned locations.

| Phase | Module | Purpose |
| --- | --- | --- |
| 4 | `llm/` | Signal extraction and risk narratives |
| 5 | `delivery/` | Brief rendering and delivery |
| 6 | `evals/` | Eval runner over the snapshot |

## Schema

Alembic revisions `0001_initial_schema`, `0002_sourcing`, `0003_run_listing_match` (each run's match on `run_listing`), `0004_candidate_estimate` and `0005_proforma` (the sourcing tables are described under [Sourcing](#sourcing), the pro-forma table under [Pro-forma](#pro-forma)).

| Table | Purpose | Key points |
| --- | --- | --- |
| `source_file` | One row per imported CAD file set | `market`, `source`, `kind` (`certified` or `current`), `roll_year`, `file_date`, `sha256`, `carries_values`, `status` (`loading`, `loaded`, `failed`), row counts (read, loaded, skipped) and `skip_reasons`. Unique on (market, source, kind, roll_year, file_date). |
| `parcel_version` | The parcel as of each file | Primary key (market, account_id, source_file_id). Situs fields, `zip5`, land, improvement and total value (`numeric(14,2)`, NULL when the file has no values), `year_built`, `living_area_sqft`, `lot_size_sqft`, `use_code`, `zoning`. |
| `parcel` | Current parcel state | Primary key (market, account_id). Same columns plus `attrs_file_date` and `values_file_date`. Attributes are upserted when the incoming file date is at least `attrs_file_date`. Values come only from files that carry values, and only when at least as new as `values_file_date`. A values-free load never blanks certified values. |
| `listing` | Listings from any source | Unique on (source, external_id). Market, normalized address, `zip5`, price, status, property type, lot size, living area, year built, listed date, nullable `remarks`, `first_seen_at`, `last_seen_at`, nullable `unit`, nullable `account_id` (set when the listing matches a parcel), `raw` jsonb (scrubbed). |
| `api_cache` | Provider response cache | Primary key (provider, request_key), where the key is the sha256 of the canonical endpoint and params. `params` holds query params only. `body` is the scrubbed response. `fetched_at`, `expires_at`. |
| `api_budget` | Monthly counter | Primary key (provider, period_start). `request_limit`, `used` (check: not negative). |
| `api_request_log` | One row per attempt | Provider, endpoint, request key, period start, outcome (`ok`, `not_found`, `http_error`, `network_error`, `schema_error`, `refused_budget`, `cache_hit`, `stale_served`), status code, `billed`, `at`. |
| `job` | The queue | `kind`, `payload` jsonb, `status` (`queued`, `running`, `done`, `failed`, `dead`), `priority`, `run_after`, `attempts`, `max_attempts`, `locked_by`, `locked_until`, `last_error`, `dedupe_key` (partial unique index while queued or running), timestamps. |

No owner, mailing-address or agent-contact column exists anywhere. The JSON columns (`listing.raw`, `api_cache.body`) store only fields the RentCast models declare; undeclared fields are dropped and only their names are logged. Two more JSON columns hold comparable sales from the value estimate (address, price, size): `candidate_estimate.comps` and `proforma.result`.

Later phases add their own tables in their own migrations: signals and risk narratives (4), briefs and deliveries (5).

## Job queue

Claim shape:

```sql
UPDATE job
SET status = 'running', locked_by = :worker_id, locked_until = now() + :lease,
    attempts = attempts + 1
WHERE id = (
    SELECT id FROM job
    WHERE status = 'queued' AND run_after <= now()
    ORDER BY priority, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, kind, payload, attempts, max_attempts
```

- The worker polls every 2 seconds. There is no LISTEN/NOTIFY.
- The lease is 30 minutes. Each poll first requeues running jobs whose `locked_until` has passed. A reclaimed job that has used all its attempts goes to `dead`.
- On failure, the job is requeued with `run_after = now() + 2^attempts minutes`. It goes to `dead` when `attempts >= max_attempts` (default 5).
- `complete` and `fail` only act if this worker still holds the job, so a worker that outlived its lease cannot overwrite a requeued job.
- Payloads are Pydantic models, validated at enqueue time and again when the job runs.
- Failure text is redacted of secrets and truncated to 2,000 characters before it is stored in `last_error`.
- `cad.import` payloads name a file inside `local/` and are checked to stay inside that directory.
- Phase 1 has **no HTTP endpoint that writes**, because anything that writes can spend RentCast budget. Jobs are enqueued from the CLI. Phase 5 adds an authenticated trigger endpoint for the 7 a.m. schedule run by n8n.

Job kinds: `cad.import`, `listings.sync` and `sourcing.run`.

## Sources and the DCAD reality

- **DCAD is the parcel and value lookup, not the feed of new candidates.** `cad.import` runs on demand over a zip the operator downloaded into `local/`.
- DCAD ships one zip per year, `DCAD{YYYY}_CURRENT.ZIP`. The name has no date and there is no Last-Modified header; the only date is the zip member's timestamp. The "Certified Data Files with Supplemental Changes" set is re-issued under the same name as supplements land, so a re-download is a new version (new member date, new sha256).
- The "Most Current Ownership" set lists every property, including value-in-dispute ones, but carries no values (the value columns are all zero). Importing it is optional. It adds parcels the certified roll lacks and refreshes attributes, never values.
- **New candidates come from listings.** `listings.sync` calls `fetch_listings` on each enabled listing source in the pack. For Dallas that is one RentCast call a day: city Dallas, state TX, status Active, `daysOld` 2, limit 500. Two days overlap by one so a late run misses nothing.
- Buy-box and zip filtering is local (phase 2). Filtering by zip on RentCast's side would cost one call per zip.
- `upsert_listings` sets `first_seen_at`, which the phase 2 new-listing diff keys on.
- A `ListingBatch` carries the listings and a `stale` flag. The flag is true when the source could not refresh and answered from an expired cache.
- `PropertyRecordSource` and `ValuationSource` (RentCast) are for per-candidate lookups in phases 2 and 3, never bulk.

## CAD importer

The importer is generic and driven entirely by the market pack. DCAD is a pack, not code.

**Streaming.** Each member is read with `zipfile.ZipFile.open`, wrapped in a text reader and parsed with `csv.reader`. Only the columns the pack maps are projected, by header index, and padding is stripped. Rows go into a temporary staging table through psycopg `COPY ... FROM STDIN`. The staging tables are temporary, so they are private to the connection and not WAL-logged, and they drop on commit. Memory stays at about one row plus the COPY buffer. There is no pandas.

**Aggregation.** One `INSERT ... SELECT` builds `parcel_version` from the staged files. When a file has several rows per account, the pack picks a rule per field:

| Rule | Meaning | Dallas use |
| --- | --- | --- |
| `sum` | Add the values | Living area across buildings; lot size across land sections |
| `from_max_of:<col>` | Take the value from the row with the largest `<col>` | Year built of the largest building (`TOT_LIVING_AREA_SF`) |
| `from_min_of:<col>` | Take the value from the row with the smallest `<col>` | Zoning of the first land section (`SECTION_NUM`) |
| `first` | First row in file order | Everything from the base file |

**Unit factors.** A measured field can name a unit column. The pack's `unit_factors` convert it: `SQUARE FEET` counts 1 and `ACRES` counts 43,560. An unknown unit makes the lot size NULL and is counted in the import report.

**Guarded upsert.** `parcel` is upserted from `parcel_version` only where `attrs_file_date <= incoming file date`. Value columns are written only from files whose kind carries values.

**Idempotency.**

- The sha256 comes from a streaming pass first.
- The same key (market, source, kind, roll year, file date) with the same sha256 and status `loaded` is a no-op that returns the earlier report.
- The same key with a different sha256 is an error unless `--force`.
- Loading runs in one transaction. A failure rolls back, marks `source_file` failed, and the import can be rerun.

**Personal-data gate.**

- The pack may only reference files declared in it. The Dallas pack declares `ACCOUNT_INFO`, `ACCOUNT_APPRL_YEAR`, `LAND` and `RES_DETAIL`.
- Pack validation rejects any mapped file or column whose name matches `OWNER|PHONE|BIZ_NAME|LEGAL|TAXPAYER|APPLICANT|MAIL`.
- The reader cannot select an unmapped column.
- A pack may name a skip flag. For Dallas it is `ACCOUNT_INFO.EXCLUDE_OWNER`: any value other than blank, `N`, `0`, `F` or `FALSE` skips the whole account, counted in the import report. If an earlier load stored that account, its `parcel` and `parcel_version` rows are deleted in the same transaction. The flag's value is never stored.

## RentCast client

Request path:

1. Canonicalize the request (path and sorted, stripped params) and hash it into the request key.
2. Check the cache; return on a fresh hit.
3. Mock mode: answer from the snapshot and stop. No budget is touched.
4. Live mode: take a Postgres advisory lock on the request key, so concurrent callers make one paid call.
5. Check the cache again.
6. Reserve one budget unit and commit it before the call. If none is left, log `refused_budget` and serve a stale entry or raise `BudgetExhaustedError`.
7. Send the request.
8. Scrub personal fields.
9. Validate against `models.py`.
10. Store in the cache and log the attempt.

The client never retries. Retries happen at the job level, with backoff.

| Outcome | Budget | Cache | Caller gets |
| --- | --- | --- | --- |
| 200, schema-valid | Kept (billed) | Stored with the endpoint TTL | Data |
| 200, schema-invalid | Kept (billed) | Not stored; field paths logged as drift | `SchemaDriftError` |
| 404 | Refunded | Not stored | `None` or empty, which is a valid answer |
| Other 4xx or 5xx, including 429 | Refunded | Not stored | Stale entry (marked stale) if one exists, else `RentCastError` |
| Connect failure | Refunded | Not stored | Stale entry or `RentCastError` |
| Read timeout | Kept (outcome unknown) | Not stored | Stale entry or `RentCastError` |
| Budget exhausted | Refused before the network | | Stale entry or `BudgetExhaustedError` |

**Default TTLs** (settings, overridable): sale listings and listing by id 20 hours, property records 30 days, value estimates and comps 7 days.

**Budget period.** It starts on `RENTCAST_BILLING_ANCHOR_DAY` (1 to 28). The limit defaults to 50. The hard stop keeps the counted requests under the plan's allowance. That holds only if the refund rule below matches how RentCast actually bills; see Known gaps.

**Budget math.** About 30 listing syncs a month leave about 20 value estimates, and the spend rule below keeps a month inside that. A real deployment needs a paid tier or the client's MLS feed, which is what the stub is for. A live run makes the listing sync call (none when the response is still cached) plus at most `top_n` estimate calls, so never more than 1 + 5 an attempt with the Dallas pack, and typically 1 or 2. A same-date retry or re-run buys again for a target an earlier attempt failed or deferred (a read timeout is billed and counted as failed), within the cap and the reserve.

**The spend rule** (`[sourcing.estimates]`, `sourcing/estimates.py`). After the run is ranked, the top `top_n` ranked candidates are the targets. A target that has a stored estimate fetched within `ttl_days` of the run date, whatever its outcome, is reused. The others are bought in rank order, one address at a time, up to

`spendable = max(0, min(targets to buy, monthly_cap - billed estimate calls this period, remaining budget - reserve))`

where `reserve = days left in the period after today x sync_reserve_per_day`, so every remaining day keeps its listing sync. Syncs outrank estimates. The cap counts every billed `/avm/value` row of the period, `feasibility verify-rentcast` calls included; cache hits and refunded calls are not billed. Worked day (31-day month, run on the 1st): 49 left after the sync, reserve 30, 49 - 30 = 19, cap left 20, five targets, so 5 are bought. On the last day the reserve is 0. In a 31-day month that is at most 31 syncs and 19 estimates, in a 30-day month 30 and 20, in a 28-day month 28 and 20.

Each answer is committed the moment it arrives (`candidate_estimate`, one row per candidate and date), so a crash never loses a paid answer and a retry reuses it. A 404 is stored as `no_estimate` and reused like any other answer. A budget that runs out mid-loop defers the rest. Any other `RentCastError` counts that candidate as failed, is logged with secrets redacted and is not retried inside the run, because a job retry would spend again. A stale answer (the call failed and an old cache entry answered) is deferred and not stored. The estimate does not enter the score; it is stored for the pro-forma. In mock mode the snapshot answers, the budget is never touched and the monthly budget, cap and reserve are ignored (top N and reuse still apply). The counts record `estimates_targeted`, `estimates_reused`, `estimates_called`, `estimates_no_estimate`, `estimates_deferred` and `estimates_failed`; every target is reused, called, deferred or failed.

**Scrubbing.** `listingAgent`, `listingOffice` and `owner` are removed at any depth before validation, caching, storage in `listing.raw`, or logging. The models do not declare them. A test asserts that a raw fixture carrying them leaves no trace in the cache.

**AVM comps.** `/avm/value` returns comparables inside the estimate, and the published schema has no sale/rent flag on them. The adapter keeps a comp only when its `listingType` is a documented sale type: Standard, New Construction, Foreclosure or Short Sale. The rest are dropped and counted in `dropped_comparables`. The pro-forma phase depends on this.

## Keeping the API key out of logs, errors and the cache

- The key is a `SecretStr`. `get_secret_value()` is called at one place that sends it: the `X-Api-Key` header of the HTTP transport. (Settings also exposes the value to the log redactor.)
- Auth travels in a header, so the key never enters the URL, the params, the request key or `api_cache.params`.
- The `httpx` and `httpcore` loggers are pinned to WARNING. `SecretRedactingFilter` on the log handler replaces the literal key in the message, arguments and tracebacks.
- RentCast's error body is reduced to its short `error` code, checked against a pattern. Its free-text `message` is dropped. Transport errors are re-raised as `RentCastError(endpoint, status, error_code)` without chaining the request.
- Job failure text is redacted before it is stored.
- FastAPI returns generic bodies for validation and unexpected errors, and validation errors do not echo submitted values.
- `/health` reports `mode` and `rentcast_key_configured` (a boolean), never the key.
- Live mode with no key refuses to start. Mock mode drops any key that is set.
- `.env` is gitignored and only `.env.example` is committed. gitleaks runs in pre-commit and CI.

## Mock and live modes

- `DATA_MODE=mock` is the default and needs no key and no network. The RentCast client uses `SnapshotTransport`, which serves `data/snapshot/rentcast/` keyed by the same canonical request key. The seed loads the synthetic CAD files through the same importer.
- `DATA_MODE=live` uses `HttpTransport` with the key, the real cache and the budget.
- One client, one scrub and one validation path serve both modes. Only the transport differs.

## Keeping the synthetic snapshot in step with the schema

- `sources/rentcast/models.py` is the one source of truth for RentCast response shapes. It is written from RentCast's published OpenAPI definition. Models allow extra fields so unknown live fields survive, and require only the fields the application keys on.
- `scripts/generate_snapshot.py` uses a fixed seed and builds the JSON by instantiating those models, so hand-edited JSON cannot drift silently. It writes 70 synthetic parcels in DCAD layout (quoting, padding, CRLF), the values-free current set, and the recorded responses for the two snapshot days.
- CI checks that every snapshot file validates against the models and that regenerating reproduces the committed files byte for byte. A model change without a regenerated snapshot fails the build.
- `scripts/check_rentcast_docs.py` fetches the published OpenAPI definition and diffs field names against the models. It needs the network but no key and spends no budget. It is run by hand.
- `feasibility verify-rentcast` makes at most 4 live calls, with a ceiling of 10 enforced in code. It validates the responses and writes a local, gitignored report of field names and types only to `local/rentcast-verify-<timestamp>.json`. It spends from the same budget.

## Market pack

`markets/packs/<id>.toml`, validated with `extra="forbid"` everywhere so a typo cannot fall back to a default.

| Section | Contents |
| --- | --- |
| `[market]` | id, name, state, county, IANA timezone |
| `[sources.parcels]` | adapter `cad_csv`, source name, encoding, delimiter, archive-name regex with a `year` group, join key, base file, file kinds with `carries_values`, member files, `fields` map (canonical field to file, column, transform, aggregate, unit column), `unit_factors`, optional `skip_accounts` |
| `[[sources.listings]]` | `rentcast` (city, state, status, `days_old`, `limit`) and `mls` (`enabled = false`) |
| `[buy_box]` | zips, max price, minimum lot size, maximum year built, minimum land-to-total ratio, property types. Applied by `sourcing/filters.py`. |
| `[sourcing]` | `source_priority` (which listing source speaks for a property, best first) and `[sourcing.scoring]`: the four weights (they sum to 100), where each component earns full credit, the vacant-lot age credit, and the value-drift settings. Validated against the buy box. `[sourcing.estimates]` is the value-estimate spend policy: `top_n` candidates priced, `monthly_cap` billed calls a period, `ttl_days` of reuse, and `sync_reserve_per_day` calls held back for the daily sync (`monthly_cap >= top_n`). |
| `[cost_assumptions]` | `status` (`illustrative` or `reviewed`), `sources_read_on`, and the pro-forma inputs by group: `acquisition`, `demolition`, `construction`, `financing`, `holding`, `selling`, `target`, `arv`, `sizing` (home-size clamp, per-zoning coverage rules and a required `default` rule) and `sensitivity` (the grid, which must contain the base case). Every Dallas cost value is illustrative: a labelled default, not a quote or a builder's actuals. Sources are in `docs/proforma-assumptions.md`. |

`feasibility market validate` checks every pack, and a parametrized test runs over `packs/*.toml`, so a new county is a new file.

## Sourcing

`sourcing/run.py` turns the stored listings and parcels into a ranked, diffed candidate list for one day. One run is keyed by (market, `as_of`), is idempotent (running the same date again rewrites that run's rows only), and goes forward in time (an earlier date than any run already started is refused). Starting the run, the sync, and recording the sync status are separate transactions; building the run (diff, match, score, rank, write) is one transaction, taken behind an advisory lock per market that re-checks the date order. The sync itself runs outside that lock.

**Order of a run.** Resolve the date (live mode: today in the market's time zone only; mock mode: an explicit date listed in `data/snapshot/days.json`). Sync the feed (`sync_listings`; in mock mode through the snapshot overlay of that day, with the response cache bypassed so a later day is not answered with an earlier day's body). Classify listings against the previous completed run with a fresh sync. Apply the listing-level filters. Match what passes. Fold listings into candidates. Apply the parcel-level filters. Score and rank. Write. Then, outside any transaction, buy value estimates for the top candidates (the spend rule under the RentCast client's budget math). Last, compute a [pro-forma](#pro-forma) for every ranked candidate from what is stored.

**A failure after the build.** The build transaction commits the ranking and completes the run before anything is paid for. If a later stage (the estimate spend, then the pro-formas) fails, the run stays `completed` with its ranking, `sourcing_run.error` carries the redacted message (a completed run with a non-null `error` means "ranked, but a later stage failed"), the counts record what was bought and `estimates_failed`, and the exception propagates. The pro-forma stage is one transaction, so a failure leaves none of its rows or counts behind. The error is cut to 2,000 characters and database errors do not echo the values bound to a statement. An unclassified error retries the job; a shape change (`SchemaDriftError`) is permanent for the worker, and the next day's run tries again. A retry rebuilds the run's rows, reuses every stored estimate and finishes; completing the run clears `error`. A retry buys no new estimate for a target that an earlier attempt answered, but it does retry a target that attempt failed or deferred, within the same cap and reserve. The pro-forma stage, when it finds its run no longer `completed` (a newer attempt reset or failed it), writes nothing and leaves that run's own error alone; the estimate spend has no such guard and only merges its counts. A failure in the sync or the build still marks the run `failed` and clears its rows.

**Schema (`0002_sourcing` to `0004_candidate_estimate`).**

| Table | Purpose |
| --- | --- |
| `sourcing_run` | One row per (market, `as_of`): `status` (`running`, `completed`, `failed`), `sync_status` (`fresh`, `stale`, `skipped`, `pending`), `counts` jsonb, `error`, timestamps |
| `listing_match` | The latest match attempt per listing: `status` (`matched`, `ambiguous`, `unmatched`), `method`, representative `account_id`, `gis_parcel_id`, `account_count`, `street_key`. Rewritten each run |
| `candidate` | One row per property across runs and sources, unique on (market, `property_key`). History is kept: nothing cascades into it |
| `run_listing` | The daily diff: one row per listing the run saw or lost, with `change_kind`, `price`, `prev_price`, the candidate, `is_primary`, the listing-level `filter_reason` and the match this run made (`match_status`, `match_method`, `match_account_id`; all NULL when the run did not match the listing, and on runs written before migration 0003, which the API serves as `match: null`). A listing the feed lost carries the previous run's match |
| `run_candidate` | One row per candidate per run: `status` (`ranked`, `filtered`, `unscored`), `filter_reasons`, `unscored_reason`, `score`, `rank`, `breakdown` jsonb. Check constraints keep a ranked row complete and an unscored row explained |
| `candidate_estimate` | A value estimate bought for a candidate: one row per (candidate, `fetched_on`), `outcome` (`ok`, `no_estimate`), the one-line `address` sent, `price`, `price_low`, `price_high`, the kept sale comps as jsonb (`comps`, with `comp_count` and `dropped_comp_count`) and the `run_id` that fetched it. Not run-scoped, so a re-run keeps it and costs no new call; deleting a run leaves it. A check keeps `ok` and a price together |

**The diff.** `S_R` is the market's active listings last seen on the run date; `S_P` is the listings the latest earlier completed run with a fresh sync still had in its feed (a stale or skipped run records nothing about absence, so diffing against it would call every listing it missed relisted).

- In both, price differs: `price_changed` (up or down); otherwise `unchanged`.
- Only in `S_R`: `relisted` when the listing was first seen before today (the same id returning after a gap) or its property already was a candidate on an earlier run date (a new id for a known property); otherwise `new`. With no previous run everything is `new`.
- Only in `S_P`: `gone` or `aged_out`. The RentCast feed is a window (`days_old`), not the inventory: a listing older than the window stops appearing though it is probably still for sale. So absence is `gone` only when the listing's status is not Active or it was young enough to still be in the window; otherwise (including a listing with no listed date) it is `aged_out` and nothing is known about a sale.
- When the sync was `stale` (served from an expired cache) or `skipped` (budget spent), absence proves nothing: nothing is recorded as gone and the count goes to `unknown_absent`.
- Listings in the feed with another status than Active that were not in the previous run are ignored and counted in `inactive_ignored`.

**Matching.** A listing's street is normalized into a key (number, half number, name with a canonical suffix) and a stem (the name without its suffix). Rules, first that applies wins: no zip or no street number is `unmatched`; parcels with the same key in the zip are judged by unit and then by whether several accounts share one GIS parcel (`gis_group`, values summed, a two-account lot); otherwise parcels with the same stem (`stem`), unique or ambiguous. Nothing is guessed: no hit is `unmatched`, several unrelated hits are `ambiguous`. Both are kept, counted, never scored, and re-matched on every run, so a later parcel import can fix them. The match rate is matched over everything attempted, stored as a 4-decimal string.

**Candidates.** A property is identified by `acct:<account>`, `gis:<gis id>` for a shared lot, or `addr:<zip>:<street key>[:<unit>]` while unmatched. Listings of one property from several sources are one candidate; the primary listing is the source ranked first in the pack's `source_priority`, then the lowest listing id. An unmatched candidate that later matches is upgraded in place (its key changes, its history stays).

**Filters.** Listing-level (before matching, first failing reason, recorded in `run_listing.filter_reason`): `zip`, `property_type`, `price`. Parcel-level (after a match, every failing reason): `lot_size`, `year_built`, `land_to_total`. The lot size and year fall back to the listing's when the parcel has none; a missing year passes only for a Land listing. A matched parcel without certified values is `unscored` with reason `values_missing`, not filtered.

**Score.** Four components that add up to 100, each rounded to 0.01 (the score is the sum of the rounded points, so the breakdown always adds up). The numbers are the Dallas pack's.

| Component | Weight | Input | Full credit at | No credit at |
| --- | --- | --- | --- | --- |
| `land_ratio` | 35 | land value / total value | 0.85 | the buy box floor (0.55) |
| `age` | 20 | year built (a vacant lot earns `vacant_age_credit`) | 1940 or earlier | the buy box cut-off year (1965) |
| `lot` | 20 | lot size, sqft | 12,000 | the buy box floor (6,000) |
| `price_vs_land` | 25 | price / adjusted land value | 1.00 or less | 1.75 or more |

Ranks run 1..n over the ranked candidates: higher score, then lower price, then lower candidate id. `run_candidate.breakdown` stores the full explanation (match, values used, the four components, which inputs fell back to the listing, what was missing); its keys are read by later phases.

**Stale values.** County values are as of January 1 of the roll year. The land value in the price component is drifted forward by simple growth: `adjusted = land * (1 + drift_pct / 100 * years)`, with `years` the time since that January 1, capped at `max_drift_years`. The land ratio and age components use the raw values (a uniform market move cancels in a ratio). The breakdown records the age, the factor and the adjusted value, and flags `values_stale` past `stale_values_years`; staleness never excludes a candidate.

**API.** `api/routes/sourcing.py` serves runs and their candidates read-only (see the README). The router imports nothing that can write or spend, and a test asserts it.

## Pro-forma

`proforma/engine.py::build_proforma` turns a candidate's facts, its stored value estimate and the pack's `[cost_assumptions]` into a `ProformaResult` (version 1; Phases 4 and 5 read its keys, so they do not change): sizing, ARV, costs, financing, holding, selling, totals, the most that earns the target margin, and a sensitivity grid. The engine is pure `Decimal` arithmetic; rounding modes live only in `proforma/money.py`, and `docs/proforma-reference.xlsx` (live formulas) is the check on its formulas, read only by the tests. Every Dallas cost value is **illustrative**: a labelled default with sources in `docs/proforma-assumptions.md`, not a quote or a builder's actuals.

**Statuses.** `computed`, `no_arv` (costs, financing and holding still computed; profit, margin, ROI, maximum offer and the grid are null) and `unsizable` (nothing but the site and a reason).

| Case | What the engine does |
| --- | --- |
| Vacant land (a Land listing with no year built anywhere) | Demolition 0, flag `vacant_lot`; sized from the lot and zoning like a house |
| House | Demolition is `flat + per_sqft x existing living area`; an unknown area uses `fallback_sqft`, flag `existing_area_assumed` |
| Two or more accounts on one lot (`gis_group`) | One acquisition, one lot (the largest, counted once), living areas added up, the accounts' zoning when all non-blank values agree (else the default rule, flags `zoning_mixed` and `zoning_rule_assumed`); always flagged `gis_group` |
| No lot size | `unsizable`, reason `lot_size_missing` |
| Zoning missing or with no rule | The pack's default (most conservative) rule, flag `zoning_rule_assumed`; the candidate is not dropped |
| No usable estimate | `no_arv`: reason `no_estimate_yet` (none stored), `estimate_unavailable` (RentCast had none), `estimate_expired` (older than 30 days), `too_few_comps`, or `arv_not_positive`. Nothing substitutes for a missing ARV |
| An estimate older than `ttl_days` | Used, flag `estimate_stale` (a refresh was deferred) |
| Loss larger than the cash invested | Computed, flag `loss_exceeds_equity` |
| Target margin unreachable at any price | `max_offer` null, flag `no_viable_offer` |

The AVM point estimate values the existing property and is stored for context only; the ARV is the median $/sqft of the estimate's **sale comps** times the buildable size.

**Inputs (`gather.py`).** Three set-based queries serve a whole run: the ranked candidates with their primary listing's price and match in *that run*, their parcel rows (the matched account, or every account of the GIS parcel in the zip), and each candidate's newest estimate on or before the run date. The lot is the parcel's, else the listing's. A candidate whose primary listing has no positive price in the run gets no pro-forma. A stored comp is mapped to the engine's `Comp` using only its own fields (`days_old` is dropped) and a comp priced at zero or less is dropped.

**Stage 5 (`run.py`).** After the estimates, `run_proformas` takes the market's advisory lock, checks the run is still `completed`, computes every pro-forma, replaces the run's `proforma` rows and merges the counts (`proformas`, `proformas_computed`, `proformas_no_arv`, `proformas_unsizable`) into `sourcing_run.counts`, in one transaction. It is code only: it is never given a RentCast client and costs no call. It recomputes from stored estimates, so a retry or a same-date re-run rewrites the same figures. Changing the pack's assumptions changes pro-formas on the next run (a same-date re-run included); there is no recompute command.

**`proforma` table (`0005_proforma`).** Primary key (`run_id`, `candidate_id`), a composite foreign key to `run_candidate` with `ON DELETE CASCADE` (rebuilding a run's rows rebuilds its pro-formas). Columns for what a list shows and filters by: `status` (`computed`, `no_arv`, `unsizable`), `reason`, `estimate_fetched_on`, `offer_price` (the price modelled), `arv`, `total_cost`, `profit`, `margin`, `roi`, `annualized_return` (ratios, not percents), `max_offer`, `flags`, and `result` (the whole `ProformaResult`: every input, assumption and intermediate). The money and ratio columns are `numeric(20, ...)` because the engine accepts 15-digit comps and a tiny ARV makes a huge margin. Checks: a row is `computed` exactly when it has an ARV, a total cost, a profit and a margin, and has a `reason` exactly when it is not computed (`roi` and `annualized_return` are null when a loan covers every cost).

**API and CLI.** `GET /sourcing/runs/{run_id}/proformas` and `GET /sourcing/runs/{run_id}/candidates/{candidate_id}/proforma` (see the README), and `feasibility proforma list|show`. All read-only; the router imports nothing that can write or spend.

**What the approximations mean.** All are closed-form averages of cash flows over a 6-12 month build; the sensitivity grid (hold 6/9/12, ARV +-10%, hard cost -10% to +20%) and the maximum offer guard against over-trusting any one.

| Approximation | Why it is reasonable | Direction of the error |
| --- | --- | --- |
| Interest-only simple interest on the outstanding balance, no compounding | Spec construction loans are interest-only on drawn funds, billed monthly; monthly compounding over 12 months at 10% adds about 0.4% to the interest line | Slightly under |
| Land, demolition and soft costs drawn at closing | The lender funds the purchase at closing; permits, design and impact fees are paid up front | Slightly over; conservative |
| Hard cost and contingency drawn linearly over the build | Milestone draws trace an S-curve whose mean outstanding balance is about half the commitment | Roughly neutral |
| Draw inspections as a flat fee x draw count | Lenders charge a fixed fee per draw; it does not scale with price | Neutral |
| Points on the whole commitment | Lenders charge origination on the commitment at closing | Exact in kind |
| Loan = loan-to-cost x financeable cost, no cap at a share of ARV | The cap only binds when cost is above about 85-90% of ARV, where the margin is already thin | Over-states leverage on thin deals |
| Closing and holding costs as cash in | ROI is the levered return on the builder's own cash; interest, taxes and insurance are really paid over time | Understates ROI slightly |
| Property tax = combined rate x purchase price x hold / 12 | Texas taxes accrue by the calendar year; the land basis is what was paid; improvements under construction are ignored | Mixed |
| Insurance = builder's-risk rate x hard cost x hold / 12 | Priced on the construction value and runs the build | Neutral |
| Annualized return = ROI x 12 / hold, simple | Compounding would assume the same deal can be repeated at the same return | Under a compounded figure |
| Selling costs as a percent of ARV, no price drift while marketing | Commission and seller closing are percentages of the price in Texas | Neutral |

**A day, in mock mode and live.** Mock mode serves estimates from the snapshot: day 1 prices five candidates and day 2 one (the other four reuse theirs), `api_budget` stays untouched, and the pro-formas cost nothing. Live mode costs at most `1 + top_n` RentCast calls an attempt (the listing sync and up to five estimates, typically one or two), never more than the monthly cap and the sync reserve allow; the pro-formas add none.

## Listing remarks: redaction and screening

Remarks are free text from a listing feed, so they are personal-data-bearing and untrusted. The only path into `listing.remarks` is `sources/mls/reso.ingest_remarks`, applied before the domain `Listing` exists, and `attach_remarks` discards remarks set any other way.

1. **The RESO record is mapped field by field.** `ResoProperty` declares the fields the adapter uses; the agent, office, private-remarks and showing-instruction fields are never read, validated, stored or quoted in an error. `listing.raw` holds the scrubbed RentCast record, never the RESO one.
2. **Normalise.** NFKC, then removal of control, zero-width, bidirectional, tag-block and filler characters, which are counted.
3. **Redact.** `sources/mls/redact.py` replaces personal data with `[contact removed]`: a cue word ("call", "listed by", "ask for", "agent", "showings:" ...) and its clause, emails in every common spelling, phone numbers in common formats, links and bare domains, honorific names, brokerage names, licence numbers, and a full name left beside a removed contact. Every scanning pattern is anchored or length-bounded so hostile input costs linear time; input is bounded before and after normalisation.
4. **Cap and mark.** At most 4,000 characters. When three or more invisible characters were removed, a final line `[invisible characters removed]` is added after redaction, so that the injection scan can see it later without any state.
5. **Screen at prompt build** (`llm/untrusted.py`, used from phase 4c): angle-bracket lookalikes (other scripts' brackets, HTML entities) are folded to `<` and `>` first, keeping the text's length; `scan_injection` marks attack phrasings and the sentences they sit in; a quote that overlaps one is dropped. The scan is a heuristic. The structural defences are the closed output schema, code that verifies every quote, and no digits from quotes reaching a narrative.

**What redaction does not catch.** Redaction is pattern-based, not a named-entity model. A bare name with no cue word and no contact beside it ("Maria will meet you there"), spelled-out digits, letter-spaced or `-at-` style emails, homoglyph look-alikes in a cue word, lowercase brokerage names and brands with no suffix all pass. `evals/signals/answer_key.json` tags records that plant such forms `personal:residual`; the extraction eval reports them separately and does not gate on them. A real MLS feed needs a review of its remarks before it is ingested.

## Signals and narratives in the run (stages 6 and 7)

`sourcing/run.py` runs two more stages after the pro-formas, both in `llm/run.py`. They read what the run stored and write only `candidate_signals`, `candidate_narrative`, the cache `llm_result`, the ledger `llm_call` and the run's counts, so they cannot change the ranking, the estimates or the pro-formas (a test compares those rows byte for byte after a failed stage).

- **Stage 6, signals.** For every ranked candidate in rank order: the three field signals from the run's diff row and the listing's dates (code); then, if the primary listing has remarks and a model is configured, the extraction (cache, else one call), verified quote by quote. One `candidate_signals` row per candidate, committed as it is made.
- **Stage 7, narratives.** For every ranked candidate whose pro-forma was computed: the facts sheet built by code, then the narrative (cache, else one call and at most one repair), checked figure by figure. The others get a `not_eligible` row. A failed stage 6 means stage 7 does not run.
- **No call inside a transaction.** A stage reads its inputs in one short read, then calls the model with no connection open for it; the metered client writes its ledger rows on their own connections, and each result is stored in its own short transaction, serialised with the market's other runs. A stage also holds a session-level advisory lock (`llm_spend:<market>`) so a retry that overlaps its predecessor cannot spend against the same caps twice.
- **Cache.** `llm_result` is keyed by prompt, prompt version, tier and the hash of the inputs (not run-scoped). A signals entry holds the verified signals, the dropped claims and the model's flag, plus a fingerprint of the injection scan it was verified against (remarks that differ only in angle brackets send the same text but scan differently, so they are a miss). A narrative entry holds the whole `NarrativeResult`; a rejected one is cached too, so a retry does not spend again. Failed and deferred results are never cached.
- **Caps.** The metered client refuses a call when the run's spend (every attempt of the run, from the ledger) plus the reservation would pass `LLM_RUN_BUDGET_USD`, or, for billable calls, the month's would pass `LLM_MONTHLY_BUDGET_USD`. A refused candidate, and every later one not in the cache, is `deferred` / `budget`; a stage that hits a cap ends normally.

| What happens | Candidate | Rest of the stage | Run |
| --- | --- | --- | --- |
| Cap refuses a call | `deferred` / `budget` | not-cached candidates `deferred` / `budget` | completed, no error |
| Structured-output error, model refusal, a call over the per-call budget | `failed` | goes on | completed, no error |
| Provider error (429, 5xx) | `failed` / `provider_error` | not-cached candidates `deferred` / `provider_error` | completed, error set, `RetryableModelError`, the job retries and reuses everything cached |
| Missing, stale or malformed recording; model misconfigured | no row | stops | completed, error set, `PermanentModelError`, the job goes straight to `dead` |
| Live data and no model configured | `fields_only` / `llm_not_configured`; narrative `deferred` | goes on | completed, no error |

What a failure says is built from the error's class and never from its text, so nothing a listing, the model or the provider said reaches `sourcing_run.error`, a job's `last_error` or a log line.

**Until the recordings are committed**, a mock-mode run replays nothing, so its first call raises a replay miss and the run ends as the table's fourth row says: ranking, estimates and pro-formas stored, the run `completed` with the error set, signals and narratives empty. The recording session (Phase 4 job G) removes this.

`candidate_signals.result` is `SignalsResult` and `candidate_narrative.result` is `NarrativeResult` (both in `llm/results.py` and `llm/narrative.py`). `RemarksInfo.removed_invisible_count` is the number of `[invisible characters removed]` markers in the stored remarks, because only the stored text is kept after ingestion.

## Where agent-core attaches (phase 4)

agent-core is a phase 4 dependency, to be pinned to a release tag. Nothing in this repository imports it today.

| Capability | Attach point |
| --- | --- |
| Model client | New `llm/` job handlers (signal extraction, risk narrative) registered in `jobs/handlers.py`. Input is `domain.Listing.remarks` plus parcel facts. |
| Tracing | Wraps `jobs/worker.run_job()`, one span per job carrying job id and kind. |
| Audit log | agent-core's own, written for each model call and keyed by job id and listing id. This repository adds no table for it. |
| Eval runner | `evals/` (phase 6) uses the committed snapshot as its fixture corpus. |

## Compose and CI

`docker-compose.yml` (project name `realestate-feasibility`):

| Service | Role |
| --- | --- |
| `db` | `postgres:16-alpine` on `127.0.0.1:4502`, with a healthcheck and a named volume |
| `migrate` | One-shot: `feasibility migrate && feasibility seed` (both idempotent) |
| `api` | `127.0.0.1:4501`; starts after `migrate` completes successfully; has a healthcheck |
| `worker` | `feasibility worker`; starts after `migrate` completes successfully |

Every service sets `mem_limit`, each at least twice the peak working set a seeded day-1 run showed in `docker stats` (the measurements are in the compose file's header), and `tests/test_compose.py` fails if one does not. All ports bind to 127.0.0.1. Ports 4500 and 4503 stay free for the report viewer and n8n. A fresh clone needs no `.env`: compose falls back to local-only development defaults, mock mode and a localhost-bound database password.

**Container hardening.** Base images are pinned by index digest, with the tag kept in a comment beside it: `python:3.12-slim` and `ghcr.io/astral-sh/uv:0.12.10` in the Dockerfile, `postgres:16-alpine` in compose and in the CI service container. Never use `:latest`. To refresh a digest, request the manifest from the registry and read the `Docker-Content-Digest` response header (for example `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' <registry manifest URL for the tag>`), then update every place the image appears.

`migrate`, `api` and `worker` run with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, a 256-process limit and a 64 MB `noexec` tmpfs at `/tmp` (the seed writes temporary CSV archives there). `db` is read-only too, with tmpfs at `/tmp` and `/run/postgresql`, the data volume writable, and only the five capabilities its entrypoint needs to chown the data directory and drop to the postgres user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`).

CI (`.github/workflows/ci.yml`) runs four jobs:

- lint: ruff check, ruff format check, mypy strict
- test: pytest against a Postgres 16 service container on a separate test database
- gitleaks: scans the full history
- compose smoke: `docker compose up -d --wait --build`, then `curl` on `/health` and `/parcels?limit=5`

Pre-commit runs gitleaks and ruff.

## Known gaps and unverified points

- **Remarks redaction is heuristic.** See "Listing remarks: redaction and screening": the patterns catch the forms listing agents commonly write, a bare name with no cue passes, and the residual eval tag measures it. Only synthetic remarks are committed.
- **Live mode is unverified.** It was built from RentCast's published OpenAPI definition and has not been run against the real API. `feasibility verify-rentcast` checks it.
- **The published schema marks no field as required.** The models require only the fields the application keys on (`id` and `formattedAddress`; `price` on a value estimate), so drift in other fields is caught only as a type mismatch.
- **`EXCLUDE_OWNER` semantics are inferred.** It appears to flag a confidential owner. This is not confirmed from DCAD's layout document, so the importer skips the whole account for any value other than blank, `N`, `0`, `F` or `FALSE`.
- **No real DCAD archive has been imported yet.** Column names and file locations follow DCAD's published files as researched, and the importer is tested against synthetic files in the same format. Where `SPTD_CODE` lives (the pack reads it from `ACCOUNT_APPRL_YEAR`) is unverified. CSV headers, not DCAD's layout spreadsheet (which has typos), are authoritative.
- **Free-tier overage is unknown.** Whether RentCast blocks or charges requests past 50 a month is not documented.
- **The refund rule is an unverified billing assumption.** The client refunds every non-200 response, including 404 ("no records matched") and 429, because RentCast's documentation says requests that return an error are not billed. Whether a 404 ("no records matched") and a 429 count as errors for billing is unconfirmed. If they are billed, the counter under-counts. Confirm with `verify-rentcast` and RentCast's usage dashboard before relying on the hard stop; until then, a conservative operator can set `RENTCAST_MONTHLY_BUDGET` below the plan's allowance.
- **Run one worker.** Leases are 30 minutes with no renewal. With two or more workers, a job running longer than its lease (a full county import on a slow disk) can be claimed and run twice. Compose runs one worker.
- **The billing period is computed in UTC.** RentCast's reset time zone is unknown, so requests within hours of the boundary may count against the neighbouring period.
- **`/health` reports `commit: null` under compose unless `GIT_COMMIT` and `GIT_BRANCH` are exported before the build.** Null is deliberate: the image cannot know its revision otherwise.
- **Listing text.** RentCast listings have no description field. Phase 4 needs either a synthetic RESO-shaped set with `PublicRemarks` or a client's MLS feed.
- **AVM comps** are filtered by `listingType` because the published schema has no sale/rent flag. The list of sale types is taken from the documentation.
- **No update schedule and no downloader for DCAD.** The operator downloads files by hand. DCAD publishes no redistribution license that we found, so its files are never committed.
- **A listing with no directional never matches a parcel that has one.** `5521 WEXCOMBE AVE` against parcels `5521 N WEXCOMBE AVE` and `5521 S WEXCOMBE AVE` is `unmatched`: the stem keeps the directional (`N WEXCOMBE`), so neither the exact nor the stem lookup finds anything. It is left unmatched, not guessed, and a test asserts it.
- **Price changes and delistings are only visible inside the feed window.** The free tier's feed is `days_old` days wide; a listing that ages out of it can no longer show a price change or a delisting, and is recorded as `aged_out`.
- **RentCast's `daysOld` semantics and result ordering are unverified**, so is whether a delisting reaches the feed as a status flip. The diff's `gone` rule depends on them.
- **Stem matches can be wrong on streets that differ only by suffix** (`OSTRAVELLE AVE` and `OSTRAVELLE DR`). A stem match requires exactly one hit in the zip, which limits the damage; the match method is stored so a stem match can be audited.
- **A candidate's key upgrades only once**, from `addr:` to `acct:` or `gis:`. If a property later matches a different account, it becomes a second candidate.
- **No retention for the new tables.** `run_listing`, `run_candidate` and `candidate` grow by a day's rows per run and are never pruned. A retention rule is undecided.
- **`load_parcel_index` loads every parcel of the requested zips into memory.** Estimated at about 73 MB per 70,000 parcels, close to the worker's 256 MB limit; check before adding zips near 100,000 accounts. Narrowing by street number is not done.
- **A new source's listing can make a continuously listed property `relisted`.** A listing from a source not seen before, for a property that had a candidate on an earlier day, is classified as relisted by the property rule even though another source's listing was in the previous feed.
- **The synthetic AVM comps for the six estimate candidates are not market data.** They are drawn from per-zip $/sqft ranges chosen so the demo's pro-formas spread out (`NEW_BUILD_PSF_BY_ZIP` in the generator), and say nothing about real Dallas prices.
- **`/properties` fixtures are keyed on the title-case address** and would miss in mock mode; nothing calls that endpoint, so they are left alone. The `/avm/value` fixtures are keyed on the normalised one-line address the client sends.
- **The live estimate spend has never run against RentCast.** It is tested through stub transports only. Not verified live: whether `/avm/value` returns sale comps for most addresses, whether it returns any for a vacant lot, and (as above) whether 404 and 429 are billed.
- **The spend and the client can disagree about the billing period near its boundary.** The cap and reserve use the period that contains the run date (the market's time zone); the budget reservation uses the UTC date. When the two differ, before the first call or between two calls, the stage spends nothing more and defers the rest until the next run; the hard budget stop is unaffected.
- **Estimates are reused for `ttl_days` and a market move inside that window is not seen.**
- **The API has no authentication, rate limiting or IP banning yet.** It is read-only, binds to 127.0.0.1 and serves synthetic data in mock mode. Phase 5's write endpoint brings authentication with it; anything exposed beyond localhost needs these controls first.
- **Pro-formas are illustrative.** Every Dallas cost value is a labelled default from aggregator and lender-blog pages, not a quote or a builder's actuals. Contingency, the build/marketing split, the living share and the default zoning rule are unsourced; hard cost is probably low for inner-Dallas spec homes. Quote nothing from a pro-forma to a client without the builder's own figures.
- **ARV from resale comps.** There is no new-build premium (the pack knob `new_build_premium_pct` is zero because no public source supports a number) and no size adjustment; the sign of the combined error is unknown. The AVM point estimate is not used.
- **The loan is not capped at a share of ARV**, the draw schedule is a linear approximation, taxes use the purchase price as the land basis and ignore improvements under construction, and setbacks, platted building lines and overlays are ignored in sizing. The maximum offer is a closed form that ignores the rounding of each cost line, so profit at that offer can differ from the target by a couple of cents.
- **The property tax rate is for Dallas ISD addresses inside the city.** Richardson ISD and other districts differ. Only the City of Dallas component was read from an official page.
- **Live `/avm/value` has never been called.** Whether comps' `listingType` values match the sale types, how often there are three or more sale comps, and whether an estimate of a vacant lot returns comps are unverified.
- **Estimates are used for up to 30 days** (reused for `ttl_days`); a market move inside that window is not seen. A ranked candidate below the top N has no estimate by design and shows `no_arv`. On day 2 of the snapshot six candidates compute, not five: the sixth-ranked one still has day 1's estimate.
- **The model results have no retention or deletion path.** `llm_result` keeps verified remarks quotes and accepted narratives by input hash, indefinitely, and re-serves them for identical inputs; `candidate_signals` and `candidate_narrative` grow by a run's rows and are never pruned; `llm_call` is spend and is kept. A quote is redacted listing text, so a future retention or deletion rule must cover `llm_result` as well as the run tables. `llm_result.llm_call_id` has no index, so deleting a ledger row scans the cache (not measured; deletes are rare).
- **A rejected narrative is cached as rejected and is not re-checked on reuse**, because its draft text is deliberately not kept; an accepted one is checked again by the current figure check before it is reused, and a cached signal is verified again by the current verifier.
- **A 400-class provider error fails its candidate once and is not retried by the job**; a 429 or a 5xx defers the rest of the stage and retries the job.
- **Retention is undecided for `proforma` and `candidate_estimate`**, like the other run tables. A comp's address is stored twice, in `candidate_estimate.comps` and in `proforma.result`, so any future retention or deletion path must cover both. The API serves the full result with the comparables' addresses left out; the `proforma show` command, which is local, prints them.
- **A GIS group means more in the pro-forma than in the ranking.** The pro-forma adds up every account on the GIS parcel in the zip; the ranking's aggregate uses only the accounts matched at the listing's street address. On a parcel with two situs addresses the two can differ.
- **Only the price comes from the run itself.** Lot size, property type and year built come from the listing's current row and the parcel from the current table; a parcel import or a later sync between the build and the pro-forma stage can change them. An out-of-order re-run is refused, which keeps that window small.
- **A candidate whose price is not positive gets no pro-forma**, so `proformas` can be lower than `ranked`. After a failed pro-forma stage the counts read zero, and only `sourcing_run.error` tells "did not run" from "none".
- **The pro-forma stage reads a GIS group's parcels once per run** (by GIS id and zip). Migration 0008 indexes `parcel (market, gis_parcel_id)` for it; a test shows the planner can use the index, but the gain at county size is unmeasured, because the demo has 70 parcels.
- **Each stored result carries the whole assumptions block and the 60-cell grid** (about 5 KB a computed row on the demo), so the table grows with every run until retention exists.
- **The `proforma` and `candidate_estimate` rows of runs written before this phase do not exist**, and runs written before migration 0003 serve `match: null`.
- **Stored results are read through today's models.** A pro-forma's `result` embeds the pack's `CostAssumptions`, and the endpoint and `proforma show` validate it against the current model. A later change to the pack schema (a new required field, a tighter validator) would make old rows unreadable: the endpoint would answer 500 and `proforma show` would report it. Result version 1 should get its own frozen copy of the assumptions model before the pack schema changes.
- **A missing lot is labelled as coming from the listing.** When neither the parcel nor the listing has a lot, `lot_source` still reads `listing` on the `unsizable` result.
- **The sync reserve is read once per estimate stage.** `verify-rentcast` and a concurrent `listings.sync` do not take the spend lock, so another spender can run the period below the reserve during the stage. The hard budget stop and the cap still hold.
- **The drift test compares columns and indexes, not check constraints** (Alembic's compare skips them); the check constraints of 0003 to 0005 were compared with `tables.py` by reading.
- **`source show` exits 1 when there is no run, `proforma list|show` exit 2.**
- **Retention duties for flagged accounts are unconfirmed.** If `EXCLUDE_OWNER` marks a confidential address (Texas Tax Code §25.025), what applies to copies already held is a question for counsel. The importer deletes them on the next load that flags them.

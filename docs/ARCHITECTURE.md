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
| Model layer | `aox-agent-core` pinned at git tag v0.1.0 (routing, prices, the Anthropic client, replay and record, eval runner); the Anthropic SDK comes with it. Only `llm/` and `evals/` import either |
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
  mls/reso.py        the RESO record model (mapped fields only), ingest_remarks, the remarks sources
  mls/redact.py      personal-data redaction of remarks (pure)
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
llm/                 the model layer: stages 6 and 7 of the run and everything they need
  client.py          build_model_client / load_llm_config from settings and data/llm/agent-core.toml
  metered.py         MeteredClient: spend guard, ledger row and the call, in that order
  spend.py           RunSpendGuard (run and monthly caps), SessionSpendGuard (evals)
  ledger.py          every llm_call row; the spend sums
  catalogue.py       the twelve remarks signals as data (code, polarity, meaning, notes)
  field_signals.py   price_reduced, relisted, long_on_market (code)
  untrusted.py       normalising, tag defanging and the injection scan of remarks (pure)
  signals.py         extraction prompt v1, closed schema, build_extraction_input, verify_extraction
  facts.py, figures.py  the narrative's facts sheet and the figure strings (pure)
  narrative_check.py the NarrativeDraft schema and the check (pure)
  narrative.py       narrative prompt v1, the one repair, NarrativeResult
  results.py         SignalsResult and the other stored shapes
  store.py           every SQL statement of results, the cache and the cost reads
  run.py             stage 6 (run_signals) and stage 7 (run_narratives) and their failure rules
  render.py, errors.py
evals/               the two eval harnesses (extraction, narrative), their scorers and scorecard writers
api/                 app factory, identity, schemas, and routers: health, markets, parcels,
                     listings, jobs, budget, sourcing, proforma, llm, brief, triggers
  auth.py            bearer parsing, the two scopes, constant-time token comparison (pure)
  gate.py            the ASGI middleware in front of every route: authentication, scope, rate
                     limits, the ban, a body cap
  ratelimit.py       window counters and the failed-authentication ban (bounded, in process)
delivery/            what leaves the database
  brief.py           the Brief models and the pure presenters (figures, comps, signals, narrative)
  build.py           build_brief: a run's rows to a Brief
  store.py           the brief's SQL: set-based reads and the stored copy
  render.py          the lines behind `feasibility brief show`
  document.py, html.py, pdf.py   the pro-forma as a view model, its HTML and its PDF
  notion.py, slack.py            the two service clients, each with an HTTP and a mock transport
  transport.py       the shared pacer, retry helper and response types; errors.py: codes only
  ledger.py          every SQL statement of the delivery ledger, and the per-run lock
  deliver.py         deliver_brief: the order, the skip rules, the failure classes, the dry run
  outbox.py          where mock delivery leaves its PDFs and request lines (MEDIA_OUT)
retention/           prune.py: one bounded batched delete per kind of data past its window
cli.py               the feasibility command
migrations/          Alembic environment and versions/ (0001 initial schema, 0002 sourcing,
                     0003 per-run match, 0004 candidate estimates, 0005 pro-formas, 0006 llm_call,
                     0007 llm_result and the per-run signals and narratives, 0008 schema polish,
                     0009 brief, 0010 delivery, 0011 retention)
```

Outside the package: `scripts/` (`generate_snapshot.py`, `check_rentcast_docs.py`, `build_narrative_cases.py`), `data/snapshot/`, `data/mls/` (the synthetic RESO records), `data/llm/` (the model config and the recordings), `evals/` (answer key, eval-only records, narrative cases, scorecards), `tests/`.

### Phase slots reserved for later

No empty modules exist. These are the planned locations.

| Phase | Module | Purpose |
| --- | --- | --- |
| 6 | `evals/` (exists for the two model tasks) | More suites and the cost per daily run |

## Schema

Alembic revisions `0001_initial_schema`, `0002_sourcing`, `0003_run_listing_match` (each run's match on `run_listing`), `0004_candidate_estimate`, `0005_proforma`, `0006_llm_call`, `0007_llm_results`, `0008_schema_polish`, `0009_brief`, `0010_delivery` and `0011_retention` (the sourcing tables are described under [Sourcing](#sourcing), the pro-forma table under [Pro-forma](#pro-forma), the model tables under [Signals and narratives in the run](#signals-and-narratives-in-the-run-stages-6-and-7)).

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

Phase 4 added four tables:

| Table | Purpose | Key points |
| --- | --- | --- |
| `llm_call` | The ledger: one row per model call, in every outcome | `run_id` and `candidate_id` (null for eval calls), `stage` (`signals`, `narrative`, `eval`), prompt id and version, `input_sha256` (our cache key, not the library's replay key), `tier`, `model`, `mode` (`replay`, `record`, `live`), `billable` (exactly the record and live modes), `outcome`, token counts, `cost_usd` (null when the call raised), `reserved_usd`, `latency_ms`. A row is `ok` exactly when it has a cost. Never cleared by a re-run. No prompt text, remarks or output is stored in it. |
| `llm_result` | The cache of verified results, not run-scoped | Primary key (prompt id, version, tier, input hash); `result` jsonb; `llm_call_id` of the call that made it. |
| `candidate_signals` | One row per ranked candidate and run | `status` (`extracted`, `fields_only`, `failed`, `deferred`), `reason`, the primary `listing_id` read, `result` (`SignalsResult`). Composite foreign key to `run_candidate`, `ON DELETE CASCADE`. |
| `candidate_narrative` | One row per ranked candidate and run | `status` (`accepted`, `rejected`, `failed`, `deferred`, `not_eligible`), `reason`, `input_sha256`, `result` (`NarrativeResult`). Same key and cascade. |

Phase 5 adds `brief` (migration 0009): one row per run (`run_id`, cascading from `sourcing_run`), the `version`, `completeness` (`complete` or `partial`), the whole `content` as jsonb, its `content_sha256` and `built_at`. It is rebuilt in place. Migration 0010 adds `delivery`, the ledger (below).

### The delivery ledger

`delivery` (migration 0010) has one row per run, target (`notion` or `slack`), item and mode (`mock` or `live`): `id`, `run_id` (cascading from `sourcing_run`), `target`, `item` (`digest`, `row:<candidate id>` or `file:<candidate id>`, a check on the shape), `mode`, `status` (`sending`, `sent`, `failed`, `unknown`, `skipped`), `content_sha256`, `remote_ref` (a page id, a message `ts` or a file id; a check allows `[A-Za-z0-9._:-]{1,64}`, so a URL cannot be stored), `attempts`, `error_code` (`[a-z0-9_]{1,64}`), `created_at` and `updated_at`. A `sent` row must have a `remote_ref`. An index on (`target`, `item`, `mode`, `updated_at`) finds the page a Notion row was last written to in any run. The table holds no payload, URL or service text, and its checks refuse them.

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

Job kinds: `cad.import`, `listings.sync`, `sourcing.run`, `morning.run` and `brief.deliver`.

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

where `reserve = days left in the period after today x sync_reserve_per_day`, so every remaining day keeps its listing sync. Syncs outrank estimates. The headroom is read before the first call, and again before each later one (`_may_still_spend`: the budget, the monthly cap and the reserve), so units another caller takes mid-stage (the listing sync, a manual check) defer the rest instead of eating the reserve. The cap counts every billed `/avm/value` row of the period, `feasibility verify-rentcast` calls included; cache hits and refunded calls are not billed. Worked day (31-day month, run on the 1st): 49 left after the sync, reserve 30, 49 - 30 = 19, cap left 20, five targets, so 5 are bought. On the last day the reserve is 0. In a 31-day month that is at most 31 syncs and 19 estimates, in a 30-day month 30 and 20, in a 28-day month 28 and 20.

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
- `feasibility verify-rentcast` takes the same spend lock as the estimate stage (`sources/rentcast/budget.py::spend_lock`) without waiting for it and exits 2 with "a spend is in progress; try again" when another caller holds it. It makes at most 4 live calls, with a ceiling of 10 enforced in code. It validates the responses and writes a local, gitignored report of field names and types only to `local/rentcast-verify-<timestamp>.json`. It spends from the same budget.

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

`proforma/engine.py::build_proforma` turns a candidate's facts, its stored value estimate and the pack's `[cost_assumptions]` into a `ProformaResult` (version 1; Phases 4 and 5 read its keys, so they do not change): sizing, ARV, costs, financing, holding, selling, totals, the most that earns the target margin, and a sensitivity grid. The engine is pure `Decimal` arithmetic; rounding modes live only in `proforma/money.py`, and `docs/proforma-reference.xlsx` (live formulas) is the check on its formulas, read only by the tests. The assumptions a result stores are a frozen copy of the pack's model (`proforma/assumptions_v1.py`: `CostAssumptionsV1` and its parts, field for field), not the pack's own class, and the engine converts the pack's assumptions into it when it builds a result (`frozen_assumptions`). A change to the pack schema therefore cannot make a stored result unreadable or change what it says; a test pins that the snapshot's day-1 and day-2 results are byte for byte what they were before the change (`tests/fixtures/proforma/results_before_v1.json`), and that a pack subclass with an added required field still reads a stored result. To change what a result records, add `assumptions_v2.py` and a result version beside it. Every Dallas cost value is **illustrative**: a labelled default with sources in `docs/proforma-assumptions.md`, not a quote or a builder's actuals.

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

**Without a recording**, a mock-mode run cannot replay: its first call raises a replay miss and the run ends as the table's fourth row says (ranking, estimates and pro-formas stored, the run `completed` with the error set, signals and narratives empty). The repository now holds recordings for the snapshot, so this happens only after something they depend on changed (next section).

`candidate_signals.result` is `SignalsResult` and `candidate_narrative.result` is `NarrativeResult` (both in `llm/results.py` and `llm/narrative.py`). `RemarksInfo.removed_invisible_count` is the number of `[invisible characters removed]` markers in the stored remarks, because only the stored text is kept after ingestion.

## Routing, caps and the ledger

`data/llm/agent-core.toml` is merged over agent-core's packaged defaults, which hold the model ids and prices per tier; no model id or price appears in our code or TOML. Task `signals_extract` routes to tier `small` (Haiku 4.5, `max_tokens` 1,200) and `narrative_write` to tier `mid` (Sonnet 5.5, `max_tokens` 1,500). Task names are snake_case because the library refuses dots in them (prompt ids keep dots). `budget_usd_per_call` is $0.05: the library refuses a call whose worst case exceeds it, and it is also the amount reserved before each call. There is no escalation to a larger tier, so cost stays predictable.

| Setting | Meaning | Default |
| --- | --- | --- |
| `AGENT_CORE_MODE` | `replay` (recordings, no key), `record` (call the model, write recordings), `live` | `replay` |
| `AGENT_CORE_ANTHROPIC_API_KEY` | The only place a key is read (`ANTHROPIC_API_KEY` is ignored). Needed in `record` and `live` only; never given to the `api` service | none |
| `LLM_RUN_BUDGET_USD` | Cap for one run, over every attempt of it | 1.00 |
| `LLM_MONTHLY_BUDGET_USD` | Cap for billable calls in a UTC month | 10.00 |

Mock data allows `replay` and `record` only; live data allows `live` only (replay would miss on real data, and record would write real data into the repository). **Caps are hard by reservation:** before each call the guard refuses if spent plus the reservation would pass the cap; a call that raised after spending counts at its reservation, so recorded spend can never pass a cap. Calls are serial in record and live mode. The ledger row is written before the call, under an advisory lock, and updated after.

## Recordings and how to record again

A recording is a format-2 JSON file under `data/llm/replays/prompts/<prompt id>/v<n>/<key>.json`; its key is content-addressed (prompt id, version, tier, template, system prompt, schema, inputs). `aox-agent-core cassettes check data/llm/replays` validates them, and CI runs it. A recording holds the request (the redacted remarks or the facts sheet, never an address), the model's reply and its token usage, and no key.

**What breaks them.** Changing a prompt, its inputs, the catalogue, the facts sheet, the pro-forma numbers of a computed candidate, the redactor, or the `anthropic` or `pydantic` version in `uv.lock` leaves a call with no recording or a stale one. Replay then raises `ReplayMissError` or `StaleRecordingError`; it never calls a model.

**To record again** (a paid session, on the host, never in a container, with mock data):

1. Use a dedicated key with a console spend limit. Put it in a gitignored `.env` as `AGENT_CORE_ANTHROPIC_API_KEY`, and a fresh scratch database in `DATABASE_URL` (migrated and seeded), used for nothing else.
2. Set `AGENT_CORE_MODE=record`, `DATA_MODE=mock`, and caps: `LLM_RUN_BUDGET_USD`, `LLM_MONTHLY_BUDGET_USD`, and `--max-usd` on each eval. Evals also need `--allow-spend`.
3. Run `feasibility eval signals --split all`, day 1 and day 2 of `feasibility source run`, then `feasibility eval narrative`.
4. Delete the key line from `.env`. **Then regenerate the scorecards in replay** (`feasibility eval signals --split all --out evals/scorecards`, `feasibility eval narrative --out evals/scorecards`) and commit those, not the record-time ones.

Step 4 matters because record mode calls the model every time and overwrites the recording at its key, and a snapshot record's remarks (or a candidate's facts sheet) are the same input in an eval case and in a run. The last answer made for a key is the one kept, so scorecards written during recording cannot be reproduced from the files that remain. `tests/test_eval_scorecards.py` fails when the committed scorecards are not what replay produces. To keep an eval's recordings, run it last.

**The recorded session** (2026-10-09) cost about $0.53; the figures, and what the scorecards show, are in `evals/scorecards/README.md` and `cost.md`.

## Where agent-core attaches

agent-core v0.1.0 is pinned by git tag; its lockfile entry also pins `anthropic` and `pydantic`.

| Capability | How it is used here |
| --- | --- |
| Model client, routing and prices | `llm/client.py` builds `AgentClient` from settings and `data/llm/agent-core.toml`. The stages are part of `run_sourcing` (stages 6 and 7), not separate job handlers; the daily run's one job kind, `sourcing.run`, already carries them. |
| Replay and record | Recordings in `data/llm/replays`; the mode is `AGENT_CORE_MODE`. |
| Spend control | Ours: `llm/metered.py`, `llm/spend.py` and the `llm_call` ledger, in front of every call (agent-core only refuses a single call over its per-call budget). |
| Eval runner | `evals/` builds `EvalSuite` / `EvalRunner` / `Scorecard` with our own scorers and summaries. |
| Tracing | Not wired. No exporter is configured; `[tracing] capture_content = false` keeps prompts and replies out of any span that is created. |
| Audit log | Not used. The ledger records every call; the library's hash-chained log would need its own schema and adds nothing the ledger lacks. A Phase 6 candidate. |

## The API gate and the trigger

Every request passes `api/gate.py`, a pure ASGI middleware that runs before routing, so an unknown path without a token is a 401 (no route is revealed) and a router added later is gated with no code. `required_access(method, path)` decides what a request needs: `GET /livez` nothing; `POST /triggers/<name>` the trigger scope; any other `GET` or `HEAD` the read scope; anything else is refused with a 405 once the caller has authenticated. The presented token is hashed with sha256 and compared with the digests of both configured tokens through `hmac.compare_digest`, always both, so the work does not depend on which one matches. A token that is not configured is the digest of random bytes: unset tokens match nothing, and there is no switch that opens the API. The gate answers 401 (with `WWW-Authenticate: Bearer`), 403 (wrong scope), 405, 413 (a trigger body over 4 KB, declared or streamed), 429 (with `Retry-After`); the security-headers middleware outside it adds its headers to those answers too. A failed attempt logs the address and the path (control characters escaped, no query, never a header). Only a request that presents a credential and fails counts toward the ban (one that presents nothing is refused but not counted, because on the host every request arrives from one gateway address); two `Authorization` headers count as a failed attempt; a websocket connection is refused; `/livez` is exempt from the ban so a container's own healthcheck cannot be locked out. `API_CLIENT_IP_HEADER` must be set only behind a proxy that overwrites that header: the gate cannot tell a forged value from a real one.

- **Limits** (`api/ratelimit.py`, in process, fixed windows, every map bounded so a flood of addresses cannot grow memory): reads per token scope, triggers per hour, `/livez` per address, and a ban of 15 minutes after 20 failed authentications in 10 minutes that also refuses a valid token. The client address is the socket peer; `API_CLIENT_IP_HEADER` names a header to read it from instead and only a valid IP in it is used.
- **`/livez`** is the only open route: a `SELECT 1`, `{"status": "ok"}` or 503 `{"status": "degraded"}`, no revision, mode or key state. The compose healthcheck uses it. `/health` is behind the read token. `docs_url`, `redoc_url` and `openapi_url` are off (`app.openapi()` still works for tests).
- **`POST /triggers/morning`** (`api/routes/triggers.py`) imports the job queue, the payload models and the date check (`sourcing/dates.py`), and nothing that syncs, spends or calls a model (a test checks the module's names). It resolves the pack (422 unknown market) and the date (`resolve_run_date`; mock mode needs a date the snapshot holds, live mode only today), refuses a date earlier than the latest run started (409), and enqueues `morning.run {market, as_of}` with the dedupe key `morning.run:<market>:<as_of>` (202 with the job id; 200 `already_active` on a dedupe hit).
- **`morning.run`** runs `run_sourcing`, then enqueues `brief.deliver {run_id}` (dedupe `brief.deliver:<run_id>`). A `PermanentModelError` (a missing recording, a misconfigured model) leaves a ranked, completed run with its error recorded: the brief is still queued, as `partial`, and the job re-raises so it ends `dead` and stays visible. A `RetryableModelError` queues nothing: the retry reuses every cached result and briefs when it finishes. **`brief.deliver`** builds the brief and stores it; `BriefError` (no such run, a run not completed) is permanent.

## The brief

`delivery/brief.py` defines the frozen `Brief` and the entries inside it. Money and ratios are decimal strings, as the API serves them; display formatting is left to the presenters, which use `llm/figures.py`, so what a reader sees is what the narrative was checked against. The types have no field that could hold a comp address, listing text, a signal's quote, a rejected or deferred draft, an error message or a model-written figure.

`build_brief(connection, run_id)` reads in a fixed number of set-based statements (the run, the status counts of its pro-formas, the first ten computed ones with their stored results, their signals, their narratives) and raises `BriefNotReadyError` for a run that is not `completed`. For each computed candidate: figures from the stored result; the verdict as the code facts of `llm/facts.py`; the comps as the count used, the median $/sq ft and the lowest and highest price of the comps used; the flags with their written meanings; the signals as code, polarity, source and the meaning written in `llm/catalogue.py` or `llm/field_signals.py`. **Narrative rule:** an `accepted` narrative is rebuilt into a draft (`llm.narrative.draft_of`) and checked again by `check_narrative` against facts rebuilt from today's rows; if it passes it is delivered under a fixed label ("Written by a language model. Every figure in it was checked against this pro-forma by code."), if it fails it is `withheld` ("Withheld: the narrative no longer matches this pro-forma."). A `rejected` narrative is `withheld` ("Withheld: the draft did not pass the figure check.") and a failed, deferred or not-eligible one is `not_available` ("Not available today."); none of those carries any model text. **Before delivery, the text rules.** The figure check looks at numbers; a narrative is also checked here for what a number check cannot see (`delivery/brief.py::unsafe_text`): only plain prose characters (an allowlist: letters, digits, space and `.,;:'"%$()!?/-`, so look-alikes, markup, template syntax, control and invisible characters are out), no link (a scheme, `www.` or a dotted name), no injection phrasing (`scan_injection`), no word of five or more capitals, no street name of four letters or more from this brief's candidates or from the sales their values rest on, and no 24-character stretch of the listing's own words. A narrative that fails is `withheld` ("the narrative held text that is not safe to deliver"). When the listing's text was flagged as an injection, no remarks signal and no narrative built on the facts it chose is delivered (`remarks_withheld`). A flag in a brief must be a code; a street is normalised again; a stored signal must have a code of its source and the catalogue's polarity; the stored pro-forma result must agree with its own columns and its comp count; and a brief is built from one `REPEATABLE READ` snapshot, only after the run's last stage ended (`sourcing_run.stages_finished_at`, set when the stages finish or when one records its error) and from the mode the run ran in (`sourcing_run.data_mode`). The stored brief goes when its run's rows are rebuilt, and the endpoint serves it only if it still matches its hash.

A brief is `partial` exactly when `sourcing_run.error` is set (notice `later_stage_failed`); the error text is never copied. The content hash covers the canonical JSON and no timestamp, so a same-day re-run that changes nothing hashes the same.

## The PDF and the adapters

`delivery/document.py` turns a brief entry and its stored pro-forma into a `ProformaDocument`: display strings only, formatted by `llm/figures.py`, so a template cannot compute, round or reformat a number. `delivery/html.py` renders it with Jinja2 (autoescape on, `StrictUndefined`, templates and CSS from the package, no network); `delivery/pdf.py` renders that HTML with WeasyPrint. The fetcher is the point of the module: WeasyPrint resolves every URL through `safe_fetcher()`, which allows only `file:` URLs that resolve inside the package's templates and fonts and raises for anything else, so a value that somehow became a URL (an image, a stylesheet, a font) cannot read a local file or call out. Tests render a document whose every text field holds an attack string and check the PDF's text, its links and its annotations (`pypdf`), and a golden HTML file for three scenarios pins the structure; a PDF's bytes are not compared, because they carry a creation time and an id.

Design tokens are in `delivery/templates/tokens.css` (ink `#15181b`, on-ink `#ffffff`, ink-soft `#e9ecef`); `brief.css` sets the page, the running footer (the illustrative-values line and "Page n of m") and the rule that the sensitivity block stays on one page. Space Grotesk has no static SemiBold, so `SpaceGrotesk-SemiBold.ttf` is an instance cut from the variable font (the source's hash is in `THIRD_PARTY_NOTICES.md`).

`delivery/transport.py` holds the shared parts: a `Pacer`, the response type and `send_with_retries`, the one retry helper both clients use. A 429 is waited out for the service's `Retry-After` (capped at 30 s) up to three times, because it means the request was not processed; a 5xx or a lost connection is retried twice (after 1 s and 4 s) **only for a request that is safe to send twice** (reads, queries, updates, the upload-URL request). Creating a Notion row, posting the Slack digest and completing a Slack upload are never retried after a 5xx or a lost connection, because the first attempt may have succeeded: they raise `OutcomeUnknownError` (`NotionOutcomeUnknownError`, `SlackOutcomeUnknownError`), which the delivery ledger records as `unknown` (see [Delivery, the ledger and the morning run](#delivery-the-ledger-and-the-morning-run)). A Notion 400 is still `validation_error` whatever its cause, so a bad payload and an archived page look the same; the update path treats it, and a 404, as "look the row up by its key". A Notion query may not see a row created a moment ago (not verified against the live API). `delivery/notion.py` and `delivery/slack.py` each have a client over a transport protocol with an HTTP transport (`httpx`, a short timeout, no redirects, the token in one header; Slack's upload goes through a second client with no token and no base URL) and a mock transport (Slack ids `mock-ts-<n>` and `mock-file-<n>`; Notion page ids `mock-page-<12 hex of the row key>`, so two processes never hand one id to two rows). `build_notion_transport(settings)` and `build_slack_transport(settings)` pick the mock unless `DELIVERY_MODE=live`. An error never carries a response body, a header or a URL: it is reduced to a code (`http_<status>`, `validation_error`, `network_error`, or Slack's own error code if it looks like one) checked against `^[a-z0-9_]{1,64}$`.

**Notion.** One row per candidate, found by a `Candidate key` property (`<market>:<candidate id>`) and created or updated, so a candidate keeps one row and a later run overwrites it; `row_properties` returns the properties the app owns and nothing else, so `Decision` is never in a payload. Numbers go as floats converted from the exact decimal strings (Notion numbers are floats; the exact strings are in the brief and the PDF). The API version is pinned to `2022-06-28` and the database endpoints of that version are used.

**Slack.** `digest_blocks` builds a header, one section per candidate and the footer; all dynamic text is escaped (`&`, `<`, `>`) and sent as `mrkdwn` with `verbatim: true`, with unfurling off, so a stray `@channel`, a link or a markup character is inert. The PDFs go through the external upload flow (`files.getUploadURLExternal`, a POST of the bytes, `files.completeUploadExternal` into the digest's thread).

**Credentials.** The tokens are `SecretStr` settings, read only by `build_*_transport`; compose gives them to the worker and the migrate service and to nothing else, and they are dropped when the mode is mock. A test reads every payload, log line and error text for them, and in live mode the settings refuse a token for a service that is not a target (in mock mode a token is dropped, not refused).

## Delivery, the ledger and the morning run

`delivery/deliver.py::deliver_brief(engine, settings, run_id, ...)` is the only code that sends a brief. Order: take the run's lock, build the brief from the database now (`build_and_store`: one `REPEATABLE READ` snapshot; the stored row is a copy for audit and is never what is sent), render the PDFs that still need sending (so a document that cannot be made stops the delivery while nothing is half-sent), write the Notion rows, post the Slack digest, upload each PDF into its thread.

**The ledger brackets every call** (`delivery/ledger.py`, short transactions, never one open during a call): `start` upserts the item `sending` with one more attempt and commits; the call is made; `finish` records `sent` with the reference, `failed` with a code, or `unknown`. A `sending` row older than ten minutes reads as `unknown` (`ledger.read`). **The lock** is a session-level `pg_try_advisory_lock` on a connection of its own, keyed by the run, so a second delivery of the same run (the worker and a person at the command line) gets `DeliveryBusyError` at once instead of posting a second digest; a crash drops the connection and frees it. A fresh `sending` row (a digest or a file) with no lock holder is a call that is too new to judge: the delivery stops with `DeliveryBusyError` (code `recent_call`, with the report of what was done before it) and a retry finds it `unknown` once it is ten minutes old. The same error with code `busy` means another delivery holds the lock and nothing was sent. If unlocking ever fails, the lock connection is ended (`invalidate`) instead of being returned to the pool with the lock still on it.

**The classes of failure** come from the clients. `send_with_retries` (`transport.py`, shared by both) waits out a 429 (the request was not processed) and retries a 5xx or a lost connection twice only for a request that is safe to send twice. For a request that is not (creating a Notion row, posting the digest, completing an upload) it raises `OutcomeUnknownError` instead of retrying: the service may have acted. A 200 whose body says `internal_error`, `fatal_error`, `request_timeout` or `service_unavailable`, or is not JSON, is treated the same way for a post or a completion, because Slack documents that part of the operation may have happened. `deliver_brief` maps that to `unknown` for Slack, which is never retried automatically (`DeliveryUnknownOutcomeError`, permanent: the job goes `dead`; `--resend slack` is the operator's decision, and it forgets only the rows whose outcome is unsettled, so a sent digest stays and is not posted again). Notion's failed rows are not retried while Slack is unknown (the job is dead): `brief deliver --only notion` sends them, and to `unknown` for Notion, which is retried because the row is found by its key first (`DeliveryIncompleteError`, retryable). A definite refusal is `failed` (`DeliveryIncompleteError`; a retry sends only what is left). Credentials, a channel Slack does not know, a database missing an owned property, an unconfigured target or live delivery with no token for it are `DeliveryConfigError`: that target stops (its later items are reported `not_attempted`), the other target still runs, and the job ends permanent. `PERMANENT_ERRORS` holds `DeliveryConfigError` and `DeliveryUnknownOutcomeError`; `DeliveryIncompleteError` and `DeliveryBusyError` are retried by the job's backoff.

**Notion** (at-least-once): the item's content hash is the sha256 of its canonical property JSON; if the newest `sent` or `skipped` row of the same item and mode in any run has the same hash the item is `skipped` and nothing is requested. A row whose last delivered page belongs to a later day's run is `superseded` and left alone, so delivering an older run again (a resend, a late job) never writes old figures over new ones. Otherwise the database schema is read once, then the page last written to (`ledger.remembered`) is updated; a 404 or a validation error there (gone, archived) falls back to a query by key and an update or a create. **Slack** (at-most-once): the digest once per run and mode; each file once, in the digest's thread; a file whose state is `unknown` is left for a person. A mock delivery paces nothing; a live one is paced to each service's limit. The HTTP clients a delivery builds are closed when it ends.

**Dry run** builds, prints every payload and renders the PDFs to a folder, makes no request, writes no ledger row and takes no lock. **Mock outbox:** with `MEDIA_OUT` set, mock transports append one JSON line per request they answer and save each uploaded file under the name the client chose (a rank and an id, never an address) in `<MEDIA_OUT>/<market>-<date>/`.

**The morning chain.** `POST /triggers/morning` (trigger token) queues `morning.run`; the worker runs the sourcing stages and queues `brief.deliver {run_id}`; that job calls `deliver_brief`. Nothing in the API process delivers, renders or calls a model. `GET /sourcing/runs/{id}/deliveries` (read token) serves the ledger: target, item, mode, status, attempts, error code, remote reference and time, and nothing else.

**n8n** (`profiles: [schedule]`, never started by `up` or by CI) is the clock. `n8n/morning-brief.json` is a schedule trigger, a fixed market and one POST to the API's trigger route with a Header Auth credential that lives in n8n's encrypted store; `tests/test_n8n_workflow.py` fails on any other node type, any inbound webhook, any value shaped like a token and any stored header. Its compose service holds none of the application's variables, is on a `schedule` network shared with the api and not with the database (that network is not `internal`, so n8n can still reach the internet and anything listening on the host: it is a clock behind a localhost-only editor, not a sandbox), excludes the node types that run a command, read a file, run code or listen (`NODES_EXCLUDE`, the types of the n8n 2.42.6 catalogue that do, plus the form, chat and MCP triggers), cannot install community packages, blocks environment access from nodes, has telemetry and update checks off, binds `127.0.0.1:4503`, and runs with a read-only root (its `/tmp` and its `~/.cache` are memory; without the cache folder n8n stops at start) and all capabilities dropped. The image is pinned by index digest. Measured on 2026-10-10: it settles at about 440 MiB and its peak touched 512 MiB while it migrated its database at the first start, so its limit is 1024m. A run started by the schedule trigger itself (a copy of the workflow on an every-minute cron, published, with n8n restarted, as `publish:workflow` requires) reached the API's trigger route with the credential and left the database, the ledger and the request lines exactly as they were, because the day had already run. The `n8n execute` command cannot run this workflow (it needs a manual trigger), so the schedule trigger is what was exercised.

## Retention

`retention/prune.py::prune(engine, policy, as_of=..., dry_run=...)` deletes stored data once it is strictly older than its window, one function per kind of data, each a loop of bounded batches (1,000 rows) in short transactions of their own (a failure part-way leaves a consistent database, and the next prune finishes). Windows are settings with floors that protect something that still reads the data:

| Data | Window | Floor, and why |
| --- | --- | --- |
| Run detail: `run_listing`, `run_candidate`, `proforma`, `candidate_signals`, `candidate_narrative`, `brief`, `delivery` | `RETENTION_RUN_DAYS`, 90 | 8: a run's estimate is reusable for 7 days |
| `candidate_estimate` (with its comps) | `RETENTION_ESTIMATE_DAYS`, 90 | 30: an estimate older than 30 days is no longer used |
| `llm_result` (the model cache) | `RETENTION_MODEL_CACHE_DAYS`, 30 | 1 |
| `llm_call` (spend) | `RETENTION_LEDGER_MONTHS`, 13 | 2: the monthly cap reads the current month |
| `job` finished (`done` or `dead`) | `RETENTION_JOB_DAYS`, 30 | 1 |
| `api_cache` | the run window | |
| `listing` (remarks) and `candidate` | `RETENTION_LISTING_DAYS`, 30, and only when nothing refers to them | 1 |

`api_request_log` is kept (the budget audit; no content). **Never deleted:** a market's latest run (the next run's diff and the pages Notion rows point at), a run still running or not yet finished with its stages, a spend row inside its months, and any listing or candidate a kept row refers to. A pruned run keeps its own row (counts, error) with `pruned_at` set; `GET /sourcing/runs/{id}` serves it, `previous_fresh_run` skips such a run, and running the same date again clears the mark. Each run is pruned in one transaction under the market's run lock (`lock_market_runs`), after re-checking that it still qualifies, so a prune cannot race a run that started again. The delete order is children first and no row relies on a cascade, so the counts are exact. The dry run counts through the same predicates (treating the runs that would go as already gone, so it reports the listings and candidates that only those runs were holding on to) and deletes nothing.

Comparable sales' addresses live in two places, `candidate_estimate.comps` and `proforma.result`; a pro-forma's run window and an estimate's window are both 90 days so they go in the same pass, and a test searches every table's text for a seeded address after a prune. **Out of reach:** rows already sent to Notion and messages and PDFs in Slack are not deleted. **Left behind on purpose:** the latest run of a market keeps its own comps until a newer run replaces it, so if daily runs stop, its addresses stay.

Run it with `feasibility retention prune [--dry-run] [--as-of DATE]` (with live data `--as-of` is accepted only together with `--dry-run`), as the job `retention.prune`, from `POST /triggers/retention` (trigger token; `{"dry_run": true}` optional; one at a time), or from the n8n workflow's weekly node (Sunday 03:00). Migration 0011 adds `sourcing_run.pruned_at`, an index on `llm_result.llm_call_id` (deleting an old `llm_call` row nulls that column through the foreign key) and four indexes for the "is anything still using it" checks; the date predicates have none, and the migration records the `EXPLAIN` numbers behind both choices.

## Compose and CI

`docker-compose.yml` (project name `realestate-feasibility`):

| Service | Role |
| --- | --- |
| `db` | `postgres:16-alpine` on `127.0.0.1:4502`, with a healthcheck and a named volume |
| `migrate` | One-shot: `feasibility migrate && feasibility seed` (both idempotent) |
| `api` | `127.0.0.1:4501`; starts after `migrate` completes successfully; has a healthcheck |
| `worker` | `feasibility worker`; starts after `migrate` completes successfully |
| `n8n` | Opt in with `--profile schedule`: the clock that triggers the morning run (see the delivery section); on its own `schedule` network with the api, on `127.0.0.1:4503` |

Every service sets `mem_limit`, each at least twice the peak working set a seeded day-1 run showed in `docker stats` (the measurements are in the compose file's header), and `tests/test_compose.py` fails if one does not. All ports bind to 127.0.0.1. Port 4500 is reserved and unused (the report viewer was dropped) and 4503 stays free for n8n. A fresh clone needs no `.env`: compose falls back to local-only development defaults, mock mode and a localhost-bound database password.

**Container hardening.** Base images are pinned by index digest, with the tag kept in a comment beside it: `python:3.12-slim` and `ghcr.io/astral-sh/uv:0.12.10` in the Dockerfile, `postgres:16-alpine` in compose and in the CI service container. Never use `:latest`. To refresh a digest, request the manifest from the registry and read the `Docker-Content-Digest` response header (for example `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' <registry manifest URL for the tag>`), then update every place the image appears.

`migrate`, `api` and `worker` run with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, a 256-process limit and a 64 MB `noexec` tmpfs at `/tmp` (the seed writes temporary CSV archives there). `db` is read-only too, with tmpfs at `/tmp` and `/run/postgresql`, the data volume writable, and only the five capabilities its entrypoint needs to chown the data directory and drop to the postgres user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`).

CI (`.github/workflows/ci.yml`) runs on pushes to `main` and on pull requests to any base branch (so stacked pull requests get their checks; a test parses the file and checks the trigger, because an unquoted `run:` value with a colon and a space in it once made the whole file invalid and GitHub ran nothing) and has four jobs:

- lint: ruff check, ruff format check, mypy strict, and `aox-agent-core cassettes check` on the recordings
- test: pytest against a Postgres 16 service container on a separate test database (this includes the tests that regenerate the eval scorecards from the recordings, in replay with no key)
- gitleaks: scans the full history
- compose smoke: `docker compose up -d --wait --build` with tokens made for the job, then `curl` checks of the gate: `/livez` open, a gated route 401 with no token and 200 with the read token, `/health` behind it, `/docs` 404, and the read token refused (403) on the trigger route

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
- **Listing text.** RentCast listings have no description field, so live data has no remarks and a live day's model work is narratives only. Remarks extraction runs only on the synthetic RESO-shaped set (`data/mls/dallas.json`) and on a client's MLS feed, which is not built.
- **AVM comps** are filtered by `listingType` because the published schema has no sale/rent flag. The list of sale types is taken from the documentation.
- **No update schedule and no downloader for DCAD.** The operator downloads files by hand. DCAD publishes no redistribution license that we found, so its files are never committed.
- **A listing with no directional never matches a parcel that has one.** `5521 WEXCOMBE AVE` against parcels `5521 N WEXCOMBE AVE` and `5521 S WEXCOMBE AVE` is `unmatched`: the stem keeps the directional (`N WEXCOMBE`), so neither the exact nor the stem lookup finds anything. It is left unmatched, not guessed, and a test asserts it.
- **Price changes and delistings are only visible inside the feed window.** The free tier's feed is `days_old` days wide; a listing that ages out of it can no longer show a price change or a delisting, and is recorded as `aged_out`.
- **RentCast's `daysOld` semantics and result ordering are unverified**, so is whether a delisting reaches the feed as a status flip. The diff's `gone` rule depends on them.
- **Stem matches can be wrong on streets that differ only by suffix** (`OSTRAVELLE AVE` and `OSTRAVELLE DR`). A stem match requires exactly one hit in the zip, which limits the damage; the match method is stored so a stem match can be audited.
- **A candidate's key upgrades only once**, from `addr:` to `acct:` or `gis:`. If a property later matches a different account, it becomes a second candidate.
- **`load_parcel_index` loads every parcel of the requested zips into memory.** Estimated at about 73 MB per 70,000 parcels, close to the worker's 256 MB limit; check before adding zips near 100,000 accounts. Narrowing by street number is not done.
- **A new source's listing can make a continuously listed property `relisted`.** A listing from a source not seen before, for a property that had a candidate on an earlier day, is classified as relisted by the property rule even though another source's listing was in the previous feed.
- **The synthetic AVM comps for the six estimate candidates are not market data.** They are drawn from per-zip $/sqft ranges chosen so the demo's pro-formas spread out (`NEW_BUILD_PSF_BY_ZIP` in the generator), and say nothing about real Dallas prices.
- **`/properties` fixtures are keyed on the title-case address** and would miss in mock mode; nothing calls that endpoint, so they are left alone. The `/avm/value` fixtures are keyed on the normalised one-line address the client sends.
- **The live estimate spend has never run against RentCast.** It is tested through stub transports only. Not verified live: whether `/avm/value` returns sale comps for most addresses, whether it returns any for a vacant lot, and (as above) whether 404 and 429 are billed.
- **The spend and the client can disagree about the billing period near its boundary.** The cap and reserve use the period that contains the run date (the market's time zone); the budget reservation uses the UTC date. When the two differ, before the first call or between two calls, the stage spends nothing more and defers the rest until the next run; the hard budget stop is unaffected.
- **Estimates are reused for `ttl_days` and a market move inside that window is not seen.**
- **The API's tokens are static.** No expiry, rotation or per-person identity: rotate by changing `.env` and restarting the api. Rate limits and the ban live in the API process: they reset on a restart and assume one process. Behind a proxy or tunnel, set `API_CLIENT_IP_HEADER`, or every request shares the proxy's address and one attacker can ban everyone. Nothing here is a reason to expose the API beyond localhost without a reverse proxy that terminates TLS.
- **Pro-formas are illustrative.** Every Dallas cost value is a labelled default from aggregator and lender-blog pages, not a quote or a builder's actuals. Contingency, the build/marketing split, the living share and the default zoning rule are unsourced; hard cost is probably low for inner-Dallas spec homes. Quote nothing from a pro-forma to a client without the builder's own figures.
- **ARV from resale comps.** There is no new-build premium (the pack knob `new_build_premium_pct` is zero because no public source supports a number) and no size adjustment; the sign of the combined error is unknown. The AVM point estimate is not used.
- **The loan is not capped at a share of ARV**, the draw schedule is a linear approximation, taxes use the purchase price as the land basis and ignore improvements under construction, and setbacks, platted building lines and overlays are ignored in sizing. The maximum offer is a closed form that ignores the rounding of each cost line, so profit at that offer can differ from the target by a couple of cents.
- **The property tax rate is for Dallas ISD addresses inside the city.** Richardson ISD and other districts differ. Only the City of Dallas component was read from an official page.
- **Live `/avm/value` has never been called.** Whether comps' `listingType` values match the sale types, how often there are three or more sale comps, and whether an estimate of a vacant lot returns comps are unverified.
- **Estimates are used for up to 30 days** (reused for `ttl_days`); a market move inside that window is not seen. A ranked candidate below the top N has no estimate by design and shows `no_arv`. On day 2 of the snapshot six candidates compute, not five: the sixth-ranked one still has day 1's estimate.
- **Model results and their copies.** `llm_result` is pruned after 30 days and `llm_call` after 13 months (see Retention); a quote in a cached result or a signals row is redacted listing text, which retention removes with the run. A call's recorded cost is spend, so a spend row is never touched inside its months.
- **A rejected narrative is cached as rejected and is not re-checked on reuse**, because its draft text is deliberately not kept; an accepted one is checked again by the current figure check before it is reused, and a cached signal is verified again by the current verifier.
- **A 400-class provider error fails its candidate once and is not retried by the job**; a 429 or a 5xx defers the rest of the stage and retries the job.
- **A GIS group means more in the pro-forma than in the ranking.** The pro-forma adds up every account on the GIS parcel in the zip; the ranking's aggregate uses only the accounts matched at the listing's street address. On a parcel with two situs addresses the two can differ.
- **Only the price comes from the run itself.** Lot size, property type and year built come from the listing's current row and the parcel from the current table; a parcel import or a later sync between the build and the pro-forma stage can change them. An out-of-order re-run is refused, which keeps that window small.
- **A candidate whose price is not positive gets no pro-forma**, so `proformas` can be lower than `ranked`. After a failed pro-forma stage the counts read zero, and only `sourcing_run.error` tells "did not run" from "none".
- **The pro-forma stage reads a GIS group's parcels once per run** (by GIS id and zip). Migration 0008 indexes `parcel (market, gis_parcel_id)` for it; a test shows the planner can use the index, but the gain at county size is unmeasured, because the demo has 70 parcels.
- **The `proforma` and `candidate_estimate` rows of runs written before this phase do not exist**, and runs written before migration 0003 serve `match: null`.
- **A missing lot is reported as `missing`.** When neither the parcel nor the listing has a lot, `lot_source` reads `missing` on the `unsizable` result. A ranked candidate always has a lot, because the buy box needs one, so this shows only when a stored parcel or listing has lost it since. Pro-formas stored before this change read `listing` in that case, and are not rewritten; code from before it fails to validate a stored `missing`, so a rollback past it needs those rows recomputed (a run rebuilds them).
- **The drift test compares check constraints too**, which Alembic's compare skips: `tests/test_schema.py` builds the schema from `tables.py` into a scratch schema and compares each check constraint's name and Postgres-printed expression with the migrated one (a second test breaks three constraints on purpose to show the comparison fails). It found no drift when it was added.
- **Upgrade a database to 0008 before downgrading it.** A database built before 0008 has doubled check-constraint names (`ck_x_ck_x_y`); the downgrade steps of 0003 to 0005 drop the single names, so they fail on such a database until 0008 has renamed them. A database built from scratch is not affected.
- **Retention duties for flagged accounts are unconfirmed.** If `EXCLUDE_OWNER` marks a confidential address (Texas Tax Code §25.025), what applies to copies already held is a question for counsel. The importer deletes them on the next load that flags them. A candidate's street also lives in the stored `brief`, in Notion rows, in Slack messages and files and in the mock outbox folder; none of those has a deletion path except the stored `brief` and the mock outbox's owner (the Notion and Slack copies are out of the program's reach, and retention does not touch them). Retention removes the stored brief with its run, after the run window.
- **Two eval targets are missed, and the numbers rest on a small set.** The narrative eval's acceptance is 8 of 9 scored cases (88.9%) against a 0.90 target, and 4 of its 13 cases errored because the mid tier's 1,500-token limit cut the JSON off (each recorded reply ended at exactly 1,500 output tokens, and 8 of the 13 replies that finished used 1,222 or more, so the limit has little headroom and the live cost projection, which assumes success, may be optimistic; the cut-off replies hold little text for their token count, so the cause may not be the visible text alone); the extraction eval's dev split fails one injection case on its "signal set equals the key" check. Extraction precision on the holdout (90.2%) is on its target. The eval set (56 extraction and 13 narrative cases) is synthetic and written by this project, so precision and recall on real listings are unknown. See `evals/scorecards/README.md`.
- **Recordings are tied to the prompts, the inputs and the `anthropic` and `pydantic` versions in `uv.lock`.** Changing any of them means a paid re-recording. Record mode overwrites a recording at its key, so scorecards must be regenerated in replay after recording (see "Recordings and how to record again").
- **A failed call's true cost is unknown.** The library raises without usage, so the ledger counts the reservation: spend is over-stated, never under-stated. The four cut-off narrative calls of the recording session are counted at $0.20 against about $0.08 that their recorded usage implies.
- **The monthly cap is a UTC month and covers only this app's ledger.** Other use of the same key is not seen; use a dedicated key with a console spend limit.
- **The narrative is checked for figures, basis codes and length, not for quality or tone.** No model judges another. Quality rests on a person reading samples.
- **The figure check has known gaps** (decided in Phase 4c). Arithmetic words are read only *before* a figure and only for the listed words, within three words of it: an inflected word after a figure (`$107,560.14 doubled`, `halved`, `tripled`, `doubling`), and an arithmetic word more than three words before a figure, get through. Only `(` and `)` count as an accounting negative: `[$107,560.14]` and `{...}` do not. Angle-bracket lookalikes are a list of code points and entities that are folded before defanging and the injection scan; other code points that look like `<` or `>`, entities without a semicolon, double-encoded `&amp;lt;` and other encodings (`%3C`, `\x3c`, `\u003c`) are not folded. Number words are a blocklist: a number in a language or slang the lists lack is out of reach of a list. The structural defences do not depend on these: a closed schema, any ASCII digit outside an exact standing-alone figure rejected, ASCII-only text, no listing text in the facts sheet.
- **The injection scan is a heuristic list.** The structural defences (closed schema, verified quotes, no digits from quotes, no tools) are the real guard.
- **Haiku-tier extraction is the routing choice and its quality on real remarks is unmeasured.** Moving extraction to the mid tier is a config change plus a re-recording.
- **Tracing and the library's audit log are not used.** The ledger is the only record of calls.
- **Mock-mode triggers need an explicit date**, as `source run` does. The trigger queues a job; if no worker is running nothing happens until one is.
- **The brief covers computed pro-formas only** (at most ten, in rank order), and counts the rest. Its signals are code and meaning, never the quote, so a reader cannot see the listing words behind a signal; the quote stays in `GET /sourcing/runs/{id}/candidates/{id}/llm`.
- **A figure under another figure's name is not caught.** `The profit is <the ARV string>.` passes the figure check, which tests that a figure is real and not what it is called. A test pins it (`test_b12`, an `xfail`); the facts sheet's figure names are in the narrative's prompt, but nothing checks the pairing.
- **Street names of three letters or fewer are not caught** by the street-name rule ("Oak", "Elm"), and the rule compares letters only. The brief's own street and the comps' streets are the only names it knows.
- **The trigger scope learns a little.** The token that can read nothing still learns the latest run date (from a 409), the snapshot's dates (from a 422) and sequential job ids. The read and trigger limits are per scope, so twelve malformed triggers in an hour use up the trigger token's hour.
- **Attack tests.** `tests/test_attacks_*.py` hold the attack list's tests (the list is in the plans folder, not in this repository): injection through to delivery, invented values, credentials, and the gate. Those marked `xfail(strict=True)` name the session that makes them pass.
- **The Notion and Slack clients have never run against the real services.** They were written from the public API documentation and tested against recorded fakes only. Not verified: the exact error shapes, rate-limit headers and pagination of the live APIs, the external file upload flow in a real workspace, and how a Notion percent-format column shows `Margin`. Run `feasibility brief smoke notion|slack` against a test workspace first.
- **Notion API version.** The clients use `2022-06-28` and the database endpoints of that version. Notion's 2025 versions introduced data sources, which change where a database's properties live; moving to them is a future change and `notion-check` would show the difference.
- **The PDF is checked structurally, not by eye on every run.** Sample renders were looked at once; the tests check text, links, annotations, page count and fonts. The fixtures are three ordinary scenarios (a 15-character street, a short narrative, at most three flags); a long street name, a long narrative or a candidate with every flag has not been rendered, so a layout break at an extreme is unknown.
- **Street names of three letters or fewer are not caught by the narrative's street-name rule** (`unsafe_text`); such a name needs the other rules to catch it.
- **A figure under another name (B12).** A narrative that states a true figure of the pro-forma under a wrong label passes the figure check, which compares numbers, not what they are called. The attack test for it is `xfail(strict=True)`.
- **WeasyPrint and the container.** The image needs Pango, HarfBuzz subsetting and a cache directory it can write (`XDG_CACHE_HOME=/tmp/.cache`). Measured in the built image under a 256 MB limit, rendering the three fixture pro-formas nine times in one process: peak resident memory 149 MB (92 MB before the first render), 241 KB of PDF in all. The image grew from 343 MB to 465 MB (Pango, HarfBuzz, the fonts and WeasyPrint's dependencies). Measured on the fixtures, not on a ten-candidate live brief and not through a live worker; the worker's limit is 384m for that reason (see `docker-compose.yml`).
- **Delivery has run only against mock services.** The ledger, the lock, the skip rules and the failure classes are tested with transports that lose replies, answer 429 and 5xx and refuse credentials, and the morning run was run end to end on the compose stack in mock mode; no request has reached Notion or Slack. Not verified: how Slack treats the digest's text and the file thread, whether a Notion query sees a row created a moment ago (the unknown-outcome path relies on it), Notion's answer for an archived page (a 400 is read as "look the row up"), and rate limits under a real workspace.
- **A crash between `start` and the call leaves a row `sending`.** A Notion row is simply sent again (it is found by its key); a Slack item reads as `unknown` after ten minutes and waits for a person. The digest's content hash is stored but not compared: a re-run that changes the day's content does not post a second digest (one per run and mode), though it does update the Notion rows.
- **The lock holds one database connection per delivery** for as long as it runs, and is per database: two databases would not see each other's deliveries.
- **Mock delivery is stateless across processes.** A mock Notion page id comes from the row's key so ids do not collide, but a new process does not remember the pages an earlier one created; the ledger carries that. Under mock delivery the "find the row by its key" path therefore always creates.
- **n8n needs a person once.** The owner account and the Header Auth credential are made in its editor (or imported from a file kept outside the repository), because the workflow file holds no token. In mock mode a scheduled run with an empty `as_of` is refused (422) by design, so the "Markets" node needs a date to try the schedule. Whoever opens the editor first becomes its owner, so open it once through the SSH tunnel before anyone else can reach the port. A run that hits the trigger's hourly limit or an earlier date is refused and n8n retries it three times a minute apart.

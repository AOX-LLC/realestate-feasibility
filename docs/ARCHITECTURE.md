# Architecture

This document describes the system as built in phase 1, and where later phases attach.

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
listings.py          upsert of listing batches
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
  load.py            load the committed snapshot into the database (the seed)
api/                 app factory, identity, schemas, and routers: health, markets, parcels,
                     listings, jobs, budget
cli.py               the feasibility command
migrations/          Alembic environment and versions/0001_initial_schema.py
```

Outside the package: `scripts/generate_snapshot.py`, `scripts/check_rentcast_docs.py`, `data/snapshot/`, `tests/`.

### Phase slots reserved for later

No empty modules exist. These are the planned locations.

| Phase | Module | Purpose |
| --- | --- | --- |
| 2 | `sourcing/` | Buy-box filtering, listing-to-parcel matching, ranking |
| 3 | `proforma/` | Pro-forma engine |
| 4 | `llm/` | Signal extraction and risk narratives |
| 5 | `delivery/` | Brief rendering and delivery |
| 6 | `evals/` | Eval runner over the snapshot |

## Schema

Alembic revision `0001_initial_schema`.

| Table | Purpose | Key points |
| --- | --- | --- |
| `source_file` | One row per imported CAD file set | `market`, `source`, `kind` (`certified` or `current`), `roll_year`, `file_date`, `sha256`, `carries_values`, `status` (`loading`, `loaded`, `failed`), row counts (read, loaded, skipped) and `skip_reasons`. Unique on (market, source, kind, roll_year, file_date). |
| `parcel_version` | The parcel as of each file | Primary key (market, account_id, source_file_id). Situs fields, `zip5`, land, improvement and total value (`numeric(14,2)`, NULL when the file has no values), `year_built`, `living_area_sqft`, `lot_size_sqft`, `use_code`, `zoning`. |
| `parcel` | Current parcel state | Primary key (market, account_id). Same columns plus `attrs_file_date` and `values_file_date`. Attributes are upserted when the incoming file date is at least `attrs_file_date`. Values come only from files that carry values, and only when at least as new as `values_file_date`. A values-free load never blanks certified values. |
| `listing` | Listings from any source | Unique on (source, external_id). Market, normalized address, `zip5`, price, status, property type, lot size, living area, year built, listed date, nullable `remarks`, `first_seen_at`, `last_seen_at`, nullable `account_id` (matching is phase 2), `raw` jsonb (scrubbed). |
| `api_cache` | Provider response cache | Primary key (provider, request_key), where the key is the sha256 of the canonical endpoint and params. `params` holds query params only. `body` is the scrubbed response. `fetched_at`, `expires_at`. |
| `api_budget` | Monthly counter | Primary key (provider, period_start). `request_limit`, `used` (check: not negative). |
| `api_request_log` | One row per attempt | Provider, endpoint, request key, period start, outcome (`ok`, `not_found`, `http_error`, `network_error`, `schema_error`, `refused_budget`, `cache_hit`, `stale_served`), status code, `billed`, `at`. |
| `job` | The queue | `kind`, `payload` jsonb, `status` (`queued`, `running`, `done`, `failed`, `dead`), `priority`, `run_after`, `attempts`, `max_attempts`, `locked_by`, `locked_until`, `last_error`, `dedupe_key` (partial unique index while queued or running), timestamps. |

No owner, mailing-address or agent-contact column exists anywhere. The JSON columns (`listing.raw`, `api_cache.body`) store only fields the RentCast models declare; undeclared fields are dropped and only their names are logged.

Later phases add their own tables in their own migrations: candidates and scores (2), pro-formas (3), signals and risk narratives (4), briefs and deliveries (5).

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

Job kinds: `cad.import` and `listings.sync`.

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

**Budget math.** About 30 listing syncs a month leave about 20 value estimates. Later phases must spend estimates only on top-ranked candidates. A real deployment needs a paid tier or the client's MLS feed, which is what the stub is for.

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
- `scripts/generate_snapshot.py` uses a fixed seed and builds the JSON by instantiating those models, so hand-edited JSON cannot drift silently. It writes 60 synthetic parcels in DCAD layout (quoting, padding, CRLF), the values-free current set, and the recorded responses.
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
| `[buy_box]` | zips, max price, minimum lot size, maximum year built, minimum land-to-total ratio, property types. Validated now, applied in phase 2. |
| `[cost_assumptions]` | `status` (`placeholder` or `reviewed`) and optional decimal inputs for the pro-forma. The Dallas values are placeholders. |

`feasibility market validate` checks every pack, and a parametrized test runs over `packs/*.toml`, so a new county is a new file.

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

All ports bind to 127.0.0.1. Ports 4500 and 4503 stay free for the report viewer and n8n. A fresh clone needs no `.env`: compose falls back to local-only development defaults, mock mode and a localhost-bound database password.

**Container hardening.** Base images are pinned by index digest, with the tag kept in a comment beside it: `python:3.12-slim` and `ghcr.io/astral-sh/uv:0.12.10` in the Dockerfile, `postgres:16-alpine` in compose and in the CI service container. Never use `:latest`. To refresh a digest, request the manifest from the registry and read the `Docker-Content-Digest` response header (for example `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' <registry manifest URL for the tag>`), then update every place the image appears.

`migrate`, `api` and `worker` run with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, a 256-process limit and a 64 MB `noexec` tmpfs at `/tmp` (the seed writes temporary CSV archives there). `db` is read-only too, with tmpfs at `/tmp` and `/run/postgresql`, the data volume writable, and only the five capabilities its entrypoint needs to chown the data directory and drop to the postgres user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`).

CI (`.github/workflows/ci.yml`) runs four jobs:

- lint: ruff check, ruff format check, mypy strict
- test: pytest against a Postgres 16 service container on a separate test database
- gitleaks: scans the full history
- compose smoke: `docker compose up -d --wait --build`, then `curl` on `/health` and `/parcels?limit=5`

Pre-commit runs gitleaks and ruff.

## Known gaps and unverified points

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
- **The API has no authentication, rate limiting or IP banning yet.** It is read-only, binds to 127.0.0.1 and serves synthetic data in mock mode. Phase 5's write endpoint brings authentication with it; anything exposed beyond localhost needs these controls first.
- **Retention duties for flagged accounts are unconfirmed.** If `EXCLUDE_OWNER` marks a confidential address (Texas Tax Code §25.025), what applies to copies already held is a question for counsel. The importer deletes them on the next load that flags them.

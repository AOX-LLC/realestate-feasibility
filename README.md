# Real Estate Feasibility Engine

A daily acquisition brief for a Dallas spec builder. Each morning it lists new teardowns and lots that fit a buy box. Each candidate gets a pro-forma computed in code and a short LLM risk narrative.

The thesis: **LLM for judgment, code for math.** The model reads listing text and writes the risk narrative. Every number in the pro-forma comes from deterministic, tested code.

## Status

Phases 1 (foundation) and 2 (sourcing and scoring) exist today.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Schema, job queue, source adapters (county appraisal CSV, RentCast, MLS stub), mock and live modes, synthetic snapshot, market packs, read-only API, Docker Compose | Built |
| 2 | Sourcing and scoring: apply the buy box, match listings to parcels, diff each day's feed, score and rank candidates, read-only API | Built |
| 3 | Pro-forma engine | Not started |
| 4 | LLM layer: listing-text signals and risk narratives | Not started |
| 5 | Delivery: the morning brief, scheduling | Not started |
| 6 | Evals | Not started |

## Quick start

Needs Docker with Compose. A fresh clone needs no `.env`; mock mode is the default and needs no API key and no network.

```bash
docker compose up -d --wait

curl -s http://127.0.0.1:4501/health
curl -s "http://127.0.0.1:4501/parcels?limit=5"
curl -s "http://127.0.0.1:4501/listings?limit=5"
curl -s http://127.0.0.1:4501/markets/dallas
```

Enqueue a listings sync and watch the worker pick it up:

```bash
docker compose exec worker feasibility enqueue listings.sync --payload '{"market": "dallas"}'
curl -s http://127.0.0.1:4501/jobs
```

Stop and delete the data:

```bash
docker compose down -v
```

The stack has four services: `db` (Postgres 16), `migrate` (one-shot: runs the migrations, then loads the synthetic snapshot), `api` and `worker`. All ports bind to 127.0.0.1 only.

| Port | Use |
| --- | --- |
| 4500 | Reserved for the report viewer (later phase) |
| 4501 | API |
| 4502 | Postgres |
| 4503 | Reserved for n8n (later phase) |

The API is read-only. List endpoints page with a `limit` (1 to 100, default 50) and an `after` cursor. Other routes: `/parcels/{market}/{account_id}`, `/listings/{id}`, `/markets`, `/jobs`, `/budget`, and the sourcing views below.

## The data

Everything committed to this repository is synthetic.

- `data/snapshot/` holds 70 synthetic parcels in the Dallas Central Appraisal District (DCAD) CSV layout (quoted fields, padded values, CRLF line endings), a values-free "current ownership" set, and recorded RentCast-shaped responses for two days (`days.json` lists them).
- `scripts/generate_snapshot.py` produces all of it from a fixed seed. The RentCast responses are built by instantiating the response models, so they cannot drift from the schema the code validates against.
- Account numbers start with `99`. Street names are invented. Zip codes are real Dallas zips. No record names a person.

Real DCAD files and real RentCast responses are fetched by whoever runs the software. They are never committed: `local/` and `*.zip` are gitignored. DCAD publishes no redistribution license that we found.

**Personal data is excluded by construction.**

- The CAD importer can only read columns the market pack maps. A pack cannot map owner, contact, legal-description or taxpayer columns; validation rejects it.
- Accounts flagged `EXCLUDE_OWNER` are skipped whole, and an account flagged after an earlier load has its stored rows deleted in the same import.
- RentCast agent, office and owner objects are removed before anything is validated. Only fields the response models declare are cached or stored; the names of any other fields are logged as drift, never their values.
- No table has a dedicated column for owner or contact data. The JSON columns (`listing.raw`, `api_cache.body`) hold only declared fields.

**Listing text.** `domain.Listing` has a nullable `remarks` field. RentCast listings carry no description text, so in live RentCast mode the LLM layer gets signals from structured fields only. Phase 4 adds a small synthetic RESO-shaped listing set with `PublicRemarks` for mock mode. A client's own MLS feed (the RESO stub in `src/feasibility/sources/mls/stub.py`) is where real remarks would come from.

## Mock and live modes

| | `DATA_MODE=mock` (default) | `DATA_MODE=live` |
| --- | --- | --- |
| RentCast key | Not needed; ignored if set | `RENTCAST_API_KEY` required; the app refuses to start without it |
| Network | None | Calls `api.rentcast.io` |
| RentCast data | Recorded responses in `data/snapshot/rentcast/` | Real responses |
| Budget | Never spent | Hard monthly stop |

There is one client, one scrub path and one validation path. Only the transport differs.

**RentCast budget.** The free developer tier is 50 requests a month. `RENTCAST_MONTHLY_BUDGET` is a hard stop, and `RENTCAST_BILLING_ANCHOR_DAY` (1 to 28) aligns the period with your billing date. One unit is reserved before each call:

| Outcome | Budget unit | Result |
| --- | --- | --- |
| 200, valid | Kept | Cached, returned |
| 200, schema drift | Kept | Not cached; `SchemaDriftError` |
| 404 | Refunded | Empty answer, not an error |
| Other HTTP error, connect failure | Refunded | Stale cache entry if any, else `RentCastError` |
| Read timeout | Kept (outcome unknown) | Stale cache entry if any, else `RentCastError` |
| Budget exhausted | Refused before the network | Stale cache entry if any, else `BudgetExhaustedError` |

The refund rules follow RentCast's documented rule that error responses are not billed. Whether that covers 404 and 429 is not yet confirmed; see [Known gaps](docs/ARCHITECTURE.md#known-gaps-and-unverified-points).

Budget math: about 30 listing syncs a month leave about 20 value estimates. Later phases spend estimates only on top-ranked candidates. A real deployment needs a paid tier or the client's MLS feed.

Live mode has not been exercised against the real API yet; it was built from RentCast's published OpenAPI definition. `feasibility verify-rentcast` (at most 4 calls) checks it. It needs `DATA_MODE=live` and a key, and writes `local/rentcast-verify-<timestamp>.json` with field names and types only.

## Sourcing: the daily candidate list

A sourcing run takes one day's listing feed and turns it into a ranked list: it syncs listings, compares them with the previous run (new, relisted, price changed, unchanged, gone, aged out), applies the buy box, matches listings to county parcels, scores each match and ranks the result. How it works is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#sourcing).

In mock mode the snapshot holds two days, `2026-10-01` and `2026-10-02`, and the date must be given:

```bash
docker compose run --rm migrate feasibility source run --as-of 2026-10-01
docker compose run --rm migrate feasibility source run --as-of 2026-10-02
docker compose run --rm migrate feasibility source show --status unscored
```

- `feasibility source run [--market dallas] [--as-of YYYY-MM-DD] [--enqueue]` runs inline and prints the counts and the top 10; `--enqueue` queues a `sourcing.run` job for the worker instead. Live mode sources for today only. A run for an earlier date than any run already started is refused (exit code 2); running the same date again rewrites that run only.
- `feasibility source show [--run-id N] [--status ranked|filtered|unscored] [--limit 20]` prints a stored run.
- A run costs one RentCast call in live mode (zero when the response is cached) and none in mock mode.

Read-only API:

| Endpoint | Returns |
| --- | --- |
| `GET /sourcing/runs` | Runs, newest first, with their counts |
| `GET /sourcing/runs/{run_id}` | One run |
| `GET /sourcing/runs/{run_id}/candidates?status=ranked\|filtered\|unscored` | A run's candidates (ranked by rank; the others by candidate id) |
| `GET /sourcing/runs/{run_id}/candidates/{candidate_id}` | One candidate with its score breakdown and the listings the run saw for it |

## Importing real DCAD data

1. Download the certified zip (`DCAD{YYYY}_CURRENT.ZIP`, about 200 MB) from `dallascad.org/dataproducts.aspx` into `local/`.
2. Import it:

```bash
uv run feasibility import-cad local/DCAD2026_CURRENT.ZIP --kind certified
# optional: the Most Current Ownership file (adds parcels and attributes, never values),
# saved under its own name so it does not overwrite the certified zip
uv run feasibility import-cad local/DCAD2026_OWNERSHIP.ZIP --kind current --roll-year 2026
```

The import is idempotent by sha256. The same file again is a no-op. A re-download after DCAD issues supplements has a new sha and a new member date, so it loads as a new version. Options: `--force` replaces a load whose contents changed under the same key, `--file-date YYYY-MM-DD` overrides the archive member's date, `--roll-year` overrides the year parsed from the file name.

Under Compose, `./local` is bind-mounted at `/app/local`: writable in `migrate` (the roots stay read-only) and read-only in `worker`, which only reads archives from it. Create it first and make it writable by the container user, `mkdir -p local && chown 10001 local` (or `chmod 770` with matching group ownership), otherwise Docker creates it root-owned. Then `docker compose run --rm migrate feasibility verify-rentcast` and `import-cad /app/local/<file>` work; `verify-rentcast` without a key exits 2 with a message before touching the filesystem.

The same import runs as a job: enqueue `cad.import` with the payload `{"market": "dallas", "archive": "<file name inside local/>", "kind": "certified"}`.

DCAD publishes no update schedule, and there is no downloader yet. The importer streams the files, so memory stays bounded; a slow test imports a roughly 100 MB archive under 50 MB peak.

## CLI

Run `uv run feasibility --help` (or `docker compose exec worker feasibility --help`).

| Command | What it does |
| --- | --- |
| `migrate` | Upgrade the database schema to the latest revision |
| `seed` | Load the committed synthetic snapshot (idempotent, no network) |
| `serve` | Run the read-only API (default `127.0.0.1:4501`) |
| `worker` | Run the job worker; `--once` runs at most one job |
| `enqueue <kind> --payload <json>` | Queue a job; `--dedupe-key` skips it if one is active |
| `import-cad <archive>` | Import a county appraisal archive (`--kind`, `--roll-year`, `--file-date`, `--force`, `--market`) |
| `market validate [files]` | Validate market pack files (all packs by default) |
| `verify-rentcast` | Check the live RentCast API against the models (at most 4 calls) |

Job kinds in phase 1: `cad.import` and `listings.sync`.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

- **Tests need Postgres.** Set `TEST_DATABASE_URL` (default `postgresql+psycopg://feasibility:feasibility@127.0.0.1:4502/feasibility_test`). The test run drops and recreates the `public` schema of that database, so point it at a throwaway database.
- `uv run pytest -m slow` runs the memory test.
- `uv run python scripts/generate_snapshot.py` regenerates the snapshot. CI requires the committed files to match the output byte for byte.
- `uv run python scripts/check_rentcast_docs.py` diffs the models against RentCast's published docs. It needs the network but no key.
- Pre-commit runs ruff and gitleaks: `uvx pre-commit install`. The gitleaks hook runs in a container that sees only the working directory, so in a git worktree it scans nothing; run `gitleaks git` against the main checkout instead.

Design notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Adding a county

Add `src/feasibility/markets/packs/<id>.toml` and run `feasibility market validate`. A pack maps the county's files and columns, unit factors and aggregation rules, and sets the listing sources, buy box and cost assumptions. No code changes.

## License

MIT, © 2026 AOX LLC. Built by [AOX](https://automatedoperationsexperts.com).

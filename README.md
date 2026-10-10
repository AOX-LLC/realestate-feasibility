# Real Estate Feasibility Engine

A daily acquisition brief for a Dallas spec builder. Each morning it lists new teardowns and lots that fit a buy box. Each candidate gets a pro-forma computed in code and a short LLM risk narrative.

The thesis: **LLM for judgment, code for math.** The model reads listing text and writes the risk narrative. Every number in the pro-forma comes from deterministic, tested code.

## Status

Phases 1 (foundation), 2 (sourcing and scoring), 3 (the pro-forma), 4 (the LLM layer) and 5 (delivery) exist today. Delivery has run only against mock services; nothing in this repository has sent a real message to Notion or Slack.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Schema, job queue, source adapters (county appraisal CSV, RentCast, MLS stub), mock and live modes, synthetic snapshot, market packs, read-only API, Docker Compose | Built |
| 2 | Sourcing and scoring: apply the buy box, match listings to parcels, diff each day's feed, score and rank candidates, read-only API | Built |
| 3 | Pro-forma: value estimates for the top candidates, a code-only pro-forma for every ranked one (sizing, ARV from sale comps, costs, financing, holding, selling, maximum offer, sensitivity grid), read-only API and CLI | Built |
| 4 | LLM layer: listing-text signals and risk narratives, recorded model responses, eval scorecards | Built: both run in the daily run and replay committed recordings in mock mode with no key; read-only API and CLI; two evals with committed scorecards, which miss two of their targets (see [The LLM layer](#the-llm-layer)) |
| 5 | Delivery: the morning brief, scheduling | Built in mock mode: the API auth gate and morning trigger, the brief, the PDF pro-forma, the Notion and Slack clients, a delivery ledger that sends each item once, and an opt-in n8n schedule; a morning run goes from the trigger to mock Notion rows, a mock Slack digest and PDFs (see [Delivery and the morning run](#delivery-and-the-morning-run)). The live Notion and Slack calls are unverified |
| 6 | Evals, retention and the proof kit | Started: the two model evals exist (Phase 4); retention is built (see [Retention](#retention)); the eval report and the proof kit are to come |

## Quick start

Needs Docker with Compose. A fresh clone needs no `.env` and mock mode needs no model key and no network. **Every API route except `/livez` needs a bearer token**, so make two (any 32 or more characters of `A-Za-z0-9._~+/=-`; they must differ) and put them in `.env`:

```bash
printf 'API_READ_TOKEN=%s\nAPI_TRIGGER_TOKEN=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" >> .env
docker compose up -d --wait
set -a; . ./.env; set +a
R="Authorization: Bearer $API_READ_TOKEN"

curl -s http://127.0.0.1:4501/livez                          # open: {"status":"ok"}
curl -s -H "$R" http://127.0.0.1:4501/health
curl -s -H "$R" "http://127.0.0.1:4501/parcels?limit=5"
curl -s -H "$R" "http://127.0.0.1:4501/listings?limit=5"
curl -s -H "$R" http://127.0.0.1:4501/markets/dallas
```

Start the morning run for a day and watch the worker do it (the trigger only queues a job):

```bash
T="Authorization: Bearer $API_TRIGGER_TOKEN"
curl -s -X POST -H "$T" -H 'content-type: application/json' \
  -d '{"market":"dallas","as_of":"2026-10-01"}' http://127.0.0.1:4501/triggers/morning
curl -s -H "$R" http://127.0.0.1:4501/jobs
docker compose run --rm migrate feasibility brief show
```

Or enqueue a listings sync from the command line:

```bash
docker compose exec worker feasibility enqueue listings.sync --payload '{"market": "dallas"}'
```

Stop and delete the data:

```bash
docker compose down -v
```

The stack has four services: `db` (Postgres 16), `migrate` (one-shot: runs the migrations, then loads the synthetic snapshot), `api` and `worker`. A fifth, `n8n`, is an opt-in profile that a plain `docker compose up` does not start (see [Delivery and the morning run](#delivery-and-the-morning-run)). All ports bind to 127.0.0.1 only.

| Port | Use |
| --- | --- |
| 4500 | Reserved; the report viewer was dropped (the brief's PDFs travel as files) |
| 4501 | API |
| 4502 | Postgres |
| 4503 | n8n, opt-in (`--profile schedule`) |

Reads are `GET`; the only write is a trigger that queues a job. List endpoints page with a `limit` (1 to 100, default 50) and an `after` cursor. Other routes: `/parcels/{market}/{account_id}`, `/listings/{id}`, `/markets`, `/jobs`, `/budget`, and the sourcing views below.

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
- Listing remarks are redacted at ingestion, and the RESO agent, office, private-remarks and showing fields are never read. Redaction is pattern-based, so a bare name with no cue word can pass; the extraction eval measures that residual. See `docs/ARCHITECTURE.md`, "Listing remarks: redaction and screening".
- No table has a dedicated column for owner or contact data. The JSON columns `listing.raw` and `api_cache.body` hold only declared fields; `candidate_estimate.comps` and `proforma.result` hold comparable sales (address, price, size), and the API leaves the addresses out.

**Listing text.** `domain.Listing` has a nullable `remarks` field. RentCast listings carry no description text, so in live RentCast mode the LLM layer gets signals from structured fields only. Mock mode reads a small synthetic RESO-shaped listing set (`data/mls/dallas.json`, with `PublicRemarks`) and attaches each record's redacted remarks to the snapshot listing with the same MLS number. A client's own MLS feed (the RESO stub in `src/feasibility/sources/mls/stub.py`) is where real remarks would come from.

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

Budget math: about 30 listing syncs a month leave about 20 value estimates. A run prices only its top 5 ranked candidates, reuses an estimate for 7 days, stops at 20 billed estimates a month and holds back one unit for each remaining day's listing sync. A real deployment needs a paid tier or the client's MLS feed.

Live mode has not been exercised against the real API yet; it was built from RentCast's published OpenAPI definition. `feasibility verify-rentcast` (at most 4 calls) checks it. It needs `DATA_MODE=live` and a key, and writes `local/rentcast-verify-<timestamp>.json` with field names and types only.

## Sourcing: the daily candidate list

A sourcing run takes one day's listing feed and turns it into a ranked list: it syncs listings, compares them with the previous run (new, relisted, price changed, unchanged, gone, aged out), applies the buy box, matches listings to county parcels, scores each match and ranks the result. How it works is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#sourcing).

In mock mode the snapshot holds two days, `2026-10-01` and `2026-10-02`, and the date must be given:

```bash
docker compose run --rm migrate feasibility source run --as-of 2026-10-01
docker compose run --rm migrate feasibility source run --as-of 2026-10-02
docker compose run --rm migrate feasibility source show --status unscored
```

- `feasibility source run [--market dallas] [--as-of YYYY-MM-DD] [--enqueue]` runs inline and prints the counts (including how many value estimates were called, reused and deferred) and the top 10; `--enqueue` queues a `sourcing.run` job for the worker instead. Live mode sources for today only. A run for an earlier date than any run already started is refused (exit code 2); running the same date again rewrites that run only.
- `feasibility source show [--run-id N] [--status ranked|filtered|unscored] [--limit 20]` prints a stored run.
- In live mode a run costs one RentCast call for the listing sync (zero when the response is cached) plus up to five value estimates for the top candidates (see the budget math above); in mock mode it costs none. After ranking and pricing it computes a [pro-forma](#pro-forma) for every ranked candidate.

Read-only API:

| Endpoint | Returns |
| --- | --- |
| `GET /sourcing/runs` | Runs, most recently created first, with their counts |
| `GET /sourcing/runs/{run_id}` | One run |
| `GET /sourcing/runs/{run_id}/candidates?status=ranked\|filtered\|unscored` | A run's candidates (ranked by rank; the others by candidate id) |
| `GET /sourcing/runs/{run_id}/candidates/{candidate_id}` | One candidate with its score breakdown, the listings the run saw for it and its newest value estimate (price, range and comp counts) bought on or before the run's date, or `null` |

## Pro-forma

Every ranked candidate gets a pro-forma, computed in code from its parcel, its price in that run and the value estimate bought for it. Every figure is `Decimal` arithmetic checked against a reference spreadsheet (`docs/proforma-reference.xlsx`). **Every Dallas cost value is illustrative**: a labelled default, not a quote or a builder's actuals (sources in [docs/proforma-assumptions.md](docs/proforma-assumptions.md)). How it works is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#pro-forma).

A pro-forma has one of three statuses:

- `computed`: ARV, total cost, profit, margin (profit over ARV), ROI, annualized return, the most you can pay and still earn the 15% target, and a 60-cell sensitivity grid (ARV, hard cost, hold months).
- `no_arv`: there is no usable value estimate (`no_estimate_yet` outside the top 5, `estimate_unavailable`, `estimate_expired`, `too_few_comps`, `arv_not_positive`), so only the costs that need no ARV are shown. Nothing stands in for a missing ARV.
- `unsizable`: no lot size, so there is nothing to build on.

```bash
docker compose run --rm migrate feasibility proforma list --status computed
docker compose run --rm migrate feasibility proforma show 4 --sensitivity
```

- `feasibility proforma list [--market dallas] [--run-id N] [--status computed|no_arv|unsizable] [--limit 20]` prints a run's pro-formas in rank order (the latest completed run by default).
- `feasibility proforma show CANDIDATE_ID [--market] [--run-id N] [--sensitivity]` prints one pro-forma section by section; `--sensitivity` adds the grid as three 5x4 blocks, one per hold. Both are read-only and exit with code 2 and a message when the run or candidate does not exist.

| Endpoint | Returns |
| --- | --- |
| `GET /sourcing/runs/{run_id}/proformas?status=computed\|no_arv\|unsizable` | A run's pro-formas in rank order: candidate, rank, address, status, reason, flags, offer price, ARV, total cost, profit, margin, ROI, annualized return and maximum offer. Money and ratios are decimal strings (0.1964 is 19.64%) |
| `GET /sourcing/runs/{run_id}/candidates/{candidate_id}/proforma` | One pro-forma with its full result: every assumption, input and line (the comparable sales' addresses stay in the database) |

On the snapshot, day 1 computes five pro-formas (two clear the 15% target, two are marginal, one loses money) and day 2 six. A run costs the same RentCast calls as before: the pro-formas add none. In live mode a day is at most one listing sync plus up to five value estimates; in mock mode the estimates come from the snapshot and no budget is touched.

## The LLM layer

After the pro-formas, a run reads each ranked candidate's signals (stage 6) and writes a risk narrative for each one whose pro-forma was computed (stage 7). **The model never produces a number.** A signal survives only if code finds its quote verbatim in the listing's remarks; a narrative survives only if every figure in it is a string code gave the model, copied exactly (a rejected narrative keeps its violations and none of its text). The model sees no address and no listing text when it writes a narrative.

**The twelve remarks signals** are a closed set. Risks: `as_is_sale`, `environmental_hazard`, `flood_or_drainage`, `easement_or_encroachment`, `deed_restrictions`, `conservation_or_historic_district`, `protected_trees`, `tenant_occupied`. Opportunities: `teardown_language`, `plans_or_permits`, `seller_financing`, `multiple_lots`. Negations ("no HOA", "not in a flood zone") are not signals. Three more signals (`price_reduced`, `relisted`, `long_on_market`) are computed in code from the listing's fields and need no model.

**The figure rule.** A narrative may contain no digit except inside a figure copied exactly from the facts sheet code builds from the stored pro-forma (money to the cent, ratios as percents with two decimals). Comparisons ("below the target", "loses money") are code facts the model is handed, not judgments it makes. Spelled-out quantities, arithmetic words next to a figure and a basis code that is not in the facts are rejected; one repair call is allowed.

**Data and listing text.** Remarks come from a synthetic RESO-shaped set (`data/mls/dallas.json`), redacted for personal data at ingestion; live RentCast listings have no remarks. So in live data mode the model is called for narratives only.

| | Mock data (default) | Live data |
| --- | --- | --- |
| `AGENT_CORE_MODE` | `replay` (default): serves the committed recordings, no key, no network, no cost. `record` calls the model and writes recordings | `live`: calls the model (needs `AGENT_CORE_ANTHROPIC_API_KEY`). With no key, narratives are `deferred` and signals are the three field signals |
| Cost | Replay reports the cost of the call that was recorded; nothing is spent | Narratives only: about $0.015 a call, so about $0.08 for a first day of five computed candidates and about $0.03 for a day with two changed ones (a projection; see `evals/scorecards/cost.md`) |

Results are cached by a hash of their inputs, so a same-day re-run, or a day whose inputs did not change, makes no call. Every call has one row in the ledger (`llm_call`), and a run's spend is capped (`LLM_RUN_BUDGET_USD`, per run across all its attempts) as is a UTC month of billable calls (`LLM_MONTHLY_BUDGET_USD`). A candidate the cap cannot afford is stored as `deferred`, not as an error. The recordings are tied to the prompts, the inputs and the pinned `anthropic` and `pydantic` versions; changing any of them means a paid re-recording (see [ARCHITECTURE](docs/ARCHITECTURE.md#recordings-and-how-to-record-again)).

On the snapshot, replayed from the committed recordings: day 1 stores 12 signals rows (10 extracted, 2 fields-only) and 12 narrative rows (4 accepted, 1 rejected by the figure check, 7 not eligible); day 2 stores 17 and 17 (5 accepted, 1 rejected, 11 not eligible) with 8 calls; running day 2 again makes none.

```bash
docker compose run --rm migrate feasibility source run --as-of 2026-10-01
docker compose run --rm migrate feasibility llm cost             # the run's model calls, mode replay
docker compose run --rm migrate feasibility llm show 4           # one candidate's signals and narrative
docker compose run --rm migrate feasibility llm cost --month 2026-10
```

- `feasibility llm show CANDIDATE_ID [--market] [--run-id N]` prints one candidate's signals (with their quotes) and narrative. An accepted narrative is printed as written; a rejected one as its reason and the kinds of rule it broke.
- `feasibility llm cost [--market] [--run-id N] [--month YYYY-MM]` prints a run's calls, tokens and cost by stage and by model, and the money held for calls that raised (their real cost is unknown). With `--month` it prints that UTC month's billable spend against the monthly budget instead. Both commands are read-only and exit with code 2 and a message when the run or candidate does not exist.

| Endpoint | Returns |
| --- | --- |
| `GET /sourcing/runs/{run_id}/candidates/{candidate_id}/llm` | The candidate's signals and narrative together (either is `null` when its stage wrote no row) |
| `GET /sourcing/runs/{run_id}/narratives?status=accepted\|rejected\|failed\|deferred\|not_eligible` | A run's narratives in rank order: status, reason and, for an accepted one, the summary |
| `GET /sourcing/runs/{run_id}/llm/cost` | The run's calls, tokens, cost by stage and by model, the money reserved for calls whose cost is unknown, and the run budget |
| `GET /llm/spend?month=YYYY-MM` | Billable calls and cost in a UTC month against the monthly budget (the current month by default) |

A rejected narrative is served as its status, its reason and the kinds of rule it broke: never the model's text and never the text of a violation. The API never calls a model.

### Evals and scorecards

```bash
uv run feasibility eval signals --split all      # replay: no key, no database
uv run feasibility eval narrative
cat evals/scorecards/signals-holdout.md
```

`feasibility eval signals [--split dev|holdout|all]` scores extraction on 56 synthetic records against a committed answer key (per-signal precision and recall, evidence match, injection resistance, personal-data leaks); `feasibility eval narrative` scores 13 facts sheets. Both replay the recordings by default and need `--allow-spend` in record or live mode. The scorecards are in [evals/scorecards](evals/scorecards/README.md), and a test regenerates them from the recordings. What they show, from a recording session that cost about $0.53 and a second, narrative-only one that cost about $0.13:

- Extraction, holdout (25 records): micro precision 90.2% and recall 100.0%, evidence match 100%, no personal data leaked, all 3 injection cases resisted. Precision sits on its 0.90 target.
- Extraction, dev: micro precision 87.5%; one of its 6 injection cases fails the "signal set equals the key" check (an extra signal; no injected text reached an output).
- Narrative: **acceptance is 13 of 13 cases (100%), all on the first attempt**, with every accepted narrative's figures matching the facts sheet exactly and both injection cases resisted. It was 8 of 9 scored (88.9%, under the 0.90 target) with 4 of 13 cases cut off at the 1,500-token limit before the mid tier was set to low effort (hidden thinking was using the output tokens) and the narrative eval was recorded again. The new replies are about 8% shorter; tone and quality are not scored.
- The answer key, prompts, catalogue and cases were not changed after seeing these numbers; the one change was the mid tier's effort setting, which needed the narrative recording again. Changing a prompt or a token limit is a decision that needs a new recording.

The eval set is small, synthetic and written by this project, so these numbers say nothing about real listings.

## API access and the morning trigger

Two static bearer tokens guard the API, and there is no setting that turns the guard off: with a token unset, every route but `/livez` answers 401.

| | Opens | Held by |
| --- | --- | --- |
| `API_READ_TOKEN` | every `GET` (and `HEAD`) | anyone who reads |
| `API_TRIGGER_TOKEN` | only `POST /triggers/*` | the scheduler, which can read nothing |

- `/livez` is the one open route: `{"status": "ok"}` or 503, from a `SELECT 1`, and nothing else. `/health` (revision, mode) is behind the read token. The interactive docs and the schema route are not served.
- The wrong scope is a 403; no or a malformed header is a 401 with `WWW-Authenticate: Bearer`; an unknown path without a token is a 401 as well, so the response does not say what exists.
- Limits, in the API process: 120 reads a minute on the read token, 12 triggers an hour, 60 `/livez` a minute per address, and 20 failed authentications in 10 minutes ban an address for 15 minutes (a valid token does not lift it). The address is the socket peer; behind a tunnel or proxy set `API_CLIENT_IP_HEADER` to the header that carries it (for example `CF-Connecting-IP`), because a header anyone can send must not decide who is banned.
- `POST /triggers/morning` takes `{"market": "dallas", "as_of": "2026-10-01"}` (`as_of` may be left out in live mode; mock mode needs a date the snapshot holds). It checks the market and the date, queues one `morning.run` job and answers 202 with its id; the same market and date again, while the first is queued or running, answers 200 and queues nothing; a date earlier than a run already started is a 409. The API never syncs, spends or calls a model.

`morning.run` runs the day (`source run`) and then queues `brief.deliver {run_id}`. If the run ranked but a model stage could not finish (a missing recording), the brief is still queued, as `partial`, and the `morning.run` job still ends `dead` so the failure stays visible.

## The brief

The brief is what a run delivers, built by code from its stored rows: the computed pro-formas in rank order (at most ten), each with its figures as exact decimal strings, a verdict in code facts, the comps as a count, a median $/sq ft and the lowest and highest sale price (never an address), flags with a written meaning, the signals that held (code, polarity, source and the meaning written in this repository; **never a quote**) and the narrative. A narrative is in the brief only if it was accepted, still passes the figure check against facts rebuilt now, is plain prose (no link, mention, markup, street name, injection phrasing or copy of the listing's own words) and was not written from listing text flagged as an injection; otherwise the brief says "Withheld" or "Not available today." and carries none of the model's text. A run whose later stage failed is delivered as `partial` with a notice, never with its error. The rest of the ranked list is counted (`not_shown`: no value estimate yet, unsizable, over the cap of ten, no pro-forma).

```bash
docker compose run --rm migrate feasibility brief build          # make or rebuild the latest run's brief
docker compose run --rm migrate feasibility brief show
curl -s -H "$R" http://127.0.0.1:4501/sourcing/runs/1/brief | jq '.brief.shown'
```

The brief is stored per run with a content hash (table `brief`), so a same-day re-run that changes nothing builds the same bytes. Nothing is sent anywhere by building it.

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

### The PDF pro-forma, Notion and Slack

Each candidate of a brief has a one-to-two page PDF: the verdict, the key figures, the cost stack, the sizing, the sensitivity grid, the signals and the narrative (only if it is in the brief), and the checks and assumptions. It is drawn from the stored brief and the stored pro-forma by code; no model text reaches it except the accepted narrative, and no address of a comparable sale does. Every page says that the cost values are illustrative, not a builder's actuals. Fonts are self-hosted (Space Grotesk, IBM Plex Sans, IBM Plex Mono, all SIL OFL 1.1, licences beside the files), and the renderer can fetch nothing from the network or from outside the package.

```bash
docker compose run --rm migrate feasibility brief pdf --all --out /media    # one PDF per candidate
uv run feasibility brief preview slack                                       # the digest as JSON, sends nothing
uv run feasibility brief preview notion --rank 1                             # one row's properties
uv run feasibility brief notion-check                                        # which properties the database lacks
uv run feasibility brief notion-setup                                        # add the missing ones (never removes any)
```

`DELIVERY_MODE=mock` (the default) builds every payload and sends it to a recorded fake, with no token. `live` needs `NOTION_TOKEN` and `NOTION_DATABASE_ID` for Notion and `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` for Slack, and the settings refuse to load when one of them is missing, or when a token is set for a service that is not in `DELIVERY_TARGETS`. In mock mode a token that is set is ignored and dropped. Notion gets one row per candidate, found by a stable key and updated on later runs; the `Decision` column is yours and is never written. Slack gets at most one digest per run, with each PDF in its thread. Both are paced, retry only what is safe to retry, and report a failure as a short code, never with a response body or a token. `feasibility brief smoke notion|slack` sends one fixed synthetic row or message, and only in live mode.

`MEDIA_OUT` is a folder outside the repository where `brief pdf` writes by default and where samples and payload dumps belong. Nothing generated is committed.

## Delivery and the morning run

`feasibility brief deliver` (and the `brief.deliver` job that `morning.run` queues) sends a run's brief to Notion and Slack. It builds the brief from the database at that moment, never from the stored copy, renders the PDFs, writes the Notion rows, posts the Slack digest and puts each PDF in the digest's thread.

```bash
docker compose run --rm migrate feasibility brief deliver --dry-run     # print every payload, write the PDFs, send nothing
docker compose run --rm migrate feasibility brief deliver               # send (mock unless DELIVERY_MODE=live), once
docker compose run --rm migrate feasibility brief deliveries            # the ledger of the latest run
curl -s -H "$R" http://127.0.0.1:4501/sourcing/runs/1/deliveries | jq '.items[] | {target, item, status}'
```

**Each item is sent once.** Every call is bracketed by a row in the `delivery` ledger: `sending` is committed before the call and the outcome (`sent` with the page, message or file id; `failed` or `unknown` with a short code) after it, with no transaction open during the call. A repeat sends only what the ledger says is not there, so running it twice, or a retried job, sends nothing twice. A `sending` row older than ten minutes reads as `unknown`. A lock keeps two deliveries of one run apart (a second one stops with "another delivery of it is going").

- **Notion is at-least-once.** A candidate's row is updated at the page it was last written to, or found by its key, or created. A row whose content is already there (the same hash, in any run) is skipped, so the same day again makes no request.
- **Slack is at-most-once.** A post whose outcome is unknown (the reply was lost, or Slack said 5xx) is never repeated by the program, because a duplicate digest is worse than a late one. The job ends `dead` with `DeliveryUnknownOutcomeError`. Look in the channel; what is missing can then be posted with `brief deliver --resend slack`, which forgets only the Slack rows whose outcome is unsettled (unknown, failed or in flight) and sends those again: a digest that is already there is not posted a second time. While a Slack item is unknown the job is dead and does not retry Notion's failed rows; `brief deliver --only notion` sends them. A reply Slack documents as possibly done (`internal_error`, `fatal_error`, not JSON) counts as unknown too.
- **An older day never overwrites a newer row.** If the page already carries a later run's figures (a resend of day 1 after day 2 ran), the row is left alone and reported `superseded`.
- **A failure stops what it must and no more.** Bad credentials, a channel Slack does not know, or a Notion database missing a property stop that target (the other still runs) and end the job `dead`. A row that Notion keeps refusing fails alone, the job is retried, and the retry sends only that row.
- **Errors are codes.** A ledger row, a job's `last_error` and a log line carry `http_429`, `network_error`, `validation_error` or a service's own error code, never a response body, a header, a token or an upload URL.
- **Mock delivery leaves its work where you can see it.** With `MEDIA_OUT_DIR` set to a writable folder (under Compose, a folder the container user, uid 10001, can write), mock delivery writes the PDFs and one line of JSON per request it would have made to `<folder>/<market>-<date>/`. Without it, nothing is written.

### The schedule (n8n, opt-in)

n8n is the clock and nothing else. `n8n/morning-brief.json` has three nodes: a schedule (06:00, America/Chicago), a fixed market, and one POST to the API's `/triggers/morning` route (on the `api` service, port 4501) with a credential called `feasibility trigger`. The credential is a Header Auth credential that you create in n8n (header name `Authorization`, value the word `Bearer`, a space, then your trigger token), which keeps it in its own encrypted store; the file holds no token. n8n holds none of this application's variables, shares a Docker network with the api only (not the database), cannot load the node types that run a command, read a file, run code or listen for a request (a list of the types in n8n 2.42.6), cannot install community packages, and sends no telemetry. It can still reach the internet and the host's other listeners, so treat it as a clock behind a localhost-only editor; whoever opens the editor first becomes its owner, so open it once through the tunnel before anyone else can. The workflow is imported switched off.

```bash
docker compose --profile schedule up -d n8n          # http://127.0.0.1:4503 (use an SSH tunnel from elsewhere)
docker compose --profile schedule exec n8n n8n import:workflow --input=/workflows/morning-brief.json
# in the editor: create the owner, create the Header Auth credential, open the workflow, pick the credential, activate it
docker compose --profile schedule down                # stop it (no -v: that deletes n8n's credentials)
```

In mock mode the trigger needs a date the snapshot holds: set the `as_of` field of the "Markets" node to `2026-10-01` before a test run. Leave it empty in live mode (today in the market's time zone).

### What a live smoke test needs

Nothing here has been run against the real services. To try them you supply: a Notion internal integration (read, update and insert content; no user information) shared with one empty database, its token and the database id; a Slack app with the bot scopes `chat:write` and `files:write`, installed in a test workspace, its `xoxb-` token and a channel id with the app invited. Set `DELIVERY_MODE=live` and those four values for the worker and migrate services, run `feasibility brief notion-setup` and `brief smoke notion` / `brief smoke slack`, then `brief deliver`. The Notion API version is pinned to `2022-06-28`; the 2025 move to data sources is not adopted.

## Retention

Stored data is deleted once it is older than its window: run detail (a run's listings, candidates, pro-formas, signals, narratives, brief and delivery ledger) and value estimates, with the comparable sales' addresses in them and in the cached provider response, after 90 days; the model cache after 30 days; the model-call ledger (spend) after 13 whole months; finished jobs and unreferenced listings after 30 days. The numbers are the `RETENTION_*` settings (`.env.example`), each with a floor. A market's latest run and its latest completed run are never pruned, nor is any spend record inside its months, nor the record of which Notion page a candidate's row is on while the candidate exists. A pruned run keeps its summary row, marked `pruned_at`, and cannot be briefed or delivered again.

```bash
docker compose run --rm migrate feasibility retention prune --dry-run     # what would go; deletes nothing
docker compose run --rm migrate feasibility retention prune               # delete it (also: the weekly n8n node, POST /triggers/retention)
```

Rows already sent to Notion, and messages and PDFs already in Slack, are out of reach and are not deleted. `--as-of DATE` exists to show pruning on the synthetic snapshot; a real prune from a chosen day is refused where live data or real spend exists.

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
| `source run`, `source show` | Source a day and read a stored run (see [Sourcing](#sourcing-the-daily-candidate-list)) |
| `proforma list`, `proforma show` | Read a run's pro-formas (see [Pro-forma](#pro-forma)) |
| `brief build`, `brief show` | Build and read a run's brief (see [The brief](#the-brief)) |
| `retention prune` | Delete data past its retention window (`--dry-run`, `--as-of`; see [Retention](#retention)) |
| `brief deliver`, `brief deliveries` | Send a run's brief once (`--dry-run`, `--resend slack`, `--only`, `--out`) and read the delivery ledger (see [Delivery and the morning run](#delivery-and-the-morning-run)) |
| `brief pdf`, `brief preview`, `brief notion-check`, `brief notion-setup`, `brief smoke` | Render the PDFs, preview the Notion and Slack payloads, check or extend the Notion database, and (live only) smoke-test a credential (see [The PDF pro-forma, Notion and Slack](#the-pdf-pro-forma-notion-and-slack)) |
| `llm show`, `llm cost` | Read a candidate's signals and narrative, and a run's or a month's model cost (see [The LLM layer](#the-llm-layer)) |
| `eval signals`, `eval narrative` | Score the model tasks against their answer keys, in replay by default (see [Evals and scorecards](#evals-and-scorecards)) |
| `verify-rentcast` | Check the live RentCast API against the models (at most 4 calls) |

Job kinds: `cad.import`, `listings.sync`, `sourcing.run`, `morning.run`, `brief.deliver` and `retention.prune`.

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

Add `src/feasibility/markets/packs/<id>.toml` and run `feasibility market validate`. A pack maps the county's files and columns, unit factors and aggregation rules, and sets the listing sources, buy box and cost assumptions. No code changes. Every cost value in the Dallas pack is illustrative (a labelled default, not a quote or a builder's actuals) until it is reviewed.

## License

MIT, © 2026 AOX LLC. Built by [AOX](https://automatedoperationsexperts.com).

The data sources (DCAD's file layout, RentCast) and the dependencies' licences are credited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

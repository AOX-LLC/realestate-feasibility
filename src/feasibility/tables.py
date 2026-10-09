"""SQLAlchemy Core table definitions. Alembic migrations are the source of truth for the
database; tests/test_schema.py asserts that this module and the migrations agree.

No table has an owner, mailing-address or agent-contact column, so personal data
from the sources has nowhere to land.
"""

from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


def _check_name(constraint: Any, table: Any) -> str:
    """The name to put after `ck_<table>_`: the constraint's own name, less that prefix when it
    already carries it. A migration that spells the whole name (`ck_proforma_status`) and a table
    that gives the short one (`status`) therefore both end up as `ck_proforma_status`, never as
    `ck_proforma_ck_proforma_status`, which is what the plain convention made of the first five
    migrations."""
    name = str(constraint.name)
    prefix = f"ck_{table.name}_"
    return name[len(prefix) :] if name.startswith(prefix) else name


metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        # A callable under a token's name replaces the token for every convention above.
        "constraint_name": _check_name,
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

MONEY = Numeric(14, 2)


def _timestamp(name: str, *, nullable: bool = False) -> Column[Any]:
    if nullable:
        return Column(name, DateTime(timezone=True), nullable=True)
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _parcel_attribute_columns() -> list[Column[Any]]:
    """Columns shared by parcel_version and parcel, in canonical field order."""
    return [
        Column("gis_parcel_id", Text),
        Column("street_number", Text),
        Column("street_half", Text),
        Column("street_name", Text),
        Column("unit", Text),
        Column("city", Text),
        Column("zip5", Text),
        Column("land_value", MONEY),
        Column("improvement_value", MONEY),
        Column("total_value", MONEY),
        Column("year_built", SmallInteger),
        Column("living_area_sqft", Integer),
        Column("lot_size_sqft", Numeric(14, 2)),
        Column("use_code", Text),
        Column("zoning", Text),
    ]


source_file = Table(
    "source_file",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("roll_year", SmallInteger, nullable=False),
    Column("file_date", Date, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("carries_values", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("rows_read", Integer, nullable=False, server_default="0"),
    Column("rows_loaded", Integer, nullable=False, server_default="0"),
    Column("rows_skipped", Integer, nullable=False, server_default="0"),
    Column("skip_reasons", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error", Text),
    _timestamp("created_at"),
    _timestamp("completed_at", nullable=True),
    UniqueConstraint("market", "source", "kind", "roll_year", "file_date"),
    CheckConstraint("kind IN ('certified', 'current')", name="kind"),
    CheckConstraint("status IN ('loading', 'loaded', 'failed')", name="status"),
)

parcel_version = Table(
    "parcel_version",
    metadata,
    Column("market", Text, nullable=False),
    Column("account_id", Text, nullable=False),
    Column(
        "source_file_id",
        BigInteger,
        ForeignKey("source_file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    *_parcel_attribute_columns(),
    PrimaryKeyConstraint("market", "account_id", "source_file_id"),
    Index(None, "source_file_id"),
)

parcel = Table(
    "parcel",
    metadata,
    Column("market", Text, nullable=False),
    Column("account_id", Text, nullable=False),
    *_parcel_attribute_columns(),
    Column("attrs_file_date", Date, nullable=False),
    Column("values_file_date", Date),
    _timestamp("updated_at"),
    PrimaryKeyConstraint("market", "account_id"),
    Index(None, "market", "zip5"),
)

listing = Table(
    "listing",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("source", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("market", Text, nullable=False),
    Column("address_line", Text, nullable=False),
    Column("unit", Text),
    Column("city", Text),
    Column("state", Text),
    Column("zip5", Text),
    Column("price", MONEY),
    Column("status", Text),
    Column("property_type", Text),
    Column("lot_size_sqft", Numeric(14, 2)),
    Column("living_area_sqft", Integer),
    Column("year_built", SmallInteger),
    Column("listed_date", Date),
    Column("remarks", Text),
    _timestamp("first_seen_at"),
    _timestamp("last_seen_at"),
    Column("account_id", Text),
    Column("raw", JSONB, nullable=False),
    UniqueConstraint("source", "external_id"),
    Index(None, "market", "first_seen_at"),
    Index(None, "market", "last_seen_at"),
)

RUN_STATUSES = ("running", "completed", "failed")
SYNC_STATUSES = ("fresh", "stale", "skipped", "pending")
MATCH_STATUSES = ("matched", "ambiguous", "unmatched")
MATCH_METHODS = ("exact", "stem", "gis_group", "street_only")
LISTING_CHANGE_KINDS = ("new", "relisted", "price_changed", "unchanged", "gone", "aged_out")
CANDIDATE_CHANGE_KINDS = ("new", "relisted", "price_changed", "unchanged")
CANDIDATE_STATUSES = ("ranked", "filtered", "unscored")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


sourcing_run = Table(
    "sourcing_run",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market", Text, nullable=False),
    Column("as_of", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("sync_status", Text, nullable=False),
    Column("counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error", Text),
    _timestamp("started_at"),
    _timestamp("finished_at", nullable=True),
    UniqueConstraint("market", "as_of"),
    CheckConstraint(_in_list("status", RUN_STATUSES), name="status"),
    CheckConstraint(_in_list("sync_status", SYNC_STATUSES), name="sync_status"),
)

listing_match = Table(
    "listing_match",
    metadata,
    Column(
        "listing_id", BigInteger, ForeignKey("listing.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("status", Text, nullable=False),
    Column("method", Text),
    Column("account_id", Text),
    Column("gis_parcel_id", Text),
    Column("account_count", SmallInteger, nullable=False, server_default="0"),
    Column("street_key", Text, nullable=False),
    _timestamp("matched_at"),
    CheckConstraint(_in_list("status", MATCH_STATUSES), name="status"),
    CheckConstraint(_in_list("method", MATCH_METHODS), name="method"),
    CheckConstraint("(status = 'matched') = (method IS NOT NULL)", name="method_iff_matched"),
    Index(None, "account_id"),
)

# No ON DELETE cascade: a candidate's history is kept.
candidate = Table(
    "candidate",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market", Text, nullable=False),
    Column("property_key", Text, nullable=False),
    Column("account_id", Text),
    Column("gis_parcel_id", Text),
    Column("zip5", Text),
    Column("street_key", Text, nullable=False),
    Column("first_as_of", Date, nullable=False),
    _timestamp("created_at"),
    UniqueConstraint("market", "property_key"),
)

run_listing = Table(
    "run_listing",
    metadata,
    Column("run_id", BigInteger, ForeignKey("sourcing_run.id", ondelete="CASCADE"), nullable=False),
    Column("listing_id", BigInteger, ForeignKey("listing.id", ondelete="CASCADE"), nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("price", MONEY),
    Column("prev_price", MONEY),
    Column("candidate_id", BigInteger, ForeignKey("candidate.id")),
    Column("is_primary", Boolean, nullable=False, server_default=text("false")),
    Column("filter_reason", Text),
    # The match this run made; NULL when the run did not match the listing (and on rows
    # written before migration 0003).
    Column("match_status", Text),
    Column("match_method", Text),
    Column("match_account_id", Text),
    PrimaryKeyConstraint("run_id", "listing_id"),
    CheckConstraint(_in_list("change_kind", LISTING_CHANGE_KINDS), name="change_kind"),
    CheckConstraint(
        f"match_status IS NULL OR {_in_list('match_status', MATCH_STATUSES)}", name="match_status"
    ),
    CheckConstraint(
        f"match_method IS NULL OR {_in_list('match_method', MATCH_METHODS)}", name="match_method"
    ),
    CheckConstraint(
        "match_status IS NULL OR ((match_status = 'matched') = (match_method IS NOT NULL))",
        name="match_complete",
    ),
    CheckConstraint(
        "match_account_id IS NULL OR COALESCE(match_status = 'matched', false)",
        name="match_account",
    ),
    Index(None, "candidate_id"),
)

run_candidate = Table(
    "run_candidate",
    metadata,
    Column("run_id", BigInteger, ForeignKey("sourcing_run.id", ondelete="CASCADE"), nullable=False),
    Column("candidate_id", BigInteger, ForeignKey("candidate.id"), nullable=False),
    Column("primary_listing_id", BigInteger, ForeignKey("listing.id"), nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("filter_reasons", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("unscored_reason", Text),
    Column("score", Numeric(6, 2)),
    Column("rank", Integer),
    Column("breakdown", JSONB),
    PrimaryKeyConstraint("run_id", "candidate_id"),
    CheckConstraint(_in_list("change_kind", CANDIDATE_CHANGE_KINDS), name="change_kind"),
    CheckConstraint(_in_list("status", CANDIDATE_STATUSES), name="status"),
    CheckConstraint(
        "(status = 'ranked') = (rank IS NOT NULL AND score IS NOT NULL AND breakdown IS NOT NULL)",
        name="ranked_has_score",
    ),
    CheckConstraint(
        "(status = 'unscored') = (unscored_reason IS NOT NULL)", name="unscored_has_reason"
    ),
    UniqueConstraint("run_id", "rank"),
)

ESTIMATE_OUTCOMES = ("ok", "no_estimate")

# Not run-scoped: an estimate belongs to a candidate and a date, so a re-run keeps it and
# costs no new call. `comps` holds the sale comps kept from the response (the pro-forma's
# input), so a result can be reproduced without buying the estimate again.
candidate_estimate = Table(
    "candidate_estimate",
    metadata,
    Column("candidate_id", BigInteger, ForeignKey("candidate.id"), nullable=False),
    Column("fetched_on", Date, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("address", Text, nullable=False),
    Column("price", MONEY),
    Column("price_low", MONEY),
    Column("price_high", MONEY),
    Column("comp_count", SmallInteger, nullable=False, server_default="0"),
    Column("dropped_comp_count", SmallInteger, nullable=False, server_default="0"),
    Column("comps", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("run_id", BigInteger, ForeignKey("sourcing_run.id", ondelete="SET NULL")),
    PrimaryKeyConstraint("candidate_id", "fetched_on"),
    CheckConstraint(_in_list("outcome", ESTIMATE_OUTCOMES), name="outcome"),
    CheckConstraint("(outcome = 'ok') = (price IS NOT NULL)", name="ok_has_price"),
)

# Wider than a listing's price: the engine takes 15-digit comps, and a tiny ARV makes a huge margin.
RESULT_MONEY = Numeric(20, 2)
RESULT_RATIO = Numeric(20, 4)
PROFORMA_STATUSES = ("computed", "no_arv", "unsizable")

# One pro-forma per ranked candidate of a run; a re-run clears and rebuilds them through the
# cascade from run_candidate. The columns are the figures to list and filter by; `result` is
# the whole ProformaResult (every input and intermediate), which Phases 4 and 5 read.
proforma = Table(
    "proforma",
    metadata,
    Column("run_id", BigInteger, nullable=False),
    Column("candidate_id", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("estimate_fetched_on", Date),
    Column("offer_price", MONEY, nullable=False),
    Column("arv", RESULT_MONEY),
    Column("total_cost", RESULT_MONEY),
    Column("profit", RESULT_MONEY),
    Column("margin", RESULT_RATIO),
    Column("roi", RESULT_RATIO),
    Column("annualized_return", RESULT_RATIO),
    Column("max_offer", RESULT_MONEY),
    Column("flags", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("result", JSONB, nullable=False),
    PrimaryKeyConstraint("run_id", "candidate_id"),
    ForeignKeyConstraint(
        ["run_id", "candidate_id"],
        ["run_candidate.run_id", "run_candidate.candidate_id"],
        name="fk_proforma_run_candidate",
        ondelete="CASCADE",
    ),
    CheckConstraint(_in_list("status", PROFORMA_STATUSES), name="status"),
    CheckConstraint(
        "(status = 'computed') = (arv IS NOT NULL AND total_cost IS NOT NULL "
        "AND profit IS NOT NULL AND margin IS NOT NULL)",
        name="computed_has_outputs",
    ),
    CheckConstraint("(status = 'computed') = (reason IS NULL)", name="reason_iff_not_computed"),
    Index(None, "run_id", "status"),
)

api_cache = Table(
    "api_cache",
    metadata,
    Column("provider", Text, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("body", JSONB, nullable=False),
    _timestamp("fetched_at"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("provider", "request_key"),
)

api_budget = Table(
    "api_budget",
    metadata,
    Column("provider", Text, nullable=False),
    Column("period_start", Date, nullable=False),
    Column("request_limit", Integer, nullable=False),
    Column("used", Integer, nullable=False, server_default="0"),
    PrimaryKeyConstraint("provider", "period_start"),
    CheckConstraint("used >= 0", name="used_not_negative"),
)

REQUEST_OUTCOMES = (
    "ok",
    "not_found",
    "http_error",
    "network_error",
    "schema_error",
    "refused_budget",
    "cache_hit",
    "stale_served",
)

api_request_log = Table(
    "api_request_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("provider", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("period_start", Date),
    Column("outcome", Text, nullable=False),
    Column("status_code", SmallInteger),
    Column("billed", Boolean, nullable=False),
    _timestamp("at"),
    CheckConstraint(
        "outcome IN (" + ", ".join(f"'{outcome}'" for outcome in REQUEST_OUTCOMES) + ")",
        name="outcome",
    ),
    Index(None, "provider", "at"),
)

JOB_STATUSES = ("queued", "running", "done", "failed", "dead")

job = Table(
    "job",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("priority", SmallInteger, nullable=False, server_default="100"),
    Column("run_after", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="5"),
    Column("locked_by", Text),
    Column("locked_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("dedupe_key", Text),
    _timestamp("created_at"),
    _timestamp("updated_at"),
    _timestamp("finished_at", nullable=True),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in JOB_STATUSES) + ")",
        name="status",
    ),
    Index(
        "ix_job_claimable",
        "priority",
        "id",
        postgresql_where=text("status = 'queued'"),
    ),
    Index(
        "uq_job_dedupe_key_active",
        "dedupe_key",
        unique=True,
        postgresql_where=text("status IN ('queued', 'running')"),
    ),
)

# The ledger of model calls: one row per call in every outcome, never cleared by a re-run (it
# is spend, like api_request_log). No prompt text, remarks, model output or replay key is
# stored here. A call that raised has no known cost, so spend counts its reservation instead.
LLM_STAGES = ("signals", "narrative", "eval")
LLM_TIERS = ("small", "mid", "large")
LLM_MODES = ("replay", "record", "live")
LLM_OUTCOMES = (
    "ok",
    "structured_error",
    "refusal",
    "provider_error",
    "budget_refused",
    "replay_error",
)
LLM_USD = Numeric(12, 6)

llm_call = Table(
    "llm_call",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", BigInteger, ForeignKey("sourcing_run.id", ondelete="SET NULL")),
    Column("candidate_id", BigInteger, ForeignKey("candidate.id", ondelete="SET NULL")),
    Column("stage", Text, nullable=False),
    Column("prompt_id", Text, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("input_sha256", CHAR(64), nullable=False),
    Column("tier", Text, nullable=False),
    Column("model", Text),
    Column("mode", Text, nullable=False),
    Column("billable", Boolean, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("attempts", SmallInteger, nullable=False, server_default="1"),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cache_creation_input_tokens", Integer),
    Column("cache_read_input_tokens", Integer),
    Column("cost_usd", LLM_USD),
    Column("reserved_usd", LLM_USD, nullable=False),
    Column("latency_ms", Integer),
    _timestamp("called_at"),
    CheckConstraint(_in_list("stage", LLM_STAGES), name="stage"),
    CheckConstraint("prompt_id ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="prompt_id"),
    CheckConstraint("prompt_version >= 1", name="prompt_version"),
    CheckConstraint("attempts >= 1", name="attempts"),
    CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="input_sha256"),
    CheckConstraint(_in_list("tier", LLM_TIERS), name="tier"),
    CheckConstraint(_in_list("mode", LLM_MODES), name="mode"),
    CheckConstraint("billable = (mode IN ('record', 'live'))", name="billable"),
    CheckConstraint(_in_list("outcome", LLM_OUTCOMES), name="outcome"),
    CheckConstraint(
        "(cost_usd IS NULL OR cost_usd >= 0) AND reserved_usd >= 0 "
        "AND (input_tokens IS NULL OR input_tokens >= 0) "
        "AND (output_tokens IS NULL OR output_tokens >= 0) "
        "AND (cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0) "
        "AND (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0) "
        "AND (latency_ms IS NULL OR latency_ms >= 0)",
        name="nonnegative",
    ),
    CheckConstraint("(outcome = 'ok') = (cost_usd IS NOT NULL)", name="ok_has_cost"),
    Index(None, "run_id"),
    Index("ix_llm_call_billable_called_at", "called_at", postgresql_where=text("billable")),
)

# Verified model results, never the model's raw output. `llm_result` is a cache keyed by the
# input hash, not run-scoped: a re-run of the same inputs costs nothing. The two run tables hold
# one row per ranked candidate, rebuilt through the cascade from run_candidate on a re-run.
SIGNALS_STATUSES = ("extracted", "fields_only", "failed", "deferred")
SIGNALS_REASONS = (
    "no_remarks",
    "llm_not_configured",
    "budget",
    "provider_error",
    "structured_error",
    "refusal",
    "replay_error",
)
NARRATIVE_STATUSES = ("accepted", "rejected", "failed", "deferred", "not_eligible")
NARRATIVE_REASONS = (
    "proforma_no_arv",
    "proforma_unsizable",
    "figure_check",
    "basis_check",
    "length_check",
    "budget",
    "llm_not_configured",
    "provider_error",
    "structured_error",
    "refusal",
    "replay_error",
)

llm_result = Table(
    "llm_result",
    metadata,
    Column("prompt_id", Text, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("tier", Text, nullable=False),
    Column("input_sha256", CHAR(64), nullable=False),
    Column("result", JSONB, nullable=False),
    Column("llm_call_id", BigInteger, ForeignKey("llm_call.id", ondelete="SET NULL")),
    _timestamp("created_at"),
    PrimaryKeyConstraint("prompt_id", "prompt_version", "tier", "input_sha256"),
    CheckConstraint("prompt_id ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="prompt_id"),
    CheckConstraint("prompt_version >= 1", name="prompt_version"),
    CheckConstraint(_in_list("tier", LLM_TIERS), name="tier"),
    CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="input_sha256"),
)

candidate_signals = Table(
    "candidate_signals",
    metadata,
    Column("run_id", BigInteger, nullable=False),
    Column("candidate_id", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    # The primary listing whose remarks were read.
    Column("listing_id", BigInteger, ForeignKey("listing.id"), nullable=False),
    Column("result", JSONB, nullable=False),
    PrimaryKeyConstraint("run_id", "candidate_id"),
    ForeignKeyConstraint(
        ["run_id", "candidate_id"],
        ["run_candidate.run_id", "run_candidate.candidate_id"],
        name="fk_candidate_signals_run_candidate",
        ondelete="CASCADE",
    ),
    CheckConstraint(_in_list("status", SIGNALS_STATUSES), name="status"),
    CheckConstraint(
        f"reason IS NULL OR {_in_list('reason', SIGNALS_REASONS)}", name="reason_known"
    ),
    CheckConstraint("(status = 'extracted') = (reason IS NULL)", name="reason"),
)

candidate_narrative = Table(
    "candidate_narrative",
    metadata,
    Column("run_id", BigInteger, nullable=False),
    Column("candidate_id", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("input_sha256", CHAR(64)),
    Column("result", JSONB, nullable=False),
    PrimaryKeyConstraint("run_id", "candidate_id"),
    ForeignKeyConstraint(
        ["run_id", "candidate_id"],
        ["run_candidate.run_id", "run_candidate.candidate_id"],
        name="fk_candidate_narrative_run_candidate",
        ondelete="CASCADE",
    ),
    CheckConstraint(_in_list("status", NARRATIVE_STATUSES), name="status"),
    CheckConstraint(
        f"reason IS NULL OR {_in_list('reason', NARRATIVE_REASONS)}", name="reason_known"
    ),
    CheckConstraint("(status = 'accepted') = (reason IS NULL)", name="reason"),
    CheckConstraint("input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'", name="input_sha256"),
    CheckConstraint(
        "status <> 'not_eligible' OR input_sha256 IS NULL", name="not_eligible_no_digest"
    ),
    Index(None, "run_id", "status"),
)

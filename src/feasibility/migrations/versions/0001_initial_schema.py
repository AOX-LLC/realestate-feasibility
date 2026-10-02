"""Initial schema: CAD source files and parcels, listings, the provider cache and budget,
and the job queue.

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)


def _now() -> sa.TextClause:
    return sa.text("now()")


def _parcel_attribute_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("gis_parcel_id", sa.Text()),
        sa.Column("street_number", sa.Text()),
        sa.Column("street_half", sa.Text()),
        sa.Column("street_name", sa.Text()),
        sa.Column("unit", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("zip5", sa.Text()),
        sa.Column("land_value", MONEY),
        sa.Column("improvement_value", MONEY),
        sa.Column("total_value", MONEY),
        sa.Column("year_built", sa.SmallInteger()),
        sa.Column("living_area_sqft", sa.Integer()),
        sa.Column("lot_size_sqft", sa.Numeric(14, 2)),
        sa.Column("use_code", sa.Text()),
        sa.Column("zoning", sa.Text()),
    ]


def upgrade() -> None:
    op.create_table(
        "source_file",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("roll_year", sa.SmallInteger(), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("carries_values", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_loaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skip_reasons", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_source_file"),
        sa.UniqueConstraint(
            "market",
            "source",
            "kind",
            "roll_year",
            "file_date",
            name="uq_source_file_market_source_kind_roll_year_file_date",
        ),
        sa.CheckConstraint("kind IN ('certified', 'current')", name="ck_source_file_kind"),
        sa.CheckConstraint(
            "status IN ('loading', 'loaded', 'failed')", name="ck_source_file_status"
        ),
    )

    op.create_table(
        "parcel_version",
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("source_file_id", sa.BigInteger(), nullable=False),
        *_parcel_attribute_columns(),
        sa.PrimaryKeyConstraint("market", "account_id", "source_file_id", name="pk_parcel_version"),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_file.id"],
            ondelete="CASCADE",
            name="fk_parcel_version_source_file_id_source_file",
        ),
    )
    op.create_index("ix_parcel_version_source_file_id", "parcel_version", ["source_file_id"])

    op.create_table(
        "parcel",
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        *_parcel_attribute_columns(),
        sa.Column("attrs_file_date", sa.Date(), nullable=False),
        sa.Column("values_file_date", sa.Date()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("market", "account_id", name="pk_parcel"),
    )
    op.create_index("ix_parcel_market_zip5", "parcel", ["market", "zip5"])

    op.create_table(
        "listing",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("address_line", sa.Text(), nullable=False),
        sa.Column("city", sa.Text()),
        sa.Column("state", sa.Text()),
        sa.Column("zip5", sa.Text()),
        sa.Column("price", MONEY),
        sa.Column("status", sa.Text()),
        sa.Column("property_type", sa.Text()),
        sa.Column("lot_size_sqft", sa.Numeric(14, 2)),
        sa.Column("living_area_sqft", sa.Integer()),
        sa.Column("year_built", sa.SmallInteger()),
        sa.Column("listed_date", sa.Date()),
        sa.Column("remarks", sa.Text()),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()
        ),
        sa.Column("account_id", sa.Text()),
        sa.Column("raw", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_listing"),
        sa.UniqueConstraint("source", "external_id", name="uq_listing_source_external_id"),
    )
    op.create_index("ix_listing_market_first_seen_at", "listing", ["market", "first_seen_at"])

    op.create_table(
        "api_cache",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider", "request_key", name="pk_api_cache"),
    )

    op.create_table(
        "api_budget",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("request_limit", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("provider", "period_start", name="pk_api_budget"),
        sa.CheckConstraint("used >= 0", name="ck_api_budget_used_not_negative"),
    )

    op.create_table(
        "api_request_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date()),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("status_code", sa.SmallInteger()),
        sa.Column("billed", sa.Boolean(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_api_request_log"),
        sa.CheckConstraint(
            "outcome IN ('ok', 'not_found', 'http_error', 'network_error', 'schema_error', "
            "'refused_budget', 'cache_hit', 'stale_served')",
            name="ck_api_request_log_outcome",
        ),
    )
    op.create_index("ix_api_request_log_provider_at", "api_request_log", ["provider", "at"])

    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="100"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("dedupe_key", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'dead')", name="ck_job_status"
        ),
    )
    op.create_index(
        "ix_job_claimable",
        "job",
        ["priority", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "uq_job_dedupe_key_active",
        "job",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    for table in (
        "job",
        "api_request_log",
        "api_budget",
        "api_cache",
        "listing",
        "parcel",
        "parcel_version",
        "source_file",
    ):
        op.drop_table(table)

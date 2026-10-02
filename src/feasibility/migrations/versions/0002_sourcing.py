"""Sourcing: listing unit, match results, candidates and the daily run diff.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column("listing", sa.Column("unit", sa.Text()))
    op.create_index("ix_listing_market_last_seen_at", "listing", ["market", "last_seen_at"])

    op.create_table(
        "sourcing_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sync_status", sa.Text(), nullable=False),
        sa.Column("counts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_sourcing_run"),
        sa.UniqueConstraint("market", "as_of", name="uq_sourcing_run_market_as_of"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name="ck_sourcing_run_status"
        ),
        sa.CheckConstraint(
            "sync_status IN ('fresh', 'stale', 'skipped', 'pending')",
            name="ck_sourcing_run_sync_status",
        ),
    )

    op.create_table(
        "listing_match",
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("method", sa.Text()),
        sa.Column("account_id", sa.Text()),
        sa.Column("gis_parcel_id", sa.Text()),
        sa.Column("account_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("street_key", sa.Text(), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("listing_id", name="pk_listing_match"),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listing.id"],
            name="fk_listing_match_listing_id_listing",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('matched', 'ambiguous', 'unmatched')", name="ck_listing_match_status"
        ),
        sa.CheckConstraint(
            "method IN ('exact', 'stem', 'gis_group', 'street_only')",
            name="ck_listing_match_method",
        ),
        sa.CheckConstraint(
            "(status = 'matched') = (method IS NOT NULL)",
            name="ck_listing_match_method_iff_matched",
        ),
    )
    op.create_index("ix_listing_match_account_id", "listing_match", ["account_id"])

    op.create_table(
        "candidate",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("property_key", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text()),
        sa.Column("gis_parcel_id", sa.Text()),
        sa.Column("zip5", sa.Text()),
        sa.Column("street_key", sa.Text(), nullable=False),
        sa.Column("first_as_of", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.PrimaryKeyConstraint("id", name="pk_candidate"),
        sa.UniqueConstraint("market", "property_key", name="uq_candidate_market_property_key"),
    )

    op.create_table(
        "run_listing",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column("price", MONEY),
        sa.Column("prev_price", MONEY),
        sa.Column("candidate_id", sa.BigInteger()),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("filter_reason", sa.Text()),
        sa.PrimaryKeyConstraint("run_id", "listing_id", name="pk_run_listing"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name="fk_run_listing_run_id_sourcing_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listing.id"],
            name="fk_run_listing_listing_id_listing",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidate.id"], name="fk_run_listing_candidate_id_candidate"
        ),
        sa.CheckConstraint(
            "change_kind IN ('new', 'relisted', 'price_changed', 'unchanged', 'gone', 'aged_out')",
            name="ck_run_listing_change_kind",
        ),
    )
    op.create_index("ix_run_listing_candidate_id", "run_listing", ["candidate_id"])

    op.create_table(
        "run_candidate",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("primary_listing_id", sa.BigInteger(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filter_reasons", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("unscored_reason", sa.Text()),
        sa.Column("score", sa.Numeric(6, 2)),
        sa.Column("rank", sa.Integer()),
        sa.Column("breakdown", JSONB()),
        sa.PrimaryKeyConstraint("run_id", "candidate_id", name="pk_run_candidate"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name="fk_run_candidate_run_id_sourcing_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidate.id"], name="fk_run_candidate_candidate_id_candidate"
        ),
        sa.ForeignKeyConstraint(
            ["primary_listing_id"],
            ["listing.id"],
            name="fk_run_candidate_primary_listing_id_listing",
        ),
        sa.CheckConstraint(
            "change_kind IN ('new', 'relisted', 'price_changed', 'unchanged')",
            name="ck_run_candidate_change_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ranked', 'filtered', 'unscored')", name="ck_run_candidate_status"
        ),
        sa.CheckConstraint(
            "(status = 'ranked') = (rank IS NOT NULL AND score IS NOT NULL "
            "AND breakdown IS NOT NULL)",
            name="ck_run_candidate_ranked_has_score",
        ),
        sa.CheckConstraint(
            "(status = 'unscored') = (unscored_reason IS NOT NULL)",
            name="ck_run_candidate_unscored_has_reason",
        ),
        sa.UniqueConstraint("run_id", "rank", name="uq_run_candidate_run_id_rank"),
    )


def downgrade() -> None:
    op.drop_table("run_candidate")
    op.drop_index("ix_run_listing_candidate_id", table_name="run_listing")
    op.drop_table("run_listing")
    op.drop_table("candidate")
    op.drop_index("ix_listing_match_account_id", table_name="listing_match")
    op.drop_table("listing_match")
    op.drop_table("sourcing_run")
    op.drop_index("ix_listing_market_last_seen_at", table_name="listing")
    op.drop_column("listing", "unit")

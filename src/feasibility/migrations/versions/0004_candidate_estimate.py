"""Value estimates bought for a candidate, with the sale comps they came with.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    op.create_table(
        "candidate_estimate",
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("fetched_on", sa.Date(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("price", MONEY),
        sa.Column("price_low", MONEY),
        sa.Column("price_high", MONEY),
        sa.Column("comp_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("dropped_comp_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("comps", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("run_id", sa.BigInteger()),
        sa.PrimaryKeyConstraint("candidate_id", "fetched_on", name="pk_candidate_estimate"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidate.id"], name="fk_candidate_estimate_candidate_id_candidate"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name="fk_candidate_estimate_run_id_sourcing_run",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'no_estimate')", name="ck_candidate_estimate_outcome"
        ),
        sa.CheckConstraint(
            "(outcome = 'ok') = (price IS NOT NULL)", name="ck_candidate_estimate_ok_has_price"
        ),
    )


def downgrade() -> None:
    op.drop_table("candidate_estimate")

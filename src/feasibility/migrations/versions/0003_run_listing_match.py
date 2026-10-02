"""Each run stores the match it made for a listing.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No backfill: copying today's listing_match into old rows would reproduce the bug.
    op.add_column("run_listing", sa.Column("match_status", sa.Text()))
    op.add_column("run_listing", sa.Column("match_method", sa.Text()))
    op.add_column("run_listing", sa.Column("match_account_id", sa.Text()))
    op.create_check_constraint(
        "ck_run_listing_match_status",
        "run_listing",
        "match_status IS NULL OR match_status IN ('matched', 'ambiguous', 'unmatched')",
    )
    op.create_check_constraint(
        "ck_run_listing_match_method",
        "run_listing",
        "match_method IS NULL OR match_method IN ('exact', 'stem', 'gis_group', 'street_only')",
    )
    op.create_check_constraint(
        "ck_run_listing_match_complete",
        "run_listing",
        "match_status IS NULL OR ((match_status = 'matched') = (match_method IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_run_listing_match_account",
        "run_listing",
        "match_account_id IS NULL OR COALESCE(match_status = 'matched', false)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_listing_match_account", "run_listing", type_="check")
    op.drop_constraint("ck_run_listing_match_complete", "run_listing", type_="check")
    op.drop_constraint("ck_run_listing_match_method", "run_listing", type_="check")
    op.drop_constraint("ck_run_listing_match_status", "run_listing", type_="check")
    op.drop_column("run_listing", "match_account_id")
    op.drop_column("run_listing", "match_method")
    op.drop_column("run_listing", "match_status")

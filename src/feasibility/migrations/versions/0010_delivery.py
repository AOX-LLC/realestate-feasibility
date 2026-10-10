"""The delivery ledger: what was sent to Notion and Slack, for which run, and how it ended.

A row is written as `sending` before each outbound call and finished after it, so a crash between
the two leaves a row that says the outcome is unknown (the same pattern as the model-call ledger).
Nothing here can hold a payload, a URL or a service's own words: an item is one of three shapes, a
remote reference is a short id, and an error is a short code.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("remote_ref", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name=op.f("fk_delivery_run_id_sourcing_run"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id", "target", "item", "mode", name=op.f("uq_delivery_run_id_target_item_mode")
        ),
        sa.CheckConstraint("target IN ('notion', 'slack')", name=op.f("ck_delivery_target")),
        sa.CheckConstraint(
            "item ~ '^(digest|row:[0-9]{1,19}|file:[0-9]{1,19})$'", name=op.f("ck_delivery_item")
        ),
        sa.CheckConstraint("mode IN ('mock', 'live')", name=op.f("ck_delivery_mode")),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'unknown', 'skipped')",
            name=op.f("ck_delivery_status"),
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_delivery_content_sha256")
        ),
        sa.CheckConstraint(
            "remote_ref IS NULL OR remote_ref ~ '^[A-Za-z0-9._:-]{1,64}$'",
            name=op.f("ck_delivery_remote_ref"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,64}$'",
            name=op.f("ck_delivery_error_code"),
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR remote_ref IS NOT NULL", name=op.f("ck_delivery_sent_has_ref")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_delivery_attempts")),
    )
    # The newest delivered row of an item in any run: the page a Notion row was last written to.
    op.create_index(
        op.f("ix_delivery_target_item_mode_updated_at"),
        "delivery",
        ["target", "item", "mode", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("delivery")

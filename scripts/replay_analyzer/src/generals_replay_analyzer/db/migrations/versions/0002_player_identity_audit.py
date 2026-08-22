"""Add the immutable player identity audit ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0002_player_identity_audit"
down_revision = "0001_replay_analyzer_v2"
branch_labels = None
depends_on = None


# TheSuperHackers @feature Leex 22/08/2026 Add append-only provenance for reversible canonical identity changes. (#TBD)
def upgrade() -> None:
    op.create_table(
        "player_identity_operations",
        sa.Column("operation_kind", sa.String(length=64), nullable=False),
        sa.Column("inverse_of_operation_id", sa.Integer(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("after_json", sa.Text(), nullable=False),
        sa.Column("inverse_payload_json", sa.Text(), nullable=False),
        sa.Column("affected_revisions_json", sa.Text(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(actor)) > 0", name="actor_nonempty"),
        sa.CheckConstraint(
            "operation_kind IN ('auto_link','merge_players','split_alias','attach_external_alias','inverse')",
            name="operation_kind_valid",
        ),
        sa.CheckConstraint(
            "length(public_id) = 36 AND public_id = lower(public_id) "
            "AND substr(public_id, 9, 1) = '-' AND substr(public_id, 14, 1) = '-' "
            "AND substr(public_id, 19, 1) = '-' AND substr(public_id, 24, 1) = '-' "
            "AND length(replace(public_id, '-', '')) = 32 "
            "AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'",
            name="public_id_lowercase_uuid",
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name="reason_nonempty"),
        sa.ForeignKeyConstraint(
            ["inverse_of_operation_id"],
            ["player_identity_operations.id"],
            name="fk_player_identity_operations_inverse_of_operation_id_player_identity_operations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_player_identity_operations"),
        sa.UniqueConstraint("public_id", name="uq_player_identity_operations_public_id"),
    )
    op.create_index(
        "ix_player_identity_operations_created_kind",
        "player_identity_operations",
        ["created_at", "operation_kind"],
        unique=False,
    )
    op.create_index(
        "ix_player_identity_operations_inverse_of",
        "player_identity_operations",
        ["inverse_of_operation_id"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER trg_player_identity_operations_no_update BEFORE UPDATE ON player_identity_operations "
        "BEGIN SELECT RAISE(ABORT, 'player identity audit is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_player_identity_operations_no_delete BEFORE DELETE ON player_identity_operations "
        "BEGIN SELECT RAISE(ABORT, 'player identity audit is immutable'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_player_identity_operations_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_player_identity_operations_no_update")
    op.drop_index("ix_player_identity_operations_inverse_of", table_name="player_identity_operations")
    op.drop_index("ix_player_identity_operations_created_kind", table_name="player_identity_operations")
    op.drop_table("player_identity_operations")

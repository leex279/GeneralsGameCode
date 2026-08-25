"""Persist optional canonical player external profile metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0006_player_external_profile"
down_revision = "0005_llm_graph_immutability"
branch_labels = None
depends_on = None


# TheSuperHackers @feature Leex 25/08/2026 Add profile metadata to canonical players without changing observations. (#TBD)
def upgrade() -> None:
    with op.batch_alter_table("players") as batch:
        batch.add_column(sa.Column("external_profile_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_profile_source", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("players") as batch:
        batch.drop_column("external_profile_source")
        batch.drop_column("external_profile_url")

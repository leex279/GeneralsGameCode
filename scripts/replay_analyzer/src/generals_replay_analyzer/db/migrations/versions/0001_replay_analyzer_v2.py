"""Create the complete Replay Analyzer V2 persistence baseline."""

from __future__ import annotations

from alembic import op

from generals_replay_analyzer.db import Base
from generals_replay_analyzer.db.models import baseline_tables, immutability_triggers

revision = "0001_replay_analyzer_v2"
down_revision = None
branch_labels = None
depends_on = None


# TheSuperHackers @feature Leex 21/08/2026 Make successful parser and telemetry evidence raw-SQL immutable. (#TBD)
def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=baseline_tables(), checkfirst=False)
    for _name, statement in immutability_triggers():
        op.execute(statement)


def downgrade() -> None:
    for name, _statement in reversed(immutability_triggers()):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    Base.metadata.drop_all(bind=op.get_bind(), tables=baseline_tables(), checkfirst=False)

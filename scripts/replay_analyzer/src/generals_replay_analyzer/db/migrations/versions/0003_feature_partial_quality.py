"""Allow partial features to retain a stable omission reason."""

import json

from alembic import op

revision = "0003_feature_partial_quality"
down_revision = "0002_player_identity_audit"
branch_labels = None
depends_on = None

_TYPED_VALUE = (
    "((value_type = 'integer' AND integer_value IS NOT NULL AND real_value IS NULL AND text_value IS NULL "
    "AND boolean_value IS NULL AND json_value IS NULL) OR "
    "(value_type = 'real' AND integer_value IS NULL AND real_value IS NOT NULL AND text_value IS NULL "
    "AND boolean_value IS NULL AND json_value IS NULL) OR "
    "(value_type = 'text' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NOT NULL "
    "AND boolean_value IS NULL AND json_value IS NULL) OR "
    "(value_type = 'boolean' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
    "AND boolean_value IS NOT NULL AND json_value IS NULL) OR "
    "(value_type = 'json' AND integer_value IS NULL AND real_value IS NULL AND text_value IS NULL "
    "AND boolean_value IS NULL AND json_value IS NOT NULL))"
)
_NULL_VALUE = (
    "integer_value IS NULL AND real_value IS NULL AND text_value IS NULL AND boolean_value IS NULL "
    "AND json_value IS NULL"
)
_OLD_QUALITY = (
    f"(quality = 'unavailable' AND {_NULL_VALUE} AND quality_reason IS NOT NULL) OR "
    f"(quality != 'unavailable' AND quality_reason IS NULL AND {_TYPED_VALUE})"
)
_NEW_QUALITY = (
    f"(quality = 'available' AND quality_reason IS NULL AND {_TYPED_VALUE}) OR "
    f"(quality = 'partial' AND quality_reason IS NOT NULL AND length(trim(quality_reason)) > 0 AND {_TYPED_VALUE}) OR "
    f"(quality = 'unavailable' AND {_NULL_VALUE} AND quality_reason IS NOT NULL "
    "AND length(trim(quality_reason)) > 0)"
)
_COMPATIBILITY_KEY = "__task6_partial_quality_compatibility__"
_COMPATIBILITY_SCHEMA = "task6-partial-quality-downgrade-v1"


def _compatible_feature_rows(connection: object, columns: str, direction: str) -> list[tuple[object, ...]]:
    rows = [
        list(row)
        for row in connection.exec_driver_sql(  # type: ignore[attr-defined]
            f"SELECT {columns} FROM features ORDER BY id"
        )
    ]
    for row in rows:
        if row[17] != "partial":
            continue
        if direction == "downgrade":
            marker = {
                _COMPATIBILITY_KEY: {
                    "details_json": row[21],
                    "feature_public_id": row[23],
                    "quality_reason": row[18],
                    "schema": _COMPATIBILITY_SCHEMA,
                }
            }
            row[18] = None
            row[21] = json.dumps(marker, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
            continue
        if row[18] is not None or not isinstance(row[21], str):
            continue
        try:
            marker_value = json.loads(row[21])
        except (TypeError, ValueError):
            continue
        if not isinstance(marker_value, dict) or set(marker_value) != {_COMPATIBILITY_KEY}:
            continue
        compatibility = marker_value[_COMPATIBILITY_KEY]
        if (
            not isinstance(compatibility, dict)
            or compatibility.get("schema") != _COMPATIBILITY_SCHEMA
            or compatibility.get("feature_public_id") != row[23]
            or not isinstance(compatibility.get("quality_reason"), str)
            or not compatibility["quality_reason"].strip()
            or not isinstance(compatibility.get("details_json"), str)
        ):
            continue
        row[18] = compatibility["quality_reason"]
        row[21] = compatibility["details_json"]
    return [tuple(row) for row in rows]


def _replace_quality_constraint(expression: str, direction: str) -> None:
    connection = op.get_bind()
    evidence_links = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT feature_id, evidence_item_id, role FROM feature_evidence ORDER BY feature_id, evidence_item_id, role"
        )
    ]
    longitudinal_links = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT id, feature_id FROM longitudinal_members WHERE feature_id IS NOT NULL ORDER BY id"
        )
    ]
    connection.exec_driver_sql("DELETE FROM feature_evidence")
    connection.exec_driver_sql("UPDATE longitudinal_members SET feature_id = NULL WHERE feature_id IS NOT NULL")
    connection.exec_driver_sql(
        f"""CREATE TABLE task6_features_replacement (
	feature_set_id INTEGER NOT NULL,
	evidence_item_id INTEGER NOT NULL,
	name VARCHAR(255) NOT NULL,
	value_type VARCHAR(32) NOT NULL,
	integer_value INTEGER,
	real_value FLOAT,
	text_value TEXT,
	boolean_value BOOLEAN,
	json_value TEXT,
	unit VARCHAR(64),
	scope_type VARCHAR(64) NOT NULL,
	scope_key VARCHAR(255) NOT NULL,
	replay_player_id INTEGER,
	team_id INTEGER,
	entity_id INTEGER,
	frame_start INTEGER NOT NULL,
	frame_end INTEGER NOT NULL,
	quality VARCHAR(32) NOT NULL,
	quality_reason TEXT,
	confidence FLOAT,
	explanation TEXT,
	details_json TEXT NOT NULL,
	id INTEGER NOT NULL,
	public_id VARCHAR(36) NOT NULL CONSTRAINT ck_features_public_id_lowercase_uuid CHECK (length(public_id) = 36 AND public_id = lower(public_id) AND substr(public_id, 9, 1) = '-' AND substr(public_id, 14, 1) = '-' AND substr(public_id, 19, 1) = '-' AND substr(public_id, 24, 1) = '-' AND length(replace(public_id, '-', '')) = 32 AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT pk_features PRIMARY KEY (id),
	CONSTRAINT uq_features_evidence_item_id UNIQUE (evidence_item_id),
	CONSTRAINT uq_features_logical_identity UNIQUE (feature_set_id, name, scope_type, scope_key, frame_start, frame_end),
	CONSTRAINT ck_features_value_type_valid CHECK (value_type IN ('integer','real','text','boolean','json')),
	CONSTRAINT ck_features_quality_valid CHECK (quality IN ('available','unavailable','partial')),
	CONSTRAINT ck_features_frame_window_valid CHECK (frame_start >= 0 AND frame_end >= frame_start),
	CONSTRAINT ck_features_confidence_bounded CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
	CONSTRAINT ck_features_boolean_value_boolean CHECK (boolean_value IS NULL OR boolean_value IN (0, 1)),
	CONSTRAINT ck_features_typed_value_matches_quality CHECK ({expression}),
	CONSTRAINT fk_features_feature_set_id_feature_sets FOREIGN KEY(feature_set_id) REFERENCES feature_sets (id) ON DELETE CASCADE,
	CONSTRAINT fk_features_evidence_item_id_evidence_items FOREIGN KEY(evidence_item_id) REFERENCES evidence_items (id) ON DELETE RESTRICT,
	CONSTRAINT fk_features_replay_player_id_replay_players FOREIGN KEY(replay_player_id) REFERENCES replay_players (id) ON DELETE SET NULL,
	CONSTRAINT fk_features_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE SET NULL,
	CONSTRAINT uq_features_public_id UNIQUE (public_id)
)"""
    )
    columns = (
        "feature_set_id, evidence_item_id, name, value_type, integer_value, real_value, text_value, boolean_value, "
        "json_value, unit, scope_type, scope_key, replay_player_id, team_id, entity_id, frame_start, frame_end, quality, "
        "quality_reason, confidence, explanation, details_json, id, public_id"
    )
    feature_rows = _compatible_feature_rows(connection, columns, direction)
    if feature_rows:
        placeholders = ", ".join("?" for _ in range(24))
        connection.exec_driver_sql(
            f"INSERT INTO task6_features_replacement ({columns}) VALUES ({placeholders})",
            feature_rows,
        )
    connection.exec_driver_sql("DROP TABLE features")
    connection.exec_driver_sql("ALTER TABLE task6_features_replacement RENAME TO features")
    for index_statement in (
        "CREATE INDEX ix_features_entity_id ON features (entity_id)",
        "CREATE INDEX ix_features_replay_player_id ON features (replay_player_id)",
        "CREATE INDEX ix_features_name_scope_window ON features (name, scope_type, scope_key, frame_start, frame_end)",
        "CREATE INDEX ix_features_feature_set_id ON features (feature_set_id)",
        "CREATE INDEX ix_features_evidence_item_id ON features (evidence_item_id)",
    ):
        connection.exec_driver_sql(index_statement)
    if longitudinal_links:
        connection.exec_driver_sql("UPDATE longitudinal_members SET feature_id = ? WHERE id = ?", [
            (feature_id, member_id) for member_id, feature_id in longitudinal_links
        ])
    if evidence_links:
        connection.exec_driver_sql(
            "INSERT INTO feature_evidence (feature_id, evidence_item_id, role) VALUES (?, ?, ?)", evidence_links
        )


# TheSuperHackers @bugfix Leex 22/08/2026 Preserve truthful omission reasons on partial derived features. (#TBD)
def upgrade() -> None:
    _replace_quality_constraint(_NEW_QUALITY, "upgrade")


def downgrade() -> None:
    # TheSuperHackers @fix Leex 22/08/2026 Carry partial reasons reversibly through the exact old constraint. (#TBD)
    _replace_quality_constraint(_OLD_QUALITY, "downgrade")

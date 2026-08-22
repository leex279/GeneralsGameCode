"""Freeze complete ownership graphs once a local-LLM analysis succeeds."""

from alembic import op

revision = "0005_llm_graph_immutability"
down_revision = "0004_job_lifecycle"
branch_labels = None
depends_on = None

_MESSAGE = "succeeded llm analysis graph is immutable"

_SUCCEEDED_ASSESSMENT_OLD = (
    "EXISTS (SELECT 1 FROM analysis_runs ar "
    "WHERE ar.id = OLD.analysis_run_id AND ar.status = 'succeeded')"
)
_SUCCEEDED_ASSESSMENT_NEW = (
    "EXISTS (SELECT 1 FROM analysis_runs ar "
    "WHERE ar.id = NEW.analysis_run_id AND ar.status = 'succeeded')"
)
_SUCCEEDED_ASSET_OLD = (
    "EXISTS (SELECT 1 FROM analysis_runs ar "
    "WHERE ar.raw_response_asset_id = OLD.id AND ar.status = 'succeeded')"
)
_SUCCEEDED_LINK_OLD = (
    "EXISTS (SELECT 1 FROM strategy_assessments sa JOIN analysis_runs ar ON ar.id = sa.analysis_run_id "
    "WHERE sa.id = OLD.assessment_id AND ar.status = 'succeeded')"
)
_SUCCEEDED_LINK_NEW = (
    "EXISTS (SELECT 1 FROM strategy_assessments sa JOIN analysis_runs ar ON ar.id = sa.analysis_run_id "
    "WHERE sa.id = NEW.assessment_id AND ar.status = 'succeeded')"
)
_SUCCEEDED_EVIDENCE_OLD = (
    "EXISTS (SELECT 1 FROM strategy_assessments sa JOIN analysis_runs ar ON ar.id = sa.analysis_run_id "
    "WHERE ar.status = 'succeeded' AND (sa.evidence_item_id = OLD.id OR EXISTS "
    "(SELECT 1 FROM assessment_evidence ae WHERE ae.assessment_id = sa.id AND ae.evidence_item_id = OLD.id)))"
)
_SUCCEEDED_EVIDENCE_NEW_NAMESPACE = (
    "EXISTS (SELECT 1 FROM analysis_runs ar WHERE ar.status = 'succeeded' AND "
    "substr(NEW.source_key, 1, length('analysis-run:' || ar.run_id || ':')) = "
    "'analysis-run:' || ar.run_id || ':' AND "
    "length(NEW.source_key) > length('analysis-run:' || ar.run_id || ':'))"
)


def _trigger(name: str, timing: str, table: str, condition: str) -> str:
    return (
        f"CREATE TRIGGER {name} {timing} ON {table} WHEN {condition} "
        f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
    )


_TRIGGERS = (
    (
        "trg_analysis_runs_succeeded_llm_no_update",
        _trigger(
            "trg_analysis_runs_succeeded_llm_no_update",
            "BEFORE UPDATE",
            "analysis_runs",
            "OLD.status = 'succeeded'",
        ),
    ),
    (
        "trg_analysis_runs_succeeded_llm_no_delete",
        _trigger(
            "trg_analysis_runs_succeeded_llm_no_delete",
            "BEFORE DELETE",
            "analysis_runs",
            "OLD.status = 'succeeded'",
        ),
    ),
    (
        "trg_managed_assets_succeeded_llm_no_update",
        _trigger(
            "trg_managed_assets_succeeded_llm_no_update",
            "BEFORE UPDATE",
            "managed_assets",
            _SUCCEEDED_ASSET_OLD,
        ),
    ),
    (
        "trg_managed_assets_succeeded_llm_no_delete",
        _trigger(
            "trg_managed_assets_succeeded_llm_no_delete",
            "BEFORE DELETE",
            "managed_assets",
            _SUCCEEDED_ASSET_OLD,
        ),
    ),
    (
        "trg_strategy_assessments_succeeded_llm_no_insert",
        _trigger(
            "trg_strategy_assessments_succeeded_llm_no_insert",
            "BEFORE INSERT",
            "strategy_assessments",
            _SUCCEEDED_ASSESSMENT_NEW,
        ),
    ),
    (
        "trg_strategy_assessments_succeeded_llm_no_update",
        _trigger(
            "trg_strategy_assessments_succeeded_llm_no_update",
            "BEFORE UPDATE",
            "strategy_assessments",
            f"({_SUCCEEDED_ASSESSMENT_OLD}) OR ({_SUCCEEDED_ASSESSMENT_NEW})",
        ),
    ),
    (
        "trg_strategy_assessments_succeeded_llm_no_delete",
        _trigger(
            "trg_strategy_assessments_succeeded_llm_no_delete",
            "BEFORE DELETE",
            "strategy_assessments",
            _SUCCEEDED_ASSESSMENT_OLD,
        ),
    ),
    (
        "trg_assessment_evidence_succeeded_llm_no_insert",
        _trigger(
            "trg_assessment_evidence_succeeded_llm_no_insert",
            "BEFORE INSERT",
            "assessment_evidence",
            _SUCCEEDED_LINK_NEW,
        ),
    ),
    (
        "trg_assessment_evidence_succeeded_llm_no_update",
        _trigger(
            "trg_assessment_evidence_succeeded_llm_no_update",
            "BEFORE UPDATE",
            "assessment_evidence",
            f"({_SUCCEEDED_LINK_OLD}) OR ({_SUCCEEDED_LINK_NEW})",
        ),
    ),
    (
        "trg_assessment_evidence_succeeded_llm_no_delete",
        _trigger(
            "trg_assessment_evidence_succeeded_llm_no_delete",
            "BEFORE DELETE",
            "assessment_evidence",
            _SUCCEEDED_LINK_OLD,
        ),
    ),
    (
        "trg_evidence_items_succeeded_llm_no_insert",
        _trigger(
            "trg_evidence_items_succeeded_llm_no_insert",
            "BEFORE INSERT",
            "evidence_items",
            _SUCCEEDED_EVIDENCE_NEW_NAMESPACE,
        ),
    ),
    (
        "trg_evidence_items_succeeded_llm_no_update",
        _trigger(
            "trg_evidence_items_succeeded_llm_no_update",
            "BEFORE UPDATE",
            "evidence_items",
            f"({_SUCCEEDED_EVIDENCE_OLD}) OR ({_SUCCEEDED_EVIDENCE_NEW_NAMESPACE})",
        ),
    ),
    (
        "trg_evidence_items_succeeded_llm_no_delete",
        _trigger(
            "trg_evidence_items_succeeded_llm_no_delete",
            "BEFORE DELETE",
            "evidence_items",
            _SUCCEEDED_EVIDENCE_OLD,
        ),
    ),
)


# TheSuperHackers @feature Leex 22/08/2026 Freeze successful local-LLM graphs for every downstream reader. (#TBD)
def upgrade() -> None:
    for _name, ddl in _TRIGGERS:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")

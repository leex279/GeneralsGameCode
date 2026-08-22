"""Durable planning for deterministic and optional local-LLM replay analysis."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NoReturn, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import Job, JobDependency, Replay
from ..importing.jobs import JobCoordinator, JobSpec, StageFailure
from ..importing.service import _dependency_identity
from ..importing.stages import (
    ANALYZE_LLM,
    ANALYZE_LLM_VERSION,
    ASSESS_STRATEGIES,
    ASSESS_STRATEGIES_VERSION,
    DERIVE_FEATURES,
    DERIVE_FEATURES_VERSION,
    IMPORT_OBSERVATIONS,
    IMPORT_OBSERVATIONS_VERSION,
    PARSE,
    PARSE_VERSION,
    RENDER_REPORT,
    RENDER_REPORT_VERSION,
    TELEMETRY,
    TELEMETRY_VERSION,
    content_key,
    input_digest,
)

_PLAN_VERSION = 1
_DEPENDENCY_ORDER = {PARSE: 0, TELEMETRY: 1}
_DEPENDENCY_VERSIONS = {PARSE: PARSE_VERSION, TELEMETRY: TELEMETRY_VERSION}


class AnalysisPlanningError(RuntimeError):
    """Fail-closed durable planning error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _canonical_uuid(value: str, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a canonical lowercase UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical lowercase UUID")


# TheSuperHackers @feature Leex 22/08/2026 Expose a locator-free receipt for one durable analysis graph. (#TBD)
@dataclass(frozen=True, slots=True)
class AnalysisPlanDTO:
    status: Literal["awaiting_observations", "planned"]
    replay_public_id: str
    allow_ollama: bool
    derive_job_public_id: str | None = None
    assess_job_public_id: str | None = None
    llm_job_public_id: str | None = None
    report_job_public_id: str | None = None

    def __post_init__(self) -> None:
        _canonical_uuid(self.replay_public_id, "replay_public_id")
        if type(self.allow_ollama) is not bool:
            raise TypeError("allow_ollama must be an exact boolean")
        identifiers = (
            self.derive_job_public_id,
            self.assess_job_public_id,
            self.llm_job_public_id,
            self.report_job_public_id,
        )
        if self.status == "awaiting_observations":
            if any(identifier is not None for identifier in identifiers):
                raise ValueError("awaiting plans cannot expose job identities")
            return
        if self.status != "planned":
            raise ValueError("analysis plan status is invalid")
        required = (self.derive_job_public_id, self.assess_job_public_id, self.report_job_public_id)
        if any(identifier is None for identifier in required):
            raise ValueError("planned analysis requires deterministic and report job identities")
        if (self.llm_job_public_id is not None) is not self.allow_ollama:
            raise ValueError("LLM job identity must match the exact opt-in")
        for identifier in identifiers:
            if identifier is not None:
                _canonical_uuid(identifier, "job_public_id")


@dataclass(frozen=True, slots=True)
class _ObservationAuthority:
    job: Job
    selected_dependency_digest: str


@dataclass(frozen=True, slots=True)
class _SelectedBranch:
    import_mode: str
    parse_identity: Mapping[str, Any]
    telemetry_identity: Mapping[str, Any] | None


# TheSuperHackers @feature Leex 22/08/2026 Plan one exact observation-bound analytics graph transactionally. (#TBD)
class AnalysisPlanner:
    """Create or reuse the exact downstream graph authorized by immutable observations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._jobs = JobCoordinator(session_factory, clock=clock)

    def ensure_analysis_plan(self, replay_public_id: str, allow_ollama: bool) -> AnalysisPlanDTO:
        _canonical_uuid(replay_public_id, "replay_public_id")
        if type(allow_ollama) is not bool:
            raise TypeError("allow_ollama must be an exact boolean")
        with self._session_factory.begin() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == replay_public_id))
            if replay is None:
                raise AnalysisPlanningError("unknown_replay", "unknown replay public ID")
            authority = self._observation_authority(session, replay)
            if authority is None:
                return AnalysisPlanDTO("awaiting_observations", replay.public_id, allow_ollama)
            derive = self._ensure_derive(session, replay, authority)
            assess = self._ensure_assess(session, replay, authority, derive)
            llm = self._ensure_llm(session, replay, authority, assess) if allow_ollama else None
            report = self._ensure_report(session, replay, authority, assess, llm)
            return AnalysisPlanDTO(
                "planned",
                replay.public_id,
                allow_ollama,
                derive.public_id,
                assess.public_id,
                llm.public_id if llm is not None else None,
                report.public_id,
            )

    def _observation_authority(
        self, session: Session, replay: Replay
    ) -> _ObservationAuthority | None:
        candidates = list(
            session.scalars(
                select(Job)
                .where(
                    Job.replay_id == replay.id,
                    Job.stage == IMPORT_OBSERVATIONS,
                    Job.component_version == IMPORT_OBSERVATIONS_VERSION,
                    Job.status == "succeeded",
                )
                .order_by(Job.id)
            )
        )
        if not candidates:
            return None
        authorities = tuple(self._validate_authority(session, replay, candidate) for candidate in candidates)
        if len(authorities) != 1:
            raise AnalysisPlanningError(
                "ambiguous_observation_graph",
                "multiple succeeded dependency-bound observation graphs are authoritative",
            )
        return authorities[0]

    def _validate_authority(
        self, session: Session, replay: Replay, candidate: Job
    ) -> _ObservationAuthority:
        input_json = candidate.input_json
        output_json = candidate.output_json
        if not isinstance(input_json, Mapping) or not isinstance(output_json, Mapping):
            self._invalid_authority("succeeded observation job has invalid canonical data")
        replay_identity = input_json.get("replay_public_id")
        replay_sha256 = input_json.get("replay_sha256")
        branch_recipe = input_json.get("branch_recipe")
        selected_digest = input_json.get("selected_dependency_digest")
        if (
            replay_identity != replay.public_id
            or replay_sha256 != replay.sha256
            or input_json.get("dependency_identity_bound") is not True
            or not isinstance(branch_recipe, Mapping)
            or not _is_sha256(selected_digest)
        ):
            self._invalid_authority("succeeded observation job is not exactly dependency-bound")
        provisional_key = content_key(
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            replay.sha256,
            dict(branch_recipe),
        )
        if input_json.get("provisional_idempotency_key") != provisional_key:
            self._invalid_authority("observation branch recipe does not match its provisional identity")
        selected_branch = self._selected_branch(branch_recipe)
        dependencies = list(
            session.scalars(
                select(Job)
                .join(JobDependency, Job.id == JobDependency.depends_on_job_id)
                .where(JobDependency.job_id == candidate.id)
            )
        )
        dependencies.sort(
            key=lambda dependency: (
                _DEPENDENCY_ORDER.get(dependency.stage, len(_DEPENDENCY_ORDER)),
                dependency.component_version,
                dependency.public_id,
            )
        )
        stages = tuple(dependency.stage for dependency in dependencies)
        if not dependencies or PARSE not in stages or len(stages) != len(set(stages)):
            self._invalid_authority("observation graph requires one exact parser dependency")
        expected_stages = (
            {PARSE, TELEMETRY}
            if selected_branch.telemetry_identity is not None
            else {PARSE}
        )
        if set(stages) != expected_stages:
            self._invalid_authority("observation dependencies do not match the selected branch recipe")
        if any(
            dependency.replay_id != replay.id
            or dependency.stage not in _DEPENDENCY_VERSIONS
            or dependency.component_version != _DEPENDENCY_VERSIONS[dependency.stage]
            for dependency in dependencies
        ):
            self._invalid_authority("observation graph has an unexpected direct dependency")
        selected_dependencies = {dependency.stage: dependency for dependency in dependencies}
        selected_parse = selected_dependencies[PARSE]
        expected_stage_input = {
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "import_mode": selected_branch.import_mode,
        }
        if (
            selected_parse.idempotency_key
            != content_key(
                PARSE,
                PARSE_VERSION,
                replay.sha256,
                selected_branch.parse_identity,
            )
            or selected_parse.input_json != expected_stage_input
        ):
            self._invalid_authority("selected parser does not match the production branch recipe")
        selected_telemetry = selected_dependencies.get(TELEMETRY)
        if selected_branch.telemetry_identity is not None:
            assert selected_telemetry is not None
            if (
                selected_telemetry.idempotency_key
                != content_key(
                    TELEMETRY,
                    TELEMETRY_VERSION,
                    replay.sha256,
                    selected_branch.telemetry_identity,
                )
                or selected_telemetry.input_json != expected_stage_input
            ):
                self._invalid_authority("selected telemetry does not match the production branch recipe")
            telemetry_parent_ids = set(
                session.scalars(
                    select(JobDependency.depends_on_job_id).where(
                        JobDependency.job_id == selected_telemetry.id
                    )
                )
            )
            if telemetry_parent_ids != {selected_parse.id}:
                self._invalid_authority("selected telemetry is not bound to the selected parser")
        try:
            dependency_identities = [_dependency_identity(dependency) for dependency in dependencies]
        except (TypeError, ValueError, StageFailure) as error:
            raise AnalysisPlanningError(
                "invalid_observation_graph",
                "observation graph dependency identity is invalid",
            ) from error
        selected_identity = {
            "replay_sha256": replay.sha256,
            "branch_recipe": dict(branch_recipe),
            "dependencies": dependency_identities,
        }
        expected_digest = input_digest(selected_identity)
        expected_key = content_key(
            IMPORT_OBSERVATIONS,
            IMPORT_OBSERVATIONS_VERSION,
            replay.sha256,
            selected_identity,
        )
        if selected_digest != expected_digest or candidate.idempotency_key != expected_key:
            self._invalid_authority("observation graph materialization identity does not match its dependencies")
        return _ObservationAuthority(candidate, cast(str, selected_digest))

    def _selected_branch(self, branch_recipe: Mapping[str, Any]) -> _SelectedBranch:
        if set(branch_recipe) != {
            "import_observations_version",
            "import_mode",
            "parse",
            "telemetry",
        }:
            self._invalid_authority("observation branch recipe does not use the exact production schema")
        import_mode = branch_recipe.get("import_mode")
        if (
            branch_recipe.get("import_observations_version") != IMPORT_OBSERVATIONS_VERSION
            or import_mode not in {"copy", "reference"}
        ):
            self._invalid_authority("observation branch recipe has invalid production values")
        parse_identity = branch_recipe.get("parse")
        if (
            not isinstance(parse_identity, Mapping)
            or set(parse_identity) != {"import_mode", "parse_version", "parser_version"}
            or parse_identity.get("import_mode") != import_mode
            or parse_identity.get("parse_version") != PARSE_VERSION
            or type(parse_identity.get("parser_version")) is not str
            or not parse_identity.get("parser_version")
        ):
            self._invalid_authority("observation parser recipe is invalid")
        telemetry_identity = branch_recipe.get("telemetry")
        if telemetry_identity is not None and (
            not isinstance(telemetry_identity, Mapping)
            or set(telemetry_identity)
            != {"acquirer_version", "import_mode", "telemetry_version"}
            or telemetry_identity.get("import_mode") != import_mode
            or telemetry_identity.get("telemetry_version") != TELEMETRY_VERSION
            or type(telemetry_identity.get("acquirer_version")) is not str
            or not telemetry_identity.get("acquirer_version")
        ):
            self._invalid_authority("observation telemetry recipe is invalid")
        return _SelectedBranch(
            cast(str, import_mode),
            cast(Mapping[str, Any], parse_identity),
            cast(Mapping[str, Any] | None, telemetry_identity),
        )

    @staticmethod
    def _invalid_authority(message: str) -> NoReturn:
        raise AnalysisPlanningError("invalid_observation_graph", message)

    def _ensure_derive(
        self,
        session: Session,
        replay: Replay,
        authority: _ObservationAuthority,
    ) -> Job:
        identity = self._base_identity(authority)
        input_json = self._base_input(replay, authority)
        row = self._ensure_job(
            session,
            replay,
            DERIVE_FEATURES,
            DERIVE_FEATURES_VERSION,
            identity,
            input_json,
        )
        self._jobs.ensure_dependency(session, row.id, authority.job.id)
        return row

    def _ensure_assess(
        self,
        session: Session,
        replay: Replay,
        authority: _ObservationAuthority,
        derive: Job,
    ) -> Job:
        identity = {**self._base_identity(authority), "derive_job_key": derive.idempotency_key}
        input_json = {**self._base_input(replay, authority), "derive_input_digest": input_digest(identity)}
        row = self._ensure_job(
            session,
            replay,
            ASSESS_STRATEGIES,
            ASSESS_STRATEGIES_VERSION,
            identity,
            input_json,
        )
        self._jobs.ensure_dependency(session, row.id, derive.id)
        return row

    def _ensure_llm(
        self,
        session: Session,
        replay: Replay,
        authority: _ObservationAuthority,
        assess: Job,
    ) -> Job:
        identity = {
            **self._base_identity(authority),
            "assess_job_key": assess.idempotency_key,
            "provider_mode": "ollama",
        }
        input_json = {
            **self._base_input(replay, authority),
            "allow_ollama": True,
            "assess_input_digest": input_digest(identity),
        }
        row = self._ensure_job(
            session,
            replay,
            ANALYZE_LLM,
            ANALYZE_LLM_VERSION,
            identity,
            input_json,
        )
        self._jobs.ensure_dependency(session, row.id, assess.id)
        return row

    def _ensure_report(
        self,
        session: Session,
        replay: Replay,
        authority: _ObservationAuthority,
        assess: Job,
        llm: Job | None,
    ) -> Job:
        allow_ollama = llm is not None
        identity = {
            **self._base_identity(authority),
            "allow_ollama": allow_ollama,
            "assess_job_key": assess.idempotency_key,
            "llm_job_key": llm.idempotency_key if llm is not None else None,
        }
        input_json = {
            **self._base_input(replay, authority),
            "allow_ollama": allow_ollama,
            "report_input_digest": input_digest(identity),
        }
        row = self._ensure_job(
            session,
            replay,
            RENDER_REPORT,
            RENDER_REPORT_VERSION,
            identity,
            input_json,
        )
        self._jobs.ensure_dependency(session, row.id, llm.id if llm is not None else assess.id)
        return row

    def _ensure_job(
        self,
        session: Session,
        replay: Replay,
        stage: str,
        version: str,
        identity: Mapping[str, Any],
        input_json: Mapping[str, Any],
    ) -> Job:
        key = content_key(stage, version, replay.sha256, identity)
        row = self._jobs.ensure_job(
            session,
            JobSpec(stage, version, key, input_json, replay.id),
        )
        if (
            row.replay_id != replay.id
            or row.stage != stage
            or row.component_version != version
            or row.idempotency_key != key
            or row.input_json != dict(input_json)
        ):
            raise AnalysisPlanningError(
                "analysis_job_identity_conflict",
                "an existing job does not match the exact analysis plan identity",
            )
        return row

    @staticmethod
    def _base_identity(authority: _ObservationAuthority) -> dict[str, Any]:
        return {
            "analysis_plan_version": _PLAN_VERSION,
            "observation_job_key": authority.job.idempotency_key,
            "selected_dependency_digest": authority.selected_dependency_digest,
        }

    @staticmethod
    def _base_input(replay: Replay, authority: _ObservationAuthority) -> dict[str, Any]:
        return {
            "analysis_plan_version": _PLAN_VERSION,
            "replay_public_id": replay.public_id,
            "replay_sha256": replay.sha256,
            "selected_dependency_digest": authority.selected_dependency_digest,
        }


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )

"""Pure immutable feature fixtures."""

from collections.abc import Callable

import pytest

from generals_replay_analyzer.features.base import FeatureScope
from generals_replay_analyzer.features.context import FeatureContext
from generals_replay_analyzer.features.evidence import EvidenceRef, ObservedEvidence


@pytest.fixture
def observed() -> Callable[..., ObservedEvidence]:
    def factory(
        *,
        public_id: str = "00000000-0000-4000-8000-000000000201",
        source_key: str = "telemetry:fixture:1",
        frame: int | None = 10,
        event_type: str = "fixture",
        facts: object = (),
    ) -> ObservedEvidence:
        return ObservedEvidence(
            ref=EvidenceRef(
                public_id=public_id,
                tier="observed",
                source_kind="telemetry",
                source_key=source_key,
                schema_version="telemetry-v2",
            ),
            frame=frame,
            event_type=event_type,
            facts=facts,
        )

    return factory


@pytest.fixture
def player_context() -> Callable[..., FeatureContext]:
    def factory(
        *items: ObservedEvidence,
        telemetry_status: str | None = "succeeded",
        final_frame: int | None = 300,
        logic_frames_per_second: int | None = 30,
        catalog_identity: str | None = "catalog-v1:fixture",
        settings: object = (),
    ) -> FeatureContext:
        player = "00000000-0000-4000-8000-000000000250"
        return FeatureContext(
            cache_schema="feature-context-v1",
            replay_public_id="00000000-0000-4000-8000-000000000249",
            replay_sha256="a" * 64,
            replay_player_public_id=player,
            scope=FeatureScope("player", player, player),
            observation_schema_versions=(("telemetry", "telemetry-v2"),),
            parser_completion_status="complete",
            telemetry_status=telemetry_status,
            final_frame=final_frame,
            logic_frames_per_second=logic_frames_per_second,
            catalog_identity=catalog_identity,
            observed=items,
            settings=settings,
        )

    return factory

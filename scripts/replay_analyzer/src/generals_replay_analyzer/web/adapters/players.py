"""Production adapters for player history, identity workflow, and comparisons."""

from __future__ import annotations

from typing import Literal, cast

from generals_replay_analyzer.comparison.service import (
    ComparisonDefinition,
    ComparisonFilters,
    ComparisonSelection,
    ComparisonValue,
    FixedComparisonQuery,
    LongitudinalSubject,
    MatchSubject,
    ReplayComparisonService,
)
from generals_replay_analyzer.identity.query import (
    Availability,
    DefinitionBinding,
    FixedPlayerProfileQuery,
    FixedReportReference,
    PlayerIndexQuery,
    PlayerProfileSelection,
    PlayerQueryService,
)
from generals_replay_analyzer.identity.service import (
    IdentityBusyError,
    IdentityConflictError,
    IdentityInvariantError,
    IdentityNotFoundError,
)
from generals_replay_analyzer.identity.workflow import (
    IdentityDraft,
    IdentityExecutionCommand,
    IdentityOperationSummary,
    InvalidationJobReference,
    InverseIdentityDraft,
    MergeIdentityDraft,
    PlayerIdentityWorkflowService,
    RevisionPrecondition,
    SplitIdentityDraft,
)
from generals_replay_analyzer.presentation import map_label
from generals_replay_analyzer.web.errors import PublicProblem
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ComparisonDTO,
    ComparisonMetricDTO,
    ComparisonResolutionDTO,
    ComparisonSelectionDTO,
    ComparisonSubjectDTO,
    ComparisonValueDTO,
    ComparisonVersionDTO,
    DefinitionBindingDTO,
    DistributionIntervalDTO,
    EmbeddedAliasDTO,
    ExecuteIdentityChangeDTO,
    FixedComparisonQueryDTO,
    FixedReportReferenceDTO,
    IdentityAuditPageDTO,
    IdentityImpactDTO,
    IdentityMutationReceiptDTO,
    IdentityOperationSummaryDTO,
    IdentityPreviewDTO,
    InvalidationJobReferenceDTO,
    InverseIdentityDraftDTO,
    LongitudinalBindingDTO,
    MatchSubjectDTO,
    MergeIdentityDraftDTO,
    OpeningSubjectDTO,
    PlayerCohortSubjectDTO,
    PlayerIndexPageDTO,
    PlayerIndexQueryDTO,
    PlayerInsightDTO,
    PlayerProfileDTO,
    PlayerProfileQueryDTO,
    PlayerProfileResolutionDTO,
    PlayerProfileSelectionDTO,
    PlayerProfileVersionDTO,
    PlayerSummaryDTO,
    ProviderIdentityDTO,
    PublicEvidenceReferenceDTO,
    ReplayHistoryItemDTO,
    RevisionPreconditionDTO,
    SegmentBaselineSubjectDTO,
    SplitIdentityDraftDTO,
    StrataProvenanceDTO,
    StrategySubjectDTO,
    TerminalQualityDTO,
    TimePeriodSubjectDTO,
)


def _availability(value: Availability) -> AvailabilityDTO:
    return AvailabilityDTO(state=value.state, reason_codes=value.reason_codes)


def _definition(value: DefinitionBinding) -> DefinitionBindingDTO:
    return DefinitionBindingDTO(
        definition_kind=value.definition_kind,
        definition_id=value.definition_id,
        definition_version=value.definition_version,
        unit=value.unit,
        scope_type=value.scope_type,
        window_policy_version=value.window_policy_version,
        faction_comparability=value.faction_comparability,
        taxonomy_version=value.taxonomy_version,
    )


def _report(value: FixedReportReference) -> FixedReportReferenceDTO:
    return FixedReportReferenceDTO(
        replay_public_id=value.replay_public_id,
        replay_player_public_id=value.replay_player_public_id,
        report_public_id=value.report_public_id,
        document_schema_version=value.document_schema_version,
        report_version=value.report_version,
        display_policy_version=value.display_policy_version,
        input_digest=value.input_digest,
    )


def _revision_values(values: tuple[RevisionPreconditionDTO, ...]) -> tuple[RevisionPrecondition, ...]:
    return tuple(RevisionPrecondition(item.player_public_id, item.expected_revision) for item in values)


def _domain_draft(value: object) -> MergeIdentityDraft | SplitIdentityDraft | InverseIdentityDraft:
    if isinstance(value, MergeIdentityDraftDTO):
        return MergeIdentityDraft(
            value.target_player_public_id, value.source_player_public_ids, _revision_values(value.expected_revisions)
        )
    if isinstance(value, SplitIdentityDraftDTO):
        return SplitIdentityDraft(
            value.alias_public_id,
            value.replay_player_public_ids,
            value.new_display_name,
            _revision_values(value.expected_revisions),
        )
    if isinstance(value, InverseIdentityDraftDTO):
        return InverseIdentityDraft(value.operation_public_id, _revision_values(value.expected_revisions))
    raise TypeError("unsupported identity draft")


def _web_draft(value: IdentityDraft) -> MergeIdentityDraftDTO | SplitIdentityDraftDTO | InverseIdentityDraftDTO:
    revisions = tuple(
        RevisionPreconditionDTO(player_public_id=item.player_public_id, expected_revision=item.expected_revision)
        for item in value.expected_revisions
    )
    if isinstance(value, MergeIdentityDraft):
        return MergeIdentityDraftDTO(
            operation_kind="merge_players",
            target_player_public_id=value.target_player_public_id,
            source_player_public_ids=value.source_player_public_ids,
            expected_revisions=revisions,
        )
    if isinstance(value, SplitIdentityDraft):
        return SplitIdentityDraftDTO(
            operation_kind="split_alias",
            alias_public_id=value.alias_public_id,
            replay_player_public_ids=value.replay_player_public_ids,
            new_display_name=value.new_display_name,
            expected_revisions=revisions,
        )
    assert isinstance(value, InverseIdentityDraft)
    return InverseIdentityDraftDTO(
        operation_kind="inverse", operation_public_id=value.operation_public_id, expected_revisions=revisions
    )


# TheSuperHackers @feature Leex 23/08/2026 Keep ORM identities and analytical formulas behind frozen player web DTOs. (#TBD)
class AnalyticsPlayersAdapter:
    def __init__(
        self,
        players: PlayerQueryService,
        identity: PlayerIdentityWorkflowService,
        comparisons: ReplayComparisonService,
        *,
        minimum_sample_size: int = 3,
    ) -> None:
        self._players = players
        self._identity = identity
        self._comparisons = comparisons
        self._minimum_sample_size = minimum_sample_size

    def list_players(self, query: PlayerIndexQueryDTO) -> PlayerIndexPageDTO:
        domain_query = PlayerIndexQuery(**query.model_dump())
        page = self._players.list_players(domain_query)
        return PlayerIndexPageDTO(
            query=query,
            items=tuple(
                PlayerSummaryDTO(
                    player_public_id=item.player_public_id,
                    display_name=item.display_name,
                    identity_revision=item.identity_revision,
                    state=item.state,
                    match_count=item.match_count,
                    latest_match_at_utc=item.latest_match_at_utc,
                    availability=_availability(item.availability),
                    external_profile_url=getattr(item, "external_profile_url", None),
                    external_profile_source=getattr(item, "external_profile_source", None),
                )
                for item in page.items
            ),
            page=query.page,
            page_size=query.page_size,
            total_items=page.total_items,
            availability=_availability(page.availability),
        )

    def resolve_profile(self, selection: PlayerProfileSelectionDTO) -> PlayerProfileResolutionDTO:
        result = self._players.resolve_profile(PlayerProfileSelection(**selection.model_dump()))
        return PlayerProfileResolutionDTO(
            state=result.state,
            fixed_query=None
            if result.fixed_query is None
            else PlayerProfileQueryDTO(
                **{name: getattr(result.fixed_query, name) for name in result.fixed_query.__dataclass_fields__}
            ),
            reason_codes=result.reason_codes,
        )

    def get_profile(self, query: PlayerProfileQueryDTO) -> PlayerProfileDTO:
        try:
            profile = self._players.get_profile(FixedPlayerProfileQuery(**query.model_dump()))
        except LookupError as error:
            raise PublicProblem(status=404, code="player_not_found", detail="Player was not found") from error
        except ValueError as error:
            raise PublicProblem(
                status=409, code="player_profile_binding_conflict", detail="Player profile bindings changed"
            ) from error
        bindings = tuple(
            LongitudinalBindingDTO(
                run_id=item.run_id,
                player_public_id=item.player_public_id,
                identity_revision=item.identity_revision,
                analyzer_name=item.analyzer_name,
                analyzer_version=item.analyzer_version,
                segment_schema_version="longitudinal-segment-v1",
                segment_digest=item.segment_digest,
                quality_policy_digest=item.quality_policy_digest,
                input_digest=item.input_digest,
                cache_key=item.cache_key,
                statistics_algorithm_versions=item.statistics_algorithm_versions,
            )
            for item in profile.longitudinal
        )
        reports = tuple(_report(item) for item in profile.fixed_reports)
        definitions = tuple(_definition(item) for item in profile.definition_bindings)
        return PlayerProfileDTO(
            version=PlayerProfileVersionDTO(
                schema_version="replay-player-profile-v1",
                display_policy_version="replay-player-profile-display-v1",
                profile_public_id=profile.profile_public_id,
                player_public_id=profile.player.player_public_id,
                identity_revision=profile.player.identity_revision,
                longitudinal=bindings,
                fixed_reports=reports,
                definition_bindings=definitions,
                input_digest=profile.input_digest,
            ),
            query=query,
            player=PlayerSummaryDTO(
                player_public_id=profile.player.player_public_id,
                display_name=profile.player.display_name,
                identity_revision=profile.player.identity_revision,
                state=profile.player.state,
                match_count=profile.player.match_count,
                latest_match_at_utc=profile.player.latest_match_at_utc,
                availability=_availability(profile.player.availability),
                external_profile_url=getattr(profile.player, "external_profile_url", None),
                external_profile_source=getattr(profile.player, "external_profile_source", None),
            ),
            embedded_aliases=tuple(
                EmbeddedAliasDTO(
                    alias_public_id=item.alias_public_id,
                    namespace="embedded_replay_name",
                    original_name=item.original_name,
                    normalized_name=item.normalized_name,
                )
                for item in profile.embedded_aliases
            ),
            provider_identities=tuple(
                ProviderIdentityDTO(
                    alias_public_id=item.alias_public_id,
                    provider_namespace=item.provider_namespace,
                    external_subject=item.external_subject,
                    attachment_operation_public_id=item.attachment_operation_public_id,
                    label="manually_attached_provider_identity",
                )
                for item in profile.provider_identities
            ),
            strata_provenance=tuple(
                StrataProvenanceDTO(
                    source_public_id=item.source_public_id,
                    replay_public_id=item.replay_public_id,
                    strata_match_id=item.strata_match_id,
                    strata_source_user_token=item.strata_source_user_token,
                    label="provenance_not_identity",
                    availability=_availability(item.availability),
                )
                for item in profile.strata_provenance
            ),
            replay_history=tuple(
                ReplayHistoryItemDTO(
                    replay_public_id=item.replay_public_id,
                    replay_player_public_id=item.replay_player_public_id,
                    observed_name=item.observed_name,
                    original_name=getattr(item, "original_name", None),
                    faction=item.faction,
                    opponent_factions=item.opponent_factions,
                    opponent_player_public_ids=item.opponent_player_public_ids,
                    map_public_id=item.map_public_id,
                    map_display_name=map_label(item.map_display_name),
                    patch=item.patch,
                    start_position=item.start_position,
                    result=item.result,
                    started_at_utc=item.started_at_utc,
                    terminal_quality=TerminalQualityDTO(lifecycle=item.lifecycle_state),
                    fixed_report=None if item.fixed_report is None else _report(item.fixed_report),
                    availability=_availability(item.availability),
                )
                for item in profile.history
            ),
            history_page=query.page,
            history_page_size=query.page_size,
            history_total_items=profile.history_total_items,
            engine_verified_history_count=profile.engine_verified_history_count,
            insights=tuple(
                PlayerInsightDTO(
                    insight_kind=cast(object, item.insight_kind),  # type: ignore[arg-type]
                    result_public_id=item.result_public_id,
                    definition=_definition(item.definition),
                    label=item.label,
                    raw_value=item.raw_value,
                    unit=item.unit,
                    frame_start=item.frame_start,
                    frame_end=item.frame_end,
                    sample_count=item.sample_count,
                    missing_count=item.missing_count,
                    interval=None
                    if item.interval is None
                    else DistributionIntervalDTO(
                        lower=item.interval.lower,
                        upper=item.interval.upper,
                        confidence_level=item.interval.confidence_level,
                        method=item.interval.method,
                        algorithm_version=item.interval.algorithm_version,
                    ),
                    quality_exclusion_codes=item.quality_exclusion_codes,
                    availability=_availability(item.availability),
                    evidence=tuple(
                        PublicEvidenceReferenceDTO(evidence_public_id=value, tier="derived")
                        for value in item.evidence_public_ids
                    ),
                )
                for item in profile.insights
            ),
            availability=_availability(profile.availability),
        )

    def audit(self, player_public_id: str, page: int, page_size: int) -> IdentityAuditPageDTO:
        result = self._identity.audit(player_public_id, page, page_size)
        return IdentityAuditPageDTO(
            player_public_id=result.player_public_id,
            current_identity_revision=result.current_identity_revision,
            operations=tuple(self._operation(item) for item in result.operations),
            page=result.page,
            page_size=result.page_size,
            total_items=result.total_items,
            availability=AvailabilityDTO(
                state="available" if result.available else "unavailable", reason_codes=result.reason_codes
            ),
        )

    def preview(
        self, draft: MergeIdentityDraftDTO | SplitIdentityDraftDTO | InverseIdentityDraftDTO
    ) -> IdentityPreviewDTO:
        result = self._identity.preview(_domain_draft(draft))
        impact = result.impact
        return IdentityPreviewDTO(
            schema_version="player-identity-preview-v1",
            draft=_web_draft(result.draft),
            before_snapshot_digest=result.before_snapshot_digest,
            expected_after_snapshot_digest=result.expected_after_snapshot_digest,
            impact=IdentityImpactDTO(
                canonical_player_count=impact.canonical_player_count,
                alias_count=impact.alias_count,
                replay_player_count=impact.replay_player_count,
                replay_count=impact.replay_count,
                feature_set_count=impact.feature_set_count,
                longitudinal_run_count=impact.longitudinal_run_count,
                longitudinal_result_count=impact.longitudinal_result_count,
                report_count=impact.report_count,
                invalidation_stage_counts=impact.invalidation_stage_counts,
            ),
            can_execute=result.can_execute,
            reason_codes=result.reason_codes,
        )

    def execute(self, command: ExecuteIdentityChangeDTO) -> IdentityMutationReceiptDTO:
        try:
            result = self._identity.execute(
                IdentityExecutionCommand(
                    _domain_draft(command.draft),
                    command.expected_before_snapshot_digest,
                    command.operator_label,
                    command.reason,
                )
            )
        except IdentityNotFoundError as error:
            raise PublicProblem(
                status=404, code="identity_subject_not_found", detail="Identity subject was not found"
            ) from error
        except IdentityConflictError as error:
            raise PublicProblem(
                status=409, code="identity_revision_conflict", detail="Identity state changed after preview"
            ) from error
        except IdentityInvariantError as error:
            raise PublicProblem(
                status=422, code="identity_change_invalid", detail="Identity change is invalid"
            ) from error
        except IdentityBusyError as error:
            raise PublicProblem(status=503, code="identity_service_busy", detail="Identity service is busy") from error
        return IdentityMutationReceiptDTO(
            operation=self._operation(result.operation),
            invalidation_jobs=self._invalidation_jobs(result.invalidation_jobs),
            audit_public_id=result.audit_public_id,
        )

    def retry_invalidation(self, operation_public_id: str) -> tuple[InvalidationJobReferenceDTO, ...]:
        try:
            result = self._identity.retry_invalidation(operation_public_id)
        except IdentityNotFoundError as error:
            raise PublicProblem(
                status=404, code="identity_operation_not_found", detail="Identity operation was not found"
            ) from error
        return self._invalidation_jobs(result)

    @staticmethod
    def _invalidation_jobs(
        value: tuple[InvalidationJobReference, ...],
    ) -> tuple[InvalidationJobReferenceDTO, ...]:
        return tuple(
            InvalidationJobReferenceDTO(
                job_public_id=item.job_public_id,
                stage=item.stage,
                state=item.state,
                reason_code=item.reason_code,
            )
            for item in value
        )

    @staticmethod
    def _operation(value: IdentityOperationSummary) -> IdentityOperationSummaryDTO:
        return IdentityOperationSummaryDTO(
            operation_public_id=value.operation_public_id,
            operation_kind=value.operation_kind,
            inverse_of_operation_public_id=value.inverse_of_operation_public_id,
            operator_label=value.operator_label,
            reason=value.reason,
            created_at_utc=value.created_at_utc,
            affected_player_revisions=tuple(
                RevisionPreconditionDTO(
                    player_public_id=item.player_public_id, expected_revision=item.expected_revision
                )
                for item in value.affected_player_revisions
            ),
            inverse_allowed=value.inverse_allowed,
            inverse_reason_code=value.inverse_reason_code,
        )

    def resolve(self, selection: ComparisonSelectionDTO) -> ComparisonResolutionDTO:
        filters = selection.filters
        result = self._comparisons.resolve(
            ComparisonSelection(
                selection.kind,
                selection.left_public_id,
                selection.right_public_id,
                selection.baseline_requested,
                selection.metric_definition_ids,
                self._minimum_sample_size,
                ComparisonFilters(**filters.model_dump()),
            )
        )
        if result.fixed_query is None:
            return ComparisonResolutionDTO(state="unavailable", reason_codes=result.reason_codes)
        fixed = result.fixed_query
        return ComparisonResolutionDTO(
            state="resolved",
            fixed_query=FixedComparisonQueryDTO(
                schema_version="replay-comparison-query-v1",
                kind=fixed.kind,
                left=self._web_subject(fixed.left),
                right=self._web_subject(fixed.right),
                metric_definition_ids=tuple(item.definition_id for item in fixed.metric_definitions),
                definition_bindings=tuple(self._web_comparison_definition(item) for item in fixed.metric_definitions),
                minimum_sample_size=fixed.minimum_sample_size,
            ),
        )

    @staticmethod
    def _web_comparison_definition(value: ComparisonDefinition) -> DefinitionBindingDTO:
        return DefinitionBindingDTO(
            definition_kind=value.definition_kind,
            definition_id=value.definition_id,
            definition_version=value.definition_version,
            unit=value.unit,
            scope_type=value.scope_type,
            window_policy_version=value.window_policy_version,
            faction_comparability=value.faction_comparability,
            taxonomy_version=value.taxonomy_version,
        )

    @staticmethod
    def _web_longitudinal(value: LongitudinalSubject) -> LongitudinalBindingDTO:
        return LongitudinalBindingDTO(
            run_id=value.run_id,
            player_public_id=value.player_public_id,
            identity_revision=value.identity_revision,
            analyzer_name=value.analyzer_name,
            analyzer_version=value.analyzer_version,
            segment_schema_version="longitudinal-segment-v1",
            segment_digest=value.segment_digest,
            quality_policy_digest=value.quality_policy_digest,
            input_digest=value.input_digest,
            cache_key=value.cache_key,
            statistics_algorithm_versions=value.statistics_algorithm_versions,
        )

    @classmethod
    def _web_subject(cls, value: LongitudinalSubject | MatchSubject) -> ComparisonSubjectDTO:
        if isinstance(value, MatchSubject):
            return MatchSubjectDTO(
                subject_kind="match",
                report=FixedReportReferenceDTO(
                    replay_public_id=value.replay_public_id,
                    replay_player_public_id=value.replay_player_public_id,
                    report_public_id=value.report_public_id,
                    document_schema_version=value.document_schema_version,
                    report_version=value.report_version,
                    display_policy_version=value.display_policy_version,
                    input_digest=value.report_input_digest,
                ),
                feature_set_public_ids=value.feature_set_public_ids,
            )
        longitudinal = cls._web_longitudinal(value)
        if value.subject_kind == "player_cohort":
            return PlayerCohortSubjectDTO(
                subject_kind="player_cohort",
                player_public_id=value.player_public_id,
                identity_revision=value.identity_revision,
                longitudinal=longitudinal,
            )
        if value.subject_kind == "segment_baseline":
            assert value.baseline_public_id is not None and value.population_definition_version is not None
            return SegmentBaselineSubjectDTO(
                subject_kind="segment_baseline",
                baseline_public_id=value.baseline_public_id,
                longitudinal=longitudinal,
                population_definition_version=value.population_definition_version,
            )
        if value.subject_kind == "opening":
            assert (
                value.result_public_id is not None
                and value.opening_definition_id is not None
                and value.opening_definition_version is not None
            )
            return OpeningSubjectDTO(
                subject_kind="opening",
                player_public_id=value.player_public_id,
                identity_revision=value.identity_revision,
                longitudinal=longitudinal,
                result_public_id=value.result_public_id,
                opening_definition_id=value.opening_definition_id,
                opening_definition_version=value.opening_definition_version,
            )
        if value.subject_kind == "strategy":
            assert (
                value.result_public_id is not None
                and value.strategy_id is not None
                and value.taxonomy_version is not None
                and value.rule_version is not None
            )
            return StrategySubjectDTO(
                subject_kind="strategy",
                player_public_id=value.player_public_id,
                identity_revision=value.identity_revision,
                longitudinal=longitudinal,
                result_public_id=value.result_public_id,
                strategy_id=value.strategy_id,
                taxonomy_version=value.taxonomy_version,
                rule_version=value.rule_version,
                method="rule",
            )
        assert value.start_inclusive_utc is not None and value.end_exclusive_utc is not None
        return TimePeriodSubjectDTO(
            subject_kind="time_period",
            player_public_id=value.player_public_id,
            identity_revision=value.identity_revision,
            start_inclusive_utc=value.start_inclusive_utc,
            end_exclusive_utc=value.end_exclusive_utc,
            longitudinal=longitudinal,
        )

    def compare(self, query: FixedComparisonQueryDTO) -> ComparisonDTO:
        if tuple(item.definition_id for item in query.definition_bindings) != query.metric_definition_ids:
            raise PublicProblem(
                status=409,
                code="comparison_binding_conflict",
                detail="Comparison bindings do not align",
            )
        try:
            domain_query = FixedComparisonQuery(
                query.kind,
                self._subject(query.left),
                self._subject(query.right),
                tuple(
                    ComparisonDefinition(
                        item.definition_id,
                        item.definition_version,
                        item.unit,
                        item.scope_type,
                        item.window_policy_version,
                        item.faction_comparability,
                        item.definition_kind,
                        item.taxonomy_version,
                    )
                    for item in query.definition_bindings
                ),
                query.minimum_sample_size,
            )
            result = self._comparisons.compare(domain_query)
        except ValueError as error:
            raise PublicProblem(
                status=409, code="comparison_binding_conflict", detail="Comparison bindings do not align"
            ) from error
        metrics = tuple(
            ComparisonMetricDTO(
                section=cast(
                    Literal["overview", "openings", "timings", "transitions", "strategy", "spatial", "trend"],
                    item.section,
                ),
                metric_id=item.metric_id,
                label=item.label,
                value_kind=cast(
                    Literal["scalar", "distribution", "categorical_share", "timing_band", "transition", "trend"],
                    item.value_kind,
                ),
                definition=_definition(cast(DefinitionBinding, item.value.definition)),
                left=self._comparison_value(item.value.left),
                right=self._comparison_value(item.value.right),
                derived_difference=item.value.derived_difference,
                difference_evidence=tuple(
                    PublicEvidenceReferenceDTO(evidence_public_id=value, tier="derived")
                    for value in item.value.difference_evidence_public_ids
                ),
                state=item.value.state,
                reason_codes=item.value.reason_codes,
            )
            for item in result.metrics
        )
        identity_bindings = tuple(
            sorted(
                {
                    (item.player_public_id, item.identity_revision)
                    for item in (query.left, query.right)
                    if hasattr(item, "player_public_id") and hasattr(item, "identity_revision")
                }
            )
        )
        longitudinal_bindings = tuple(
            item.longitudinal for item in (query.left, query.right) if hasattr(item, "longitudinal")
        )
        report_bindings = tuple(item.report for item in (query.left, query.right) if isinstance(item, MatchSubjectDTO))
        return ComparisonDTO(
            version=ComparisonVersionDTO(
                schema_version="replay-comparison-v1",
                comparison_definition_version="replay-comparison-definition-v1",
                display_policy_version="replay-comparison-display-v1",
                comparison_public_id=result.comparison_public_id,
                query_digest=result.query_digest,
                input_digest=result.input_digest,
                identity_bindings=identity_bindings,
                longitudinal_bindings=longitudinal_bindings,
                report_bindings=report_bindings,
                definition_bindings=query.definition_bindings,
            ),
            query=query,
            state=result.state,
            reason_codes=result.reason_codes,
            metrics=metrics,
            availability=AvailabilityDTO(
                state="unavailable"
                if result.state == "unavailable"
                else "partial"
                if result.state in {"partial", "not_comparable"}
                else "available",
                reason_codes=result.reason_codes,
            ),
        )

    @staticmethod
    def _comparison_value(value: ComparisonValue) -> ComparisonValueDTO:
        interval = value.interval
        return ComparisonValueDTO(
            raw_value=value.raw_value,
            unit=value.unit,
            sample_count=value.sample_count,
            missing_count=value.missing_count,
            interval=None
            if interval is None
            else DistributionIntervalDTO(
                lower=interval.lower,
                upper=interval.upper,
                confidence_level=interval.confidence_level,
                method=interval.method,
                algorithm_version=interval.algorithm_version,
            ),
            quality_exclusion_codes=value.quality_exclusion_codes,
            availability=AvailabilityDTO(
                state=value.availability,
                reason_codes=value.quality_exclusion_codes if value.availability != "available" else (),
            ),
            evidence=tuple(
                PublicEvidenceReferenceDTO(evidence_public_id=item, tier="derived")
                for item in value.evidence_public_ids
            ),
        )

    @staticmethod
    def _subject(value: object) -> LongitudinalSubject | MatchSubject:
        if isinstance(value, MatchSubjectDTO):
            return MatchSubject(
                "match",
                value.report.replay_public_id,
                value.report.replay_player_public_id,
                value.report.report_public_id,
                value.report.input_digest,
                value.feature_set_public_ids,
                value.report.document_schema_version,
                value.report.report_version,
                value.report.display_policy_version,
            )
        longitudinal = value.longitudinal  # type: ignore[attr-defined]
        if isinstance(
            value,
            (PlayerCohortSubjectDTO, OpeningSubjectDTO, StrategySubjectDTO, TimePeriodSubjectDTO),
        ) and (value.player_public_id, value.identity_revision) != (
            longitudinal.player_public_id,
            longitudinal.identity_revision,
        ):
            raise ValueError("subject identity must match its longitudinal binding")
        base = {
            "run_id": longitudinal.run_id,
            "player_public_id": longitudinal.player_public_id,
            "identity_revision": longitudinal.identity_revision,
            "analyzer_name": longitudinal.analyzer_name,
            "analyzer_version": longitudinal.analyzer_version,
            "segment_digest": longitudinal.segment_digest,
            "quality_policy_digest": longitudinal.quality_policy_digest,
            "input_digest": longitudinal.input_digest,
            "cache_key": longitudinal.cache_key,
            "statistics_algorithm_versions": longitudinal.statistics_algorithm_versions,
        }
        if isinstance(value, PlayerCohortSubjectDTO):
            return LongitudinalSubject("player_cohort", **base)
        if isinstance(value, SegmentBaselineSubjectDTO):
            return LongitudinalSubject(
                "segment_baseline",
                **base,
                baseline_public_id=value.baseline_public_id,
                population_definition_version=value.population_definition_version,
            )
        if isinstance(value, OpeningSubjectDTO):
            return LongitudinalSubject(
                "opening",
                **base,
                result_public_id=value.result_public_id,
                opening_definition_id=value.opening_definition_id,
                opening_definition_version=value.opening_definition_version,
            )
        if isinstance(value, StrategySubjectDTO):
            return LongitudinalSubject(
                "strategy",
                **base,
                result_public_id=value.result_public_id,
                taxonomy_version=value.taxonomy_version,
                rule_version=value.rule_version,
                strategy_id=value.strategy_id,
            )
        assert isinstance(value, TimePeriodSubjectDTO)
        return LongitudinalSubject(
            "time_period",
            **base,
            start_inclusive_utc=value.start_inclusive_utc,
            end_exclusive_utc=value.end_exclusive_utc,
        )

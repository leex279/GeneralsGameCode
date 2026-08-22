"""Transactional exact-match player identity operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.db.base import utc_now
from generals_replay_analyzer.db.models import (
    ParserRun,
    Player,
    PlayerAlias,
    PlayerIdentityOperation,
    Replay,
    ReplayPlayer,
    Source,
)
from generals_replay_analyzer.identity.audit import identity_cache_digest
from generals_replay_analyzer.identity.dto import IdentityDecision, IdentityOperationReceipt, IdentityResolutionBatch
from generals_replay_analyzer.identity.normalize import (
    EMBEDDED_REPLAY_NAME_NAMESPACE,
    EXTERNAL_NAMESPACE_PREFIX,
    InvalidPlayerNameError,
    normalize_embedded_name,
)

JSON = dict[str, Any] | list[Any]
PRESENCE_ABSENT = "absent"
PRESENCE_ACTIVE = "active"
PRESENCE_RETIRED = "retired"
PRESENCE_RETIRED_TOMBSTONE = "retired_tombstone"
PRESENCE_DETACHED_TOMBSTONE = "detached_tombstone"


class IdentityError(RuntimeError):
    """Base class for typed identity failures."""


class IdentityNotFoundError(IdentityError):
    """Raised when a stable public identity does not exist."""


class IdentityConflictError(IdentityError):
    """Raised when revisions or mappings changed after operator review."""


class IdentityBusyError(IdentityError):
    """Raised when SQLite cannot reserve the writer within its busy timeout."""


class IdentityInvariantError(IdentityError):
    """Raised when an identity request would violate a persistence invariant."""


def _is_busy(error: OperationalError) -> bool:
    message = str(error).casefold()
    return "database is locked" in message or "database is busy" in message


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityInvariantError(f"{label} must not be empty")
    return value


def _one_or_none(session: Session, statement: Select[tuple[Any]]) -> Any | None:
    return session.scalars(statement).one_or_none()


# TheSuperHackers @feature Leex 22/08/2026 Resolve and correct canonical players through revision-guarded audit transactions. (#TBD)
class PlayerIdentityService:
    """Resolve exact embedded names and perform reversible operator corrections."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        public_id_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._public_id_factory = public_id_factory or (lambda: str(uuid4()))
        self._now_factory = now_factory

    @contextmanager
    def _writer(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except OperationalError as error:
            session.rollback()
            if _is_busy(error):
                raise IdentityBusyError("identity database writer is busy") from error
            raise IdentityInvariantError("identity database operation failed") from error
        except IntegrityError as error:
            session.rollback()
            raise IdentityInvariantError("identity graph violates a database invariant") from error
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _player(self, session: Session, public_id: str) -> Player:
        player = _one_or_none(session, select(Player).where(Player.public_id == public_id))
        if player is None:
            raise IdentityNotFoundError(f"unknown player {public_id}")
        return cast(Player, player)

    def _revisions(self, players: list[Player]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((player.public_id, player.identity_revision) for player in players))

    def _validate_expected(
        self, players: list[Player], expected_revisions: Mapping[str, int], *, allow_new: set[str] | None = None
    ) -> None:
        allow_new = allow_new or set()
        required = {player.public_id for player in players if player.public_id not in allow_new}
        if set(expected_revisions) != required:
            raise IdentityConflictError("expected revisions must exactly cover every reviewed player")
        for player in players:
            if player.public_id not in allow_new and expected_revisions[player.public_id] != player.identity_revision:
                raise IdentityConflictError("player identity revision changed after review")

    def _snapshot(
        self,
        session: Session,
        *,
        operation_kind: str,
        player_ids: set[int],
        alias_ids: set[int] | None = None,
        replay_player_ids: set[int] | None = None,
        source_public_ids: tuple[str, ...] = (),
        absent_player_public_ids: tuple[str, ...] = (),
        absent_alias_public_ids: tuple[str, ...] = (),
        player_presence_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        player_presence_overrides = player_presence_overrides or {}
        players = session.scalars(select(Player).where(Player.id.in_(player_ids))).all() if player_ids else []
        if alias_ids is None:
            aliases = session.scalars(select(PlayerAlias).where(PlayerAlias.player_id.in_(player_ids))).all()
        elif alias_ids:
            aliases = session.scalars(select(PlayerAlias).where(PlayerAlias.id.in_(alias_ids))).all()
        else:
            aliases = []
        if replay_player_ids is None:
            replay_players = session.scalars(select(ReplayPlayer).where(ReplayPlayer.player_id.in_(player_ids))).all()
        elif replay_player_ids:
            replay_players = session.scalars(select(ReplayPlayer).where(ReplayPlayer.id.in_(replay_player_ids))).all()
        else:
            replay_players = []
        player_public = {player.id: player.public_id for player in session.scalars(select(Player)).all()}
        player_entries = [
            {
                "player_public_id": player.public_id,
                "identity_revision": player.identity_revision,
                "retired": player.retired_at is not None,
                "presence": player_presence_overrides.get(
                    player.public_id,
                    PRESENCE_RETIRED if player.retired_at is not None else PRESENCE_ACTIVE,
                ),
            }
            for player in players
        ]
        player_entries.extend(
            {"player_public_id": public_id, "presence": PRESENCE_ABSENT}
            for public_id in absent_player_public_ids
        )
        alias_entries = [
            {
                "alias_public_id": alias.public_id,
                "player_public_id": player_public[alias.player_id],
                "namespace": alias.namespace,
                "normalized_name": alias.normalized_name,
                "external_subject": alias.external_subject,
                "presence": (
                    PRESENCE_DETACHED_TOMBSTONE
                    if alias.namespace.startswith("external:detached:")
                    else PRESENCE_ACTIVE
                ),
            }
            for alias in aliases
        ]
        alias_entries.extend(
            {"alias_public_id": public_id, "presence": PRESENCE_ABSENT}
            for public_id in absent_alias_public_ids
        )
        return {
            "operation_kind": operation_kind,
            "players": sorted(player_entries, key=lambda item: item["player_public_id"]),
            "aliases": sorted(alias_entries, key=lambda item: item["alias_public_id"]),
            "replay_players": sorted(
                (
                    {
                        "replay_player_public_id": replay_player.public_id,
                        "player_public_id": (
                            None if replay_player.player_id is None else player_public[replay_player.player_id]
                        ),
                    }
                    for replay_player in replay_players
                ),
                key=lambda item: item["replay_player_public_id"],
            ),
            "source_public_ids": sorted(source_public_ids),
        }

    def _append_operation(
        self,
        session: Session,
        *,
        kind: str,
        actor: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
        inverse_payload: dict[str, Any],
        players: list[Player],
        inverse_of: PlayerIdentityOperation | None = None,
    ) -> PlayerIdentityOperation:
        operation = PlayerIdentityOperation(
            public_id=self._public_id_factory(),
            operation_kind=kind,
            inverse_of_operation_id=None if inverse_of is None else inverse_of.id,
            actor=actor,
            reason=reason,
            before_json=before,
            after_json=after,
            inverse_payload_json=inverse_payload,
            affected_revisions_json={
                "players": [
                    {"player_public_id": public_id, "identity_revision": revision}
                    for public_id, revision in self._revisions(players)
                ]
            },
            created_at=self._now_factory(),
        )
        session.add(operation)
        session.flush()
        return operation

    def _receipt(
        self,
        operation: PlayerIdentityOperation,
        players: list[Player],
        *,
        inverse_of_public_id: str | None = None,
    ) -> IdentityOperationReceipt:
        revisions = self._revisions(players)
        return IdentityOperationReceipt(
            operation_public_id=operation.public_id,
            operation_kind=operation.operation_kind,
            inverse_of_operation_public_id=inverse_of_public_id,
            affected_player_revisions=revisions,
            cache_tokens=tuple(
                (public_id, identity_cache_digest(public_id, revision)) for public_id, revision in revisions
            ),
        )

    def resolve_parser_run(
        self, *, replay_public_id: str, parser_run_id: str, actor: str = "system:identity-resolver"
    ) -> IdentityResolutionBatch:
        _require_text(actor, "actor")
        decisions: list[IdentityDecision] = []
        affected: dict[str, int] = {}
        with self._writer() as session:
            replay = _one_or_none(session, select(Replay).where(Replay.public_id == replay_public_id))
            run = _one_or_none(session, select(ParserRun).where(ParserRun.run_id == parser_run_id))
            if replay is None:
                raise IdentityNotFoundError(f"unknown replay {replay_public_id}")
            if run is None:
                raise IdentityNotFoundError(f"unknown parser run {parser_run_id}")
            replay = cast(Replay, replay)
            run = cast(ParserRun, run)
            slots = session.scalars(
                select(ReplayPlayer).where(ReplayPlayer.parser_run_id == run.id).order_by(ReplayPlayer.slot_index)
            ).all()
            run_eligible = run.status == "succeeded" and run.replay_id == replay.id
            for slot in slots:
                if not run_eligible:
                    decisions.append(self._decision(slot, None, "ineligible", None, None, None, "parser_run_ineligible"))
                    continue
                if slot.slot_kind != "human" or slot.original_name is None:
                    reason = "nonhuman_slot" if slot.slot_kind != "human" else "unnamed_slot"
                    decisions.append(self._decision(slot, None, "ineligible", None, None, None, reason))
                    continue
                try:
                    normalized = normalize_embedded_name(slot.original_name)
                except InvalidPlayerNameError:
                    decisions.append(self._decision(slot, None, "ineligible", None, None, None, "invalid_embedded_name"))
                    continue
                alias_candidates = session.scalars(
                    select(PlayerAlias)
                    .where(
                        PlayerAlias.namespace == EMBEDDED_REPLAY_NAME_NAMESPACE,
                        PlayerAlias.normalized_name == normalized,
                    )
                    .order_by(PlayerAlias.public_id)
                    .limit(2)
                ).all()
                if len(alias_candidates) > 1:
                    decisions.append(
                        self._decision(
                            slot,
                            normalized,
                            "manual_review",
                            None,
                            None,
                            None,
                            "ambiguous_exact_embedded_alias",
                        )
                    )
                    continue
                alias = alias_candidates[0] if alias_candidates else None
                if slot.player_id is not None:
                    linked_player = session.get(Player, slot.player_id)
                    if (
                        alias is not None
                        and cast(PlayerAlias, alias).player_id == slot.player_id
                        and linked_player is not None
                        and linked_player.retired_at is None
                    ):
                        alias = cast(PlayerAlias, alias)
                        decisions.append(
                            self._decision(
                                slot,
                                normalized,
                                "already_linked",
                                linked_player.public_id,
                                alias.public_id,
                                None,
                                "exact_alias_already_linked",
                            )
                        )
                    else:
                        decisions.append(
                            self._decision(
                                slot,
                                normalized,
                                "manual_review",
                                None if linked_player is None else linked_player.public_id,
                                None if alias is None else cast(PlayerAlias, alias).public_id,
                                None,
                                "existing_link_conflicts_with_exact_alias",
                            )
                        )
                    continue
                before_slot = {slot.id}
                if alias is None:
                    player = Player(
                        public_id=self._public_id_factory(),
                        display_name=slot.original_name,
                        identity_revision=0,
                        updated_at=self._now_factory(),
                        retired_at=None,
                        created_at=self._now_factory(),
                    )
                    session.add(player)
                    session.flush()
                    alias = PlayerAlias(
                        public_id=self._public_id_factory(),
                        player_id=player.id,
                        namespace=EMBEDDED_REPLAY_NAME_NAMESPACE,
                        normalized_name=normalized,
                        original_name=slot.original_name,
                        external_subject=None,
                        created_at=self._now_factory(),
                    )
                    session.add(alias)
                    session.flush()
                    before = self._snapshot(
                        session,
                        operation_kind="auto_link",
                        player_ids=set(),
                        alias_ids=set(),
                        replay_player_ids=before_slot,
                    )
                    outcome = "created"
                    reason_code = "created_exact_embedded_alias"
                else:
                    alias = cast(PlayerAlias, alias)
                    linked_alias_player = session.get(Player, alias.player_id)
                    if linked_alias_player is None or linked_alias_player.retired_at is not None:
                        decisions.append(
                            self._decision(
                                slot,
                                normalized,
                                "manual_review",
                                None if linked_alias_player is None else linked_alias_player.public_id,
                                alias.public_id,
                                None,
                                "exact_alias_player_inconsistent",
                            )
                        )
                        continue
                    player = linked_alias_player
                    before = self._snapshot(
                        session,
                        operation_kind="auto_link",
                        player_ids={player.id},
                        alias_ids={alias.id},
                        replay_player_ids=before_slot,
                    )
                    outcome = "linked"
                    reason_code = "linked_exact_embedded_alias"
                slot.player_id = player.id
                player.identity_revision += 1
                player.updated_at = self._now_factory()
                session.flush()
                after = self._snapshot(
                    session,
                    operation_kind="auto_link",
                    player_ids={player.id},
                    alias_ids={alias.id},
                    replay_player_ids=before_slot,
                )
                operation = self._append_operation(
                    session,
                    kind="auto_link",
                    actor=actor,
                    reason="exact embedded replay name resolution",
                    before=before,
                    after=after,
                    inverse_payload={"restore": before},
                    players=[player],
                )
                affected[player.public_id] = player.identity_revision
                decisions.append(
                    self._decision(
                        slot,
                        normalized,
                        outcome,
                        player.public_id,
                        alias.public_id,
                        operation.public_id,
                        reason_code,
                    )
                )
        return IdentityResolutionBatch(
            replay_public_id=replay_public_id,
            parser_run_id=parser_run_id,
            decisions=tuple(decisions),
            affected_player_revisions=tuple(sorted(affected.items())),
        )

    @staticmethod
    def _decision(
        slot: ReplayPlayer,
        normalized_name: str | None,
        outcome: str,
        player_public_id: str | None,
        alias_public_id: str | None,
        operation_public_id: str | None,
        reason_code: str,
    ) -> IdentityDecision:
        return IdentityDecision(
            replay_player_public_id=slot.public_id,
            normalized_name=normalized_name,
            outcome=cast(Any, outcome),
            player_public_id=player_public_id,
            alias_public_id=alias_public_id,
            operation_public_id=operation_public_id,
            reason_code=reason_code,
        )

    def merge_players(
        self,
        *,
        target_player_public_id: str,
        source_player_public_ids: tuple[str, ...],
        expected_revisions: Mapping[str, int],
        actor: str,
        reason: str,
    ) -> IdentityOperationReceipt:
        _require_text(actor, "actor")
        _require_text(reason, "reason")
        if not source_player_public_ids or len(set(source_player_public_ids)) != len(source_player_public_ids):
            raise IdentityInvariantError("merge sources must be nonempty and duplicate-free")
        if target_player_public_id in source_player_public_ids:
            raise IdentityInvariantError("merge target cannot also be a source")
        with self._writer() as session:
            target = self._player(session, target_player_public_id)
            sources = [self._player(session, public_id) for public_id in sorted(source_player_public_ids)]
            players = [target, *sources]
            self._validate_expected(players, expected_revisions)
            if target.retired_at is not None or any(source.retired_at is not None for source in sources):
                raise IdentityConflictError("merge players must all be active")
            player_ids = {player.id for player in players}
            aliases = session.scalars(select(PlayerAlias).where(PlayerAlias.player_id.in_(player_ids))).all()
            replay_players = session.scalars(select(ReplayPlayer).where(ReplayPlayer.player_id.in_(player_ids))).all()
            before = self._snapshot(
                session,
                operation_kind="merge_players",
                player_ids=player_ids,
                alias_ids={alias.id for alias in aliases},
                replay_player_ids={row.id for row in replay_players},
            )
            source_ids = {source.id for source in sources}
            for alias in aliases:
                if alias.player_id in source_ids:
                    alias.player_id = target.id
            for row in replay_players:
                if row.player_id in source_ids:
                    row.player_id = target.id
            now = self._now_factory()
            for source in sources:
                source.retired_at = now
            for player in players:
                player.identity_revision += 1
                player.updated_at = now
            session.flush()
            after = self._snapshot(
                session,
                operation_kind="merge_players",
                player_ids=player_ids,
                alias_ids={alias.id for alias in aliases},
                replay_player_ids={row.id for row in replay_players},
            )
            operation = self._append_operation(
                session,
                kind="merge_players",
                actor=actor,
                reason=reason,
                before=before,
                after=after,
                inverse_payload={"restore": before},
                players=players,
            )
            return self._receipt(operation, players)

    def split_alias(
        self,
        *,
        alias_public_id: str,
        replay_player_public_ids: tuple[str, ...],
        new_display_name: str,
        expected_revisions: Mapping[str, int],
        actor: str,
        reason: str,
    ) -> IdentityOperationReceipt:
        _require_text(actor, "actor")
        _require_text(reason, "reason")
        _require_text(new_display_name, "new display name")
        if not replay_player_public_ids or len(set(replay_player_public_ids)) != len(replay_player_public_ids):
            raise IdentityInvariantError("split replay players must be nonempty and duplicate-free")
        with self._writer() as session:
            alias = _one_or_none(session, select(PlayerAlias).where(PlayerAlias.public_id == alias_public_id))
            if alias is None:
                raise IdentityNotFoundError(f"unknown alias {alias_public_id}")
            alias = cast(PlayerAlias, alias)
            if alias.namespace != EMBEDDED_REPLAY_NAME_NAMESPACE:
                raise IdentityInvariantError("only an embedded replay alias can be split")
            old_player = session.get(Player, alias.player_id)
            if old_player is None:
                raise IdentityInvariantError("alias has no canonical player")
            self._validate_expected([old_player], expected_revisions)
            rows = session.scalars(
                select(ReplayPlayer).where(ReplayPlayer.public_id.in_(replay_player_public_ids))
            ).all()
            if {row.public_id for row in rows} != set(replay_player_public_ids):
                raise IdentityNotFoundError("one or more replay players do not exist")
            if any(row.player_id != old_player.id for row in rows):
                raise IdentityConflictError("split membership changed after review")
            new_player_public_id = self._public_id_factory()
            before = self._snapshot(
                session,
                operation_kind="split_alias",
                player_ids={old_player.id},
                alias_ids={alias.id},
                replay_player_ids={row.id for row in rows},
                absent_player_public_ids=(new_player_public_id,),
            )
            new_player = Player(
                public_id=new_player_public_id,
                display_name=new_display_name,
                identity_revision=0,
                updated_at=self._now_factory(),
                retired_at=None,
                created_at=self._now_factory(),
            )
            session.add(new_player)
            session.flush()
            players = [old_player, new_player]
            player_ids = {old_player.id, new_player.id}
            alias.player_id = new_player.id
            for row in rows:
                row.player_id = new_player.id
            now = self._now_factory()
            for player in players:
                player.identity_revision += 1
                player.updated_at = now
            session.flush()
            after = self._snapshot(
                session,
                operation_kind="split_alias",
                player_ids=player_ids,
                alias_ids={alias.id},
                replay_player_ids={row.id for row in rows},
            )
            operation = self._append_operation(
                session,
                kind="split_alias",
                actor=actor,
                reason=reason,
                before=before,
                after=after,
                inverse_payload={"restore": before},
                players=players,
            )
            return self._receipt(operation, players)

    def attach_external_alias(
        self,
        *,
        player_public_id: str,
        provider: str,
        external_subject: str,
        source_public_id: str,
        actor: str,
        reason: str,
        expected_revisions: Mapping[str, int],
    ) -> IdentityOperationReceipt:
        _require_text(actor, "actor")
        _require_text(reason, "reason")
        provider = _require_text(provider, "provider").strip()
        external_subject = _require_text(external_subject, "external subject").strip()
        namespace = f"{EXTERNAL_NAMESPACE_PREFIX}{provider}"
        with self._writer() as session:
            player = self._player(session, player_public_id)
            self._validate_expected([player], expected_revisions)
            source = _one_or_none(session, select(Source).where(Source.public_id == source_public_id))
            if source is None:
                raise IdentityNotFoundError(f"unknown source {source_public_id}")
            existing = _one_or_none(
                session,
                select(PlayerAlias).where(
                    PlayerAlias.namespace == namespace, PlayerAlias.normalized_name == external_subject
                ),
            )
            if existing is not None:
                raise IdentityConflictError("external subject is already attached")
            alias_public_id = self._public_id_factory()
            before = self._snapshot(
                session,
                operation_kind="attach_external_alias",
                player_ids={player.id},
                alias_ids=set(),
                replay_player_ids=set(),
                source_public_ids=(cast(Source, source).public_id,),
                absent_alias_public_ids=(alias_public_id,),
            )
            alias = PlayerAlias(
                public_id=alias_public_id,
                player_id=player.id,
                namespace=namespace,
                normalized_name=external_subject,
                original_name=external_subject,
                external_subject=external_subject,
                created_at=self._now_factory(),
            )
            session.add(alias)
            session.flush()
            player.identity_revision += 1
            player.updated_at = self._now_factory()
            session.flush()
            after = self._snapshot(
                session,
                operation_kind="attach_external_alias",
                player_ids={player.id},
                alias_ids={alias.id},
                replay_player_ids=set(),
                source_public_ids=(cast(Source, source).public_id,),
            )
            operation = self._append_operation(
                session,
                kind="attach_external_alias",
                actor=actor,
                reason=reason,
                before=before,
                after=after,
                inverse_payload={"restore": before},
                players=[player],
            )
            return self._receipt(operation, [player])

    def inverse_operation(
        self,
        *,
        operation_public_id: str,
        expected_revisions: Mapping[str, int],
        actor: str,
        reason: str,
    ) -> IdentityOperationReceipt:
        _require_text(actor, "actor")
        _require_text(reason, "reason")
        with self._writer() as session:
            original = _one_or_none(
                session, select(PlayerIdentityOperation).where(PlayerIdentityOperation.public_id == operation_public_id)
            )
            if original is None:
                raise IdentityNotFoundError(f"unknown identity operation {operation_public_id}")
            original = cast(PlayerIdentityOperation, original)
            if original.operation_kind not in {"merge_players", "split_alias", "attach_external_alias"}:
                raise IdentityInvariantError("operation kind is not reversible")
            already_inversed = session.scalar(
                select(func.count()).select_from(PlayerIdentityOperation).where(
                    PlayerIdentityOperation.inverse_of_operation_id == original.id
                )
            )
            if already_inversed:
                raise IdentityConflictError("identity operation was already inversed")
            revision_entries = cast(dict[str, Any], original.affected_revisions_json)["players"]
            public_ids = [entry["player_public_id"] for entry in revision_entries]
            players = [self._player(session, public_id) for public_id in public_ids]
            self._validate_expected(players, expected_revisions)
            after = cast(dict[str, Any], original.after_json)
            alias_ids = self._alias_internal_ids(session, after)
            replay_player_ids = self._replay_player_internal_ids(session, after)
            current = self._snapshot(
                session,
                operation_kind=original.operation_kind,
                player_ids={player.id for player in players},
                alias_ids=alias_ids,
                replay_player_ids=replay_player_ids,
                source_public_ids=tuple(after.get("source_public_ids", [])),
            )
            if current != after:
                raise IdentityConflictError("identity mapping changed after the original operation")
            restore = cast(dict[str, Any], cast(dict[str, Any], original.inverse_payload_json)["restore"])
            self._restore_snapshot(session, restore)
            now = self._now_factory()
            for player in players:
                player.identity_revision += 1
                player.updated_at = now
            session.flush()
            retired_tombstones = {
                entry["player_public_id"]: PRESENCE_RETIRED_TOMBSTONE
                for entry in restore.get("players", [])
                if entry.get("presence") == PRESENCE_ABSENT
            }
            restored = self._snapshot(
                session,
                operation_kind="inverse",
                player_ids={player.id for player in players},
                alias_ids=alias_ids,
                replay_player_ids=replay_player_ids,
                source_public_ids=tuple(restore.get("source_public_ids", [])),
                player_presence_overrides=retired_tombstones,
            )
            inverse = self._append_operation(
                session,
                kind="inverse",
                actor=actor,
                reason=reason,
                before=current,
                after=restored,
                inverse_payload={"restore": current},
                players=players,
                inverse_of=original,
            )
            return self._receipt(inverse, players, inverse_of_public_id=original.public_id)

    def _alias_internal_ids(self, session: Session, snapshot: dict[str, Any]) -> set[int]:
        public_ids = [entry["alias_public_id"] for entry in snapshot.get("aliases", [])]
        if not public_ids:
            return set()
        return set(session.scalars(select(PlayerAlias.id).where(PlayerAlias.public_id.in_(public_ids))))

    def _replay_player_internal_ids(self, session: Session, snapshot: dict[str, Any]) -> set[int]:
        public_ids = [entry["replay_player_public_id"] for entry in snapshot.get("replay_players", [])]
        if not public_ids:
            return set()
        return set(session.scalars(select(ReplayPlayer.id).where(ReplayPlayer.public_id.in_(public_ids))))

    def _restore_snapshot(self, session: Session, snapshot: dict[str, Any]) -> None:
        players = {
            player.public_id: player
            for player in session.scalars(
                select(Player).where(
                    Player.public_id.in_([entry["player_public_id"] for entry in snapshot.get("players", [])])
                )
            )
        }
        for entry in snapshot.get("players", []):
            player = players[entry["player_public_id"]]
            presence = entry.get("presence")
            if presence == PRESENCE_ABSENT:
                player.retired_at = self._now_factory()
            else:
                retired = entry.get("retired", presence in {PRESENCE_RETIRED, PRESENCE_RETIRED_TOMBSTONE})
                player.retired_at = self._now_factory() if retired else None
        all_player_ids = {
            player.public_id: player.id
            for player in session.scalars(select(Player)).all()
        }
        for entry in snapshot.get("aliases", []):
            alias = _one_or_none(session, select(PlayerAlias).where(PlayerAlias.public_id == entry["alias_public_id"]))
            if alias is None:
                raise IdentityConflictError("captured alias no longer exists")
            alias = cast(PlayerAlias, alias)
            if entry.get("presence") == PRESENCE_ABSENT:
                alias.namespace = f"external:detached:{alias.public_id}"
                continue
            alias.player_id = all_player_ids[entry["player_public_id"]]
            alias.namespace = entry["namespace"]
            alias.normalized_name = entry["normalized_name"]
            alias.external_subject = entry["external_subject"]
        for entry in snapshot.get("replay_players", []):
            row = _one_or_none(
                session, select(ReplayPlayer).where(ReplayPlayer.public_id == entry["replay_player_public_id"])
            )
            if row is None:
                raise IdentityConflictError("captured replay player no longer exists")
            target = entry["player_public_id"]
            cast(ReplayPlayer, row).player_id = None if target is None else all_player_ids[target]

    def identity_cache_token(self, *, player_public_id: str) -> str:
        with self._session_factory() as session:
            player = self._player(session, player_public_id)
            return identity_cache_digest(player.public_id, player.identity_revision)

"""Import-dialog and safe root-selection contracts before their route exists."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from generals_replay_analyzer.web.app import create_app
from generals_replay_analyzer.web.ports import (
    AvailabilityDTO,
    ImportRootDTO,
    ImportSubmissionDTO,
    RootImportCommandDTO,
)

from .conftest import RecordingBootstrapper

ROOT_ID = "123e4567-e89b-42d3-a456-426614174010"
REPLAY_ID = "123e4567-e89b-42d3-a456-426614174011"
SUBMISSION_ID = "123e4567-e89b-42d3-a456-426614174012"


class _ImportPort:
    def __init__(
        self, *, state: str = "available", submission_problem: str | None = None, has_roots: bool = True
    ) -> None:
        self.root_commands: list[RootImportCommandDTO] = []
        self._state = state
        self._submission_problem = submission_problem
        self._has_roots = has_roots

    def import_roots(self) -> tuple[ImportRootDTO, ...]:
        if not self._has_roots:
            return ()
        return (
            ImportRootDTO(
                root_public_id=ROOT_ID,
                label="Tournament archives",
                availability=AvailabilityDTO(state=self._state, reason_codes=("root_fixture_reason",)),
            ),
        )

    def submit_root_selection(self, command: RootImportCommandDTO) -> ImportSubmissionDTO:
        self.root_commands.append(command)
        return ImportSubmissionDTO(
            submission_public_id=SUBMISSION_ID,
            replay_public_id=REPLAY_ID if self._submission_problem is None else None,
            duplicate_of_replay_public_id=None,
            pipeline=None,
            availability=AvailabilityDTO(
                state="available" if self._submission_problem is None else "unavailable",
                reason_codes=() if self._submission_problem is None else (self._submission_problem,),
            ),
            problem_code=self._submission_problem,
        )


class _ImportPortFactory:
    def __init__(self, port: _ImportPort) -> None:
        self.port = port
        self.created = 0
        self.closed = 0

    @contextmanager
    def __call__(self) -> Iterator[_ImportPort]:
        self.created += 1
        try:
            yield self.port
        finally:
            self.closed += 1


class _TokenValidator:
    def accepts(self, token: str | None) -> bool:
        return token == "accepted-token"

    def issue(self) -> str:
        return "accepted-token"


def _client(port: _ImportPort) -> TestClient:
    return TestClient(
        create_app(
            object(),
            port_factory=_ImportPortFactory(port),
            bootstrapper=RecordingBootstrapper(),
            csrf_validator=_TokenValidator(),
        )
    )

def test_import_routes_are_registered_from_a_web_only_module() -> None:
    """Removing the import route must make the public import boundary unavailable."""
    from generals_replay_analyzer.web.routes.imports import router

    assert router.prefix == ""


def test_import_dialog_labels_the_blocked_upload_and_disables_unavailable_roots() -> None:
    port = _ImportPort(state="unavailable")

    with _client(port) as client:
        response = client.get("/imports/dialog", headers={"host": "localhost"})

    assert response.status_code == 200
    assert 'role="dialog"' in response.text
    assert 'aria-labelledby="import-dialog-title"' in response.text
    assert 'id="upload-file"' in response.text
    assert 'disabled' in response.text
    assert "opaque_ingress_handoff_pending" in response.text
    assert "Tournament archives" in response.text
    assert "root_fixture_reason" in response.text
    assert "<noscript>" in response.text


@pytest.mark.parametrize(
    "relative_path",
    [
        "../private.rep",
        "/absolute.rep",
        r"C:\\private.rep",
        r"\\server\\share\\private.rep",
        "folder//private.rep",
        "folder/./private.rep",
        "folder/../private.rep",
        "folder/control\x00.rep",
    ],
)
def test_root_import_rejects_unsafe_names_without_calling_the_port(relative_path: str) -> None:
    port = _ImportPort()

    with _client(port) as client:
        response = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": relative_path},
            headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": "accepted-token"},
        )

    assert response.status_code == 422
    assert port.root_commands == []


def test_root_import_maps_only_a_public_root_id_and_safe_relative_name_to_the_port() -> None:
    port = _ImportPort()

    with _client(port) as client:
        response = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep"},
            headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": "accepted-token"},
        )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "submission_public_id": SUBMISSION_ID,
        "replay_public_id": REPLAY_ID,
        "duplicate_of_replay_public_id": None,
        "pipeline": None,
        "availability": {"state": "available", "reason_codes": [], "evidence_references": []},
        "problem_code": None,
    }
    assert port.root_commands == [RootImportCommandDTO(root_public_id=ROOT_ID, relative_path="league/week-1/match.rep")]


def test_root_import_accepts_safe_dotted_replay_stems() -> None:
    command = RootImportCommandDTO(root_public_id=ROOT_ID, relative_path="league.2026/round.v2.rep")

    assert command.relative_path == "league.2026/round.v2.rep"


@pytest.mark.parametrize(
    "form_items",
    [
        [("root_public_id", ROOT_ID), ("relative_path", "safe.rep"), ("unexpected", "value")],
        [("root_public_id", ROOT_ID), ("root_public_id", ROOT_ID), ("relative_path", "safe.rep")],
        [("root_public_id", ROOT_ID), ("relative_path", "safe.rep"), ("relative_path", "other.rep")],
    ],
)
def test_root_import_rejects_unknown_or_repeated_form_fields_before_opening_a_port_scope(
    form_items: list[tuple[str, str]],
) -> None:
    port = _ImportPort()
    factory = _ImportPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/imports/root-selections",
            content=urlencode(form_items),
            headers={
                "host": "localhost",
                "origin": "http://localhost",
                "x-csrf-token": "accepted-token",
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_root_selection"
    assert factory.created == 0
    assert factory.closed == 0
    assert port.root_commands == []


def test_rendered_root_form_issues_a_server_token_that_a_native_post_can_submit() -> None:
    port = _ImportPort()

    with _client(port) as client:
        dialog = client.get("/imports/dialog", headers={"host": "localhost"})
        token = re.search(r'name="_csrf" value="([^"]+)"', dialog.text)
        assert token is not None
        response = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep", "_csrf": token.group(1)},
            headers={"host": "localhost", "origin": "http://localhost"},
        )

    assert response.status_code == 201, response.text
    assert port.root_commands == [RootImportCommandDTO(root_public_id=ROOT_ID, relative_path="league/week-1/match.rep")]


def test_missing_or_wrong_rendered_form_token_is_rejected_before_the_port() -> None:
    port = _ImportPort()
    factory = _ImportPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )
    headers = {"host": "localhost", "origin": "http://localhost"}

    with TestClient(app) as client:
        dialog = client.get("/imports/dialog", headers={"host": "localhost"})
        token = re.search(r'name="_csrf" value="([^"]+)"', dialog.text)
        assert token is not None
        scopes_before_post = factory.created
        missing = client.post(
            "/imports/root-selections", data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep"}, headers=headers
        )
        wrong = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep", "_csrf": "wrong-token"},
            headers=headers,
        )
        accepted = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep", "_csrf": token.group(1)},
            headers=headers,
        )
        replayed = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep", "_csrf": token.group(1)},
            headers=headers,
        )

    assert [missing.status_code, wrong.status_code, accepted.status_code, replayed.status_code] == [403, 403, 201, 403]
    assert factory.created == scopes_before_post + 1
    assert factory.closed == factory.created
    assert port.root_commands == [RootImportCommandDTO(root_public_id=ROOT_ID, relative_path="league/week-1/match.rep")]


def test_non_ascii_hidden_form_token_is_forbidden_without_opening_a_port_scope() -> None:
    port = _ImportPort()
    factory = _ImportPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )

    with TestClient(app) as client:
        dialog = client.get("/imports/dialog", headers={"host": "localhost"})
        assert dialog.status_code == 200
        scopes_before_post = factory.created
        response = client.post(
            "/imports/root-selections",
            data={"root_public_id": ROOT_ID, "relative_path": "safe.rep", "_csrf": "é"},
            headers={"host": "localhost", "origin": "http://localhost"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"
    assert factory.created == scopes_before_post
    assert factory.closed == factory.created
    assert port.root_commands == []


def test_exact_rendered_token_is_consumed_once_under_concurrent_native_posts() -> None:
    port = _ImportPort()
    factory = _ImportPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )
    headers = {"host": "localhost", "origin": "http://localhost"}
    with TestClient(app) as client:
        dialog = client.get("/imports/dialog", headers={"host": "localhost"})
        token = re.search(r'name="_csrf" value="([^"]+)"', dialog.text)
        assert token is not None
        cookie = client.cookies.get("_csrf")
        assert cookie is not None
        scopes_before_post = factory.created

    barrier = Barrier(2)

    def submit() -> int:
        with TestClient(app) as contender:
            contender.cookies.set("_csrf", cookie)
            barrier.wait()
            response = contender.post(
                "/imports/root-selections",
                data={"root_public_id": ROOT_ID, "relative_path": "league/week-1/match.rep", "_csrf": token.group(1)},
                headers=headers,
            )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _index: submit(), range(2)))

    assert statuses == [201, 403]
    assert factory.created == scopes_before_post + 1
    assert factory.closed == factory.created
    assert port.root_commands == [RootImportCommandDTO(root_public_id=ROOT_ID, relative_path="league/week-1/match.rep")]


@pytest.mark.parametrize(
    "relative_path",
    [
        "notes.txt",
        "match.REP",
        "match.rep.txt",
        "match.rep.rep",
        "match.rep.",
        "match.rep ",
        "C:private.rep",
        "folder/name.rep:stream",
        "folder:stream/match.rep",
        "folder./match.rep",
        "folder /match.rep",
        "folder/\x1f/match.rep",
        "CON.rep",
        "CON/match.rep",
        "folder/NUL/match.rep",
        "folder/COM1.backup/match.rep",
        ".rep",
        "match%2erep",
        "match.rep%20",
        "%2e%2e/private.rep",
        "%2543ON/match.rep",
        "match?.rep",
        "star*/match.rep",
        "less</match.rep",
        "greater>.rep",
        "pipe|.rep",
    ],
)
def test_root_import_rejects_adversarial_relative_path_matrix(relative_path: str) -> None:
    with pytest.raises(ValueError, match="normalized POSIX|lower-case .rep|replay_relative_name_invalid"):
        RootImportCommandDTO(root_public_id=ROOT_ID, relative_path=relative_path)


@pytest.mark.parametrize("relative_path", ["clock$.rep", "cafe\u0301.rep", "match\u200d.rep"])
def test_root_import_delegates_portable_unicode_and_device_name_contract(relative_path: str) -> None:
    with pytest.raises(ValueError, match="replay_relative_name_invalid"):
        RootImportCommandDTO(root_public_id=ROOT_ID, relative_path=relative_path)


@pytest.mark.parametrize(
    "root_public_id",
    [
        "123e4567-e89b-12d3-a456-426614174010",
        "123E4567-E89B-42D3-A456-426614174010",
        "{123e4567-e89b-42d3-a456-426614174010}",
    ],
)
def test_root_import_requires_neutral_canonical_uuid4_root_id(root_public_id: str) -> None:
    with pytest.raises(ValueError, match="root_public_id_invalid|lowercase hyphenated UUID"):
        RootImportCommandDTO(root_public_id=root_public_id, relative_path="safe.rep")


def test_malformed_native_csrf_cookie_is_forbidden_without_opening_a_port_scope() -> None:
    port = _ImportPort()
    factory = _ImportPortFactory(port)
    app = create_app(
        object(),
        port_factory=factory,
        bootstrapper=RecordingBootstrapper(),
        csrf_validator=_TokenValidator(),
    )

    messages = _raw_non_ascii_cookie_post(app)
    response_start = next(message for message in messages if message["type"] == "http.response.start")

    assert response_start["status"] == 403
    assert b"csrf_rejected" in b"".join(message.get("body", b"") for message in messages)
    assert factory.created == 0
    assert factory.closed == 0
    assert port.root_commands == []


def _raw_non_ascii_cookie_post(app: object) -> list[dict[str, object]]:
    """Exercise an attacker-controlled Latin-1 cookie value that HTTPX correctly refuses to construct."""
    body = urlencode({"root_public_id": ROOT_ID, "relative_path": "safe.rep", "_csrf": "anything"}).encode("ascii")
    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async def call() -> None:
        await app(  # type: ignore[operator]
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/imports/root-selections",
                "raw_path": b"/imports/root-selections",
                "query_string": b"",
                "headers": [
                    (b"host", b"localhost"),
                    (b"origin", b"http://localhost"),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cookie", b"_csrf=\xe9.bad"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 80),
            },
            receive,
            send,
        )

    asyncio.run(call())
    return messages


def test_import_dialog_disables_configured_root_controls_when_none_are_available() -> None:
    port = _ImportPort(has_roots=False)

    with _client(port) as client:
        response = client.get("/imports/dialog", headers={"host": "localhost"})

    assert response.status_code == 200
    assert "No configured replay roots are available." in response.text
    assert '<select id="configured-root" name="root_public_id" required disabled>' in response.text
    assert '<button type="submit" disabled>Import configured replay</button>' in response.text
    assert "no configured roots" in response.text.casefold()


def test_import_dialog_disables_root_controls_when_every_configured_root_is_unavailable() -> None:
    port = _ImportPort(state="unavailable")

    with _client(port) as client:
        response = client.get("/imports/dialog", headers={"host": "localhost"})

    assert "No configured replay roots are currently available." in response.text
    assert '<select id="configured-root" name="root_public_id" required disabled>' in response.text
    assert '<button type="submit" disabled>Import configured replay</button>' in response.text


def test_unsafe_host_origin_or_csrf_is_rejected_before_a_root_command_reaches_the_port() -> None:
    port = _ImportPort()
    cases = (
        {"host": "example.test", "origin": "http://localhost", "x-csrf-token": "accepted-token"},
        {"host": "localhost", "origin": "http://example.test", "x-csrf-token": "accepted-token"},
        {"host": "localhost", "origin": "http://localhost", "x-csrf-token": "wrong-token"},
    )

    with _client(port) as client:
        responses = [
            client.post("/imports/root-selections", data={"root_public_id": ROOT_ID, "relative_path": "safe.rep"}, headers=headers)
            for headers in cases
        ]

    assert [response.status_code for response in responses] == [403, 403, 403]
    assert port.root_commands == []


def test_upload_remains_controlled_unavailable_without_an_opaque_ingress_handoff() -> None:
    port = _ImportPort()

    with _client(port) as client:
        response = client.post(
            "/imports/uploads",
            headers={"host": "localhost", "origin": "http://localhost", "x-csrf-token": "accepted-token"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "opaque_ingress_handoff_pending"
    assert port.root_commands == []

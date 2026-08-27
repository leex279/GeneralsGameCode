from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from generals_replay_analyzer.strata.config import ResolverSettings
from generals_replay_analyzer.strata.http import HttpxStrataClient, SourceRequestError

APPROVED_REPLAY_URL = (
    "https://matchdata.playgenerals.online/replays/2026/8/1/match_3133811/"
    "user_x/match_3133811_user_x_replay.rep"
)


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class IncrementingTime(FakeTime):
    def monotonic(self) -> float:
        self.value += 1.0
        return self.value


def _settings(tmp_path: Path, **changes: object) -> ResolverSettings:
    settings = ResolverSettings.from_sources({"cache_path": tmp_path / "resolver.sqlite3"}, {})
    return replace(settings, **changes)


def test_profile_request_uses_canonical_url_descriptive_agent_and_timeouts(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"<html></html>")

    with HttpxStrataClient(_settings(tmp_path), transport=httpx.MockTransport(handler)) as client:
        assert client.get_profile(17945) == "<html></html>"

    request = requests[0]
    assert str(request.url) == "https://strata.gamereplays.org/zh/player/17945"
    assert request.headers["user-agent"].startswith("generals-strata-resolver/")
    assert request.extensions["timeout"] == {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}


def test_replay_download_rejects_redirect_to_unapproved_host(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/replay.rep"})

    with HttpxStrataClient(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client, pytest.raises(SourceRequestError) as raised:
        client.download_replay(APPROVED_REPLAY_URL)

    assert raised.value.code == "redirect_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://matchdata.playgenerals.online/replays/a_replay.rep",
        "https://strata.gamereplays.org/zh/player/17945",
        "https://matchdata.playgenerals.online/screenshots/a.jpg",
        "https://matchdata.playgenerals.online/replays/a_replay.rep?token=secret",
    ],
)
def test_replay_download_allowlist_rejects_wrong_urls(tmp_path: Path, url: str) -> None:
    with HttpxStrataClient(
        _settings(tmp_path), transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client, pytest.raises(SourceRequestError, match="url_not_allowed"):
        client.download_replay(url)


def test_429_honors_bounded_retry_after_then_succeeds(tmp_path: Path) -> None:
    attempts = 0
    fake_time = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, content=b"ok")

    with HttpxStrataClient(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as client:
        assert client.get_match(3133811) == "ok"

    assert attempts == 2
    assert 2.0 in fake_time.sleeps


def test_retry_after_above_ceiling_uses_bounded_backoff(tmp_path: Path) -> None:
    fake_time = FakeTime()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, headers={"retry-after": "121"})

    with HttpxStrataClient(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        random_seed=0,
    ) as client, pytest.raises(SourceRequestError) as raised:
        client.get_match(3133811)

    assert raised.value.code == "retry_exhausted"
    assert attempts == 3
    assert max(fake_time.sleeps) < 120


def test_html_and_replay_size_limits_abort_reads(tmp_path: Path) -> None:
    html = b"x" * (2 * 1024 * 1024 + 1)
    replay = b"r" * 33

    def handler(request: httpx.Request) -> httpx.Response:
        content = replay if request.url.host == "matchdata.playgenerals.online" else html
        return httpx.Response(200, content=content)

    settings = _settings(tmp_path, caps=replace(_settings(tmp_path).caps, replay_bytes=32))
    with HttpxStrataClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceRequestError, match="response_too_large"):
            client.get_profile(17945)
        with pytest.raises(SourceRequestError, match="response_too_large"):
            client.download_replay(APPROVED_REPLAY_URL)


def test_invalid_utf8_is_a_source_contract_failure(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"\xff"))
    with HttpxStrataClient(
        _settings(tmp_path), transport=transport
    ) as client, pytest.raises(SourceRequestError) as raised:
        client.get_profile(17945)
    assert raised.value.code == "invalid_utf8"


def test_three_attempts_exhaust_transient_connection_errors(tmp_path: Path) -> None:
    attempts = 0
    fake_time = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with HttpxStrataClient(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as client, pytest.raises(SourceRequestError) as raised:
        client.get_profile(17945)

    assert raised.value.code == "retry_exhausted"
    assert attempts == 3


def test_circuit_breaker_stops_new_transport_calls(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    settings = _settings(tmp_path, max_attempts=1, circuit_breaker_failures=2)
    with HttpxStrataClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceRequestError):
            client.get_profile(17945)
        with pytest.raises(SourceRequestError):
            client.get_profile(17945)
        with pytest.raises(SourceRequestError) as raised:
            client.get_profile(17945)

    assert raised.value.code == "circuit_open"
    assert attempts == 2


def test_at_most_two_requests_enter_transport_concurrently(tmp_path: Path) -> None:
    lock = threading.Lock()
    release = threading.Event()
    two_active = threading.Event()
    active = 0
    maximum = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_active.set()
        assert release.wait(timeout=5)
        with lock:
            active -= 1
        return httpx.Response(200, content=b"ok")

    fake_time = IncrementingTime()
    with HttpxStrataClient(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as client, ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.get_profile, player_id) for player_id in (1, 2, 3)]
        assert two_active.wait(timeout=5)
        assert maximum == 2
        release.set()
        assert [future.result(timeout=5) for future in futures] == ["ok", "ok", "ok"]


def test_host_interval_is_applied_between_request_starts(tmp_path: Path) -> None:
    fake_time = FakeTime()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
    with HttpxStrataClient(
        _settings(tmp_path),
        transport=transport,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as client:
        client.get_profile(1)
        client.get_profile(2)

    assert fake_time.sleeps == [0.5]

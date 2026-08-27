"""Bounded allowlisted HTTP client for server-rendered Strata evidence."""

from __future__ import annotations

import random
import re
import threading
import time
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlparse

import httpx

from .config import ResolverSettings
from .ports import MonotonicClock, Sleeper

_DETAIL_PATH = re.compile(r"/zh/(?:player|match)/[1-9]\d*")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class SourceRequestError(RuntimeError):
    """Stable path-free failure from the public Strata source boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HttpxStrataClient:
    """Rate-limited client for profile, match, and replay documents."""

    def __init__(
        self,
        settings: ResolverSettings,
        transport: httpx.BaseTransport | None = None,
        monotonic: MonotonicClock = time.monotonic,
        sleeper: Sleeper = _sleep,
        random_seed: int | None = None,
    ) -> None:
        self.settings = settings
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._random = random.Random(random_seed)
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            headers={"user-agent": settings.user_agent, "accept": "text/html,application/octet-stream"},
            transport=transport,
        )
        self._semaphore = threading.BoundedSemaphore(settings.http_concurrency)
        self._degraded_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_start: dict[str, float] = {}
        self._failure_count = 0
        self._circuit_open = False
        self._degraded = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_profile(self, player_id: int) -> str:
        if player_id <= 0:
            raise SourceRequestError("url_not_allowed")
        url = f"https://strata.gamereplays.org/zh/player/{player_id}"
        return self._request_bytes(url, "detail", self.settings.html_bytes).decode("utf-8", errors="strict")

    def get_match(self, match_id: int) -> str:
        if match_id <= 0:
            raise SourceRequestError("url_not_allowed")
        url = f"https://strata.gamereplays.org/zh/match/{match_id}"
        return self._request_bytes(url, "detail", self.settings.html_bytes).decode("utf-8", errors="strict")

    def download_replay(self, url: str) -> bytes:
        return self._request_bytes(url, "replay", self.settings.caps.replay_bytes)

    def _request_bytes(self, url: str, kind: str, limit: int) -> bytes:
        self._validate_url(url, kind)
        with self._state_lock:
            if self._circuit_open:
                raise SourceRequestError("circuit_open")
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                content = self._attempt(url, kind, limit)
            except UnicodeDecodeError as error:
                raise SourceRequestError("invalid_utf8") from error
            except (httpx.ConnectError, httpx.TimeoutException):
                self._record_failure()
                retry_after = None
            except _TransientStatus as error:
                self._record_failure()
                retry_after = error.retry_after
            else:
                self._record_success()
                return content
            if attempt == self.settings.max_attempts:
                break
            self._sleeper(self._retry_delay(attempt, retry_after))
        raise SourceRequestError("retry_exhausted")

    def _attempt(self, url: str, kind: str, limit: int) -> bytes:
        current_url = url
        for _ in range(4):
            self._validate_url(current_url, kind)
            with self._semaphore:
                degraded_guard = self._degraded_lock if self._degraded else _NullLock()
                with degraded_guard:
                    self._wait_for_host(urlparse(current_url).hostname or "")
                    with self._client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise SourceRequestError("redirect_not_allowed")
                            redirected = urljoin(current_url, location)
                            try:
                                self._validate_url(redirected, kind)
                            except SourceRequestError as error:
                                raise SourceRequestError("redirect_not_allowed") from error
                            current_url = redirected
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            if response.status_code in {429, 503}:
                                with self._state_lock:
                                    self._degraded = True
                            raise _TransientStatus(self._retry_after(response.headers.get("retry-after")))
                        if response.status_code < 200 or response.status_code >= 300:
                            raise SourceRequestError("http_error")
                        declared = response.headers.get("content-length")
                        if declared is not None and declared.isdecimal() and int(declared) > limit:
                            raise SourceRequestError("response_too_large")
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > limit:
                                raise SourceRequestError("response_too_large")
                        if kind == "detail":
                            try:
                                bytes(body).decode("utf-8", errors="strict")
                            except UnicodeDecodeError as error:
                                raise SourceRequestError("invalid_utf8") from error
                        return bytes(body)
        raise SourceRequestError("redirect_not_allowed")

    def _wait_for_host(self, host: str) -> None:
        with self._rate_lock:
            now = self._monotonic()
            previous = self._last_start.get(host)
            if previous is not None:
                remaining = self.settings.host_interval_seconds - (now - previous)
                if remaining > 0:
                    self._sleeper(remaining)
                    now = self._monotonic()
            self._last_start[host] = now

    def _record_failure(self) -> None:
        with self._state_lock:
            self._failure_count += 1
            if self._failure_count >= self.settings.circuit_breaker_failures:
                self._circuit_open = True

    def _record_success(self) -> None:
        with self._state_lock:
            self._failure_count = 0

    def _retry_after(self, value: str | None) -> float | None:
        if value is None or not value.isdecimal():
            return None
        seconds = int(value)
        if seconds > self.settings.max_retry_after_seconds:
            return None
        return float(seconds)

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        return float(min(8.0, 0.5 * (2 ** (attempt - 1))) + self._random.uniform(0.0, 0.25))

    @staticmethod
    def _validate_url(url: str, kind: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise SourceRequestError("url_not_allowed")
        if kind == "detail":
            valid = parsed.hostname == "strata.gamereplays.org" and _DETAIL_PATH.fullmatch(parsed.path) is not None
        else:
            valid = (
                parsed.hostname == "matchdata.playgenerals.online"
                and parsed.path.startswith("/replays/")
                and parsed.path.endswith("_replay.rep")
            )
        if not valid:
            raise SourceRequestError("url_not_allowed")


class _TransientStatus(Exception):
    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

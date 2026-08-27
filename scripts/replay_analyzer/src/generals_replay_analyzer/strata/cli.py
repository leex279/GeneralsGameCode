"""Standalone command line interface for Strata replay-player resolution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

import httpx

from generals_replay_analyzer.errors import ReplayParseError

from .acquisition import StrataAcquirer
from .browser import PlaywrightListingBrowser
from .cache import CachePathError, ResolverCache, SchemaVersionError
from .config import ConfigurationError, ResolverSettings
from .contracts import NameResolution, ReplayResolution, ResolutionStatus
from .http import HttpxStrataClient
from .normalization import InvalidQueryNameError
from .replay_context import build_replay_context
from .service import StrataResolver


class ResolverPort(Protocol):
    def resolve_name(
        self,
        value: str,
        *,
        refresh: bool,
        offline: bool,
        include_fuzzy: bool,
    ) -> NameResolution: ...

    def resolve_replay(
        self,
        path: Path,
        *,
        refresh: bool,
        offline: bool,
        include_fuzzy: bool,
    ) -> ReplayResolution: ...


ApplicationFactory = Callable[[ResolverSettings], AbstractContextManager[ResolverPort]]


def _runtime_options(parser: argparse.ArgumentParser, *, acquisition: bool = True) -> None:
    parser.add_argument("--cache", type=Path, help="absolute resolver cache path")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    parser.add_argument("--debug", action="store_true", help="show local traceback diagnostics")
    if acquisition:
        parser.add_argument("--offline", action="store_true", help="use cached evidence only")
        parser.add_argument("--refresh", action="store_true", help="ignore unexpired cache entries")
        parser.add_argument("--browser", choices=("chromium", "chrome"))
        parser.add_argument("--timeout-seconds", type=float)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strata-resolver")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect-replay", help="parse replay-local metadata without network access")
    inspect.add_argument("replay", type=Path)
    _runtime_options(inspect, acquisition=False)

    name = commands.add_parser("resolve-name", help="return all Strata alias candidates")
    name.add_argument("name", nargs="?")
    name.add_argument("--name-file", type=Path)
    name.add_argument("--fuzzy", action="store_true", help="include suggestions that can never auto-link")
    _runtime_options(name)

    replay = commands.add_parser("resolve-replay", help="resolve every human player through shared match evidence")
    replay.add_argument("replay", type=Path)
    replay.add_argument("--fuzzy", action="store_true", help="include suggestions that can never auto-link")
    _runtime_options(replay)

    cache = commands.add_parser("cache", help="inspect or purge the isolated resolver cache")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_status = cache_commands.add_parser("status")
    _runtime_options(cache_status, acquisition=False)
    cache_purge = cache_commands.add_parser("purge")
    cache_purge.add_argument("--all", action="store_true", dest="purge_all")
    cache_purge.add_argument("--yes", action="store_true")
    _runtime_options(cache_purge, acquisition=False)

    doctor = commands.add_parser("doctor", help="verify cache, browser, and HTTPS prerequisites")
    _runtime_options(doctor, acquisition=False)
    doctor.add_argument("--browser", choices=("chromium", "chrome"))
    doctor.add_argument("--timeout-seconds", type=float)
    return parser


def _document(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        if isinstance(result, dict):
            return result
    raise TypeError("result does not expose a JSON document")


def _write_json(document: dict[str, object], *, pretty: bool) -> None:
    if pretty:
        encoded = json.dumps(document, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True)
    else:
        encoded = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    print(encoded)


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}, "status": "invalid"}


def _settings(arguments: argparse.Namespace) -> ResolverSettings:
    values: dict[str, object] = {}
    if arguments.cache is not None:
        values["cache_path"] = arguments.cache.resolve()
    if getattr(arguments, "browser", None) is not None:
        values["browser"] = arguments.browser
    if getattr(arguments, "timeout_seconds", None) is not None:
        values["timeout_seconds"] = arguments.timeout_seconds
    settings = ResolverSettings.from_sources(values, os.environ)
    if arguments.cache is None:
        settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


@contextmanager
def _real_application(settings: ResolverSettings) -> Iterator[ResolverPort]:
    with ExitStack() as stack:
        cache = stack.enter_context(ResolverCache(settings.cache_path))
        browser = PlaywrightListingBrowser(settings)
        stack.callback(browser.__exit__, None, None, None)
        http = stack.enter_context(HttpxStrataClient(settings))
        acquirer = StrataAcquirer(cache, browser, http, settings, cache.clock)
        yield StrataResolver(acquirer, cache, http, cache.clock)


def _read_name(arguments: argparse.Namespace) -> str:
    if (arguments.name is None) == (arguments.name_file is None):
        raise ValueError("name_source")
    if arguments.name is not None:
        return str(arguments.name)
    path = Path(arguments.name_file)
    if path.stat().st_size > 4096:
        raise OverflowError("name_file_too_large")
    try:
        return path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise UnicodeError("invalid_name_file") from error


def _exit_for_status(status: ResolutionStatus, *, acquisition_complete: bool = True) -> int:
    if not acquisition_complete or status is ResolutionStatus.INCOMPLETE:
        return 4
    if status is ResolutionStatus.RESOLVED:
        return 0
    if status in {ResolutionStatus.INVALID, ResolutionStatus.UNSUPPORTED}:
        return 2
    return 3


def _cache_command(arguments: argparse.Namespace, settings: ResolverSettings) -> tuple[dict[str, object], int]:
    with ResolverCache(settings.cache_path) as cache:
        if arguments.cache_command == "status":
            status = asdict(cache.status())
            status["path"] = str(status["path"])
            return status, 0
        if arguments.purge_all:
            print(f"cache path: {settings.cache_path}", file=sys.stderr)
            if not arguments.yes and (
                not sys.stdin.isatty() or input("Type yes to purge all cached source evidence: ") != "yes"
            ):
                return _error("purge_confirmation_required", "cache purge was not confirmed"), 2
            result = cache.purge_all()
            return {"scope": "all", **asdict(result)}, 0
        return {"scope": "expired", **asdict(cache.purge_expired())}, 0


def _doctor(settings: ResolverSettings) -> tuple[dict[str, object], int]:
    checks: dict[str, object] = {}
    try:
        with ResolverCache(settings.cache_path) as cache:
            checks["cache"] = {"ok": True, "schema_version": cache.status().schema_version}
    except (OSError, CachePathError, SchemaVersionError) as error:
        checks["cache"] = {"ok": False, "error": error.__class__.__name__}
    try:
        with PlaywrightListingBrowser(settings):
            checks["browser"] = {"ok": True, "name": settings.browser}
    except Exception as error:  # noqa: BLE001 - doctor reports adapter-specific launch failures by safe class name
        checks["browser"] = {"ok": False, "error": error.__class__.__name__}
    try:
        response = httpx.get(
            "https://strata.gamereplays.org/robots.txt",
            timeout=settings.timeout_seconds,
            headers={"user-agent": settings.user_agent},
            follow_redirects=False,
        )
        checks["https"] = {"ok": response.status_code == 200, "status_code": response.status_code}
    except httpx.HTTPError as error:
        checks["https"] = {"ok": False, "error": error.__class__.__name__}
    ok = all(isinstance(value, dict) and value.get("ok") is True for value in checks.values())
    return {"status": "ok" if ok else "failed", "checks": checks}, 0 if ok else 5


# TheSuperHackers @feature Leex 27/08/2026 Expose replay inspection and evidence-preserving Strata resolution without the Web product.
def main(
    argv: Sequence[str] | None = None,
    *,
    application_factory: ApplicationFactory | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    pretty = bool(arguments.pretty)
    try:
        if getattr(arguments, "offline", False) and getattr(arguments, "refresh", False):
            _write_json(
                _error("offline_refresh_conflict", "offline and refresh cannot be used together"),
                pretty=pretty,
            )
            return 2
        settings = _settings(arguments)
        if arguments.command == "inspect-replay":
            _write_json(_document(build_replay_context(arguments.replay)), pretty=pretty)
            return 0
        if arguments.command == "cache":
            document, code = _cache_command(arguments, settings)
            _write_json(document, pretty=pretty)
            return code
        if arguments.command == "doctor":
            document, code = _doctor(settings)
            _write_json(document, pretty=pretty)
            return code
        factory = _real_application if application_factory is None else application_factory
        with factory(settings) as resolver:
            if arguments.command == "resolve-name":
                print("discovering and checking candidate profiles", file=sys.stderr)
                result = resolver.resolve_name(
                    _read_name(arguments),
                    refresh=arguments.refresh,
                    offline=arguments.offline,
                    include_fuzzy=arguments.fuzzy,
                )
                _write_json(_document(result), pretty=pretty)
                return _exit_for_status(result.status, acquisition_complete=result.search_complete)
            print("parsing replay and checking shared Strata match evidence", file=sys.stderr)
            replay_result = resolver.resolve_replay(
                arguments.replay,
                refresh=arguments.refresh,
                offline=arguments.offline,
                include_fuzzy=arguments.fuzzy,
            )
            _write_json(_document(replay_result), pretty=pretty)
            return _exit_for_status(
                replay_result.match_resolution.status,
                acquisition_complete=replay_result.acquisition_complete,
            )
    except UnicodeError:
        _write_json(_error("invalid_name_file", "name file must be strict UTF-8"), pretty=pretty)
        return 2
    except OverflowError:
        _write_json(_error("name_file_too_large", "name file exceeds 4096 bytes"), pretty=pretty)
        return 2
    except (ReplayParseError, InvalidQueryNameError, ConfigurationError):
        _write_json(_error("invalid_request", "request validation failed"), pretty=pretty)
        if arguments.debug:
            traceback.print_exc(file=sys.stderr)
        return 2
    except (OSError, CachePathError, SchemaVersionError):
        _write_json(
            {
                "error": {"code": "local_runtime_failure", "message": "local resolver setup failed"},
                "status": "incomplete",
            },
            pretty=pretty,
        )
        if arguments.debug:
            traceback.print_exc(file=sys.stderr)
        return 5
    except ValueError as error:
        if str(error) == "name_source":
            _write_json(_error("invalid_name_source", "provide either a name or --name-file"), pretty=pretty)
        else:
            _write_json(_error("invalid_request", "request validation failed"), pretty=pretty)
        if arguments.debug:
            traceback.print_exc(file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - public CLI converts unexpected adapter failures to a path-free stable error
        _write_json(
            {
                "error": {"code": "runtime_failure", "message": "resolver execution failed"},
                "status": "incomplete",
            },
            pretty=pretty,
        )
        if arguments.debug:
            traceback.print_exc(file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())

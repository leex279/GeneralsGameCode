"""Narrow dependency-free async structured-provider contracts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle
from generals_replay_analyzer.llm.schema import PromptResource, ResponseSchemaResource

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProviderError(RuntimeError):
    """A stable path-, URL-, and response-free provider failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ProviderError(code={self.code!r})"


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...

    async def wait(self) -> bool: ...


@dataclass(frozen=True)
class TransportTimeout:
    connect: float = 30.0
    read: float = 30.0
    write: float = 30.0
    pool: float = 30.0

    def __post_init__(self) -> None:
        if (self.connect, self.read, self.write, self.pool) != (30.0, 30.0, 30.0, 30.0):
            raise ValueError("provider timeout policy is fixed")


@dataclass(frozen=True)
class OllamaClientConfig:
    endpoint: str
    timeout: TransportTimeout = field(default_factory=TransportTimeout)
    trust_env: bool = False
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if self.trust_env or self.follow_redirects:
            raise ValueError("provider client policy forbids environment proxies and redirects")


@dataclass(frozen=True)
class TransportHeaders:
    content_type: str
    content_length: int | None
    location: str | None = None

    def __post_init__(self) -> None:
        if type(self.content_type) is not str:
            raise ValueError("invalid transport content type")
        if self.content_length is not None and (
            type(self.content_length) is not int or self.content_length < 0
        ):
            raise ValueError("invalid transport content length")
        if self.location is not None and type(self.location) is not str:
            raise ValueError("invalid transport location")


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: TransportHeaders
    body: TransportBody
    client_config: OllamaClientConfig

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("invalid transport status")
        if type(self.headers) is not TransportHeaders:
            raise ValueError("invalid transport headers")
        if not callable(getattr(self.body, "__aiter__", None)) or not callable(
            getattr(self.body, "aclose", None)
        ):
            raise ValueError("invalid transport body lifecycle")  # noqa: TRY004
        if type(self.client_config) is not OllamaClientConfig:
            raise ValueError("invalid transport client config")

    async def aclose(self) -> None:
        """Release the response stream for every terminal provider path."""
        await self.body.aclose()


class TransportBody(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class OllamaTransport(Protocol):
    client_config: OllamaClientConfig

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: CancellationSignal | None,
    ) -> TransportResponse: ...


@dataclass(frozen=True)
class ModelIdentity:
    name: str
    digest: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or self.name != self.name.strip() or len(self.name.encode()) > 255:
            raise ValueError("invalid resolved model name")
        if type(self.digest) is not str or not _SHA256.fullmatch(self.digest):
            raise ValueError("invalid resolved model digest")


@dataclass(frozen=True)
class GenerationOptions:
    temperature: int = 0
    seed: int = 0
    stream: bool = False

    def __post_init__(self) -> None:
        if type(self.temperature) is not int or self.temperature != 0:
            raise ValueError("temperature is fixed at zero")
        if type(self.seed) is not int or self.seed != 0:
            raise ValueError("seed is fixed at zero")
        if type(self.stream) is not bool or self.stream:
            raise ValueError("streaming is disabled")


@dataclass(frozen=True)
class StructuredRequest:
    prompt: PromptResource
    response_schema: ResponseSchemaResource
    evidence_bundle: EvidenceBundle
    options: GenerationOptions = field(default_factory=GenerationOptions)


@dataclass(frozen=True)
class ProviderResult:
    model: ModelIdentity
    response_bytes: bytes
    response_digest: str

    def __post_init__(self) -> None:
        if type(self.response_bytes) is not bytes:
            raise ValueError("provider response must be exact bytes")
        if type(self.response_digest) is not str or not _SHA256.fullmatch(self.response_digest):
            raise ValueError("provider response digest must be lower-case SHA-256")
        if self.response_digest != hashlib.sha256(self.response_bytes).hexdigest():
            raise ValueError("provider response digest does not match exact bytes")


# TheSuperHackers @feature Leex 22/08/2026 Define the sanitized structured-provider boundary. (#TBD)
class StructuredProvider(Protocol):
    async def generate_structured(
        self,
        request: StructuredRequest,
        cancellation: CancellationSignal | None,
    ) -> ProviderResult: ...

"""Bounded local structured-interpretation primitives."""

from generals_replay_analyzer.llm.evidence_bundle import (
    EvidenceBundle,
    EvidenceBundleError,
    EvidenceClaim,
    build_evidence_bundle,
)
from generals_replay_analyzer.llm.ollama import OllamaProvider
from generals_replay_analyzer.llm.provider import (
    CancellationSignal,
    GenerationOptions,
    ModelIdentity,
    OllamaClientConfig,
    OllamaTransport,
    ProviderError,
    ProviderResult,
    StructuredProvider,
    StructuredRequest,
    TransportHeaders,
    TransportResponse,
    TransportTimeout,
)
from generals_replay_analyzer.llm.schema import (
    PromptResource,
    ResponseSchemaResource,
    ResponseValidationError,
    ValidatedResponse,
    load_prompt,
    load_response_schema,
    validate_response,
)

__all__ = [
    "CancellationSignal",
    "EvidenceBundle",
    "EvidenceBundleError",
    "EvidenceClaim",
    "GenerationOptions",
    "ModelIdentity",
    "OllamaClientConfig",
    "OllamaProvider",
    "OllamaTransport",
    "PromptResource",
    "ProviderError",
    "ProviderResult",
    "ResponseSchemaResource",
    "ResponseValidationError",
    "StructuredProvider",
    "StructuredRequest",
    "TransportHeaders",
    "TransportResponse",
    "TransportTimeout",
    "ValidatedResponse",
    "build_evidence_bundle",
    "load_prompt",
    "load_response_schema",
    "validate_response",
]

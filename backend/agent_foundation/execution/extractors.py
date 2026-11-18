from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef
from pydantic import Field


class ExtractorSpec(ContractModel):
    release_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_id: str = Field(min_length=1, max_length=240)
    schema_version: int = Field(ge=1)
    implementation_id: str = Field(min_length=1, max_length=240)
    implementation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_input_bytes: int = Field(ge=1)
    configuration: dict[str, Any]
    output_schema: dict[str, Any]


class ExtractorImplementation(Protocol):
    implementation_id: str
    implementation_digest: str

    def extract(self, configuration: dict[str, Any], payload: dict[str, Any]) -> Any: ...


class ExtractionProposalSink(Protocol):
    def propose(
        self,
        token: str,
        extractor: ExtractorSpec,
        *,
        event_id: str,
        value: Any,
        source_refs: tuple[ResourceRef, ...],
    ) -> ResourceRef: ...


class JsonPointerExtractor:
    implementation_id = "extractor.json-pointer-v1"
    implementation_digest = hashlib.sha256(implementation_id.encode()).hexdigest()

    def extract(self, configuration: dict[str, Any], payload: dict[str, Any]) -> Any:
        path = configuration.get("path")
        if not isinstance(path, list) or not path or any(
            not isinstance(item, str) or not item for item in path
        ):
            raise FoundationError(
                "extractor_configuration_invalid", "JSON pointer extractor needs a non-empty string path"
            )
        value: Any = payload
        for item in path:
            if not isinstance(value, dict) or item not in value:
                return None
            value = value[item]
        return value


class ExtractorRuntime:
    def __init__(self, implementations: tuple[ExtractorImplementation, ...]) -> None:
        self._implementations = {item.implementation_id: item for item in implementations}

    @staticmethod
    def input_bytes(payload: dict[str, Any]) -> int:
        return len(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))

    def execute_value(
        self, extractor: ExtractorSpec, payload: dict[str, Any]
    ) -> tuple[str, Any, int]:
        implementation = self._implementations.get(extractor.implementation_id)
        if implementation is None:
            raise FoundationError(
                "extractor_implementation_untrusted", "Extractor implementation is not trusted by this host"
            )
        if extractor.implementation_digest != implementation.implementation_digest:
            raise FoundationError(
                "extractor_implementation_mismatch", "Extractor implementation digest does not match"
            )
        size = self.input_bytes(payload)
        if size > extractor.max_input_bytes:
            raise FoundationError(
                "extractor_input_budget_exceeded", "Extractor input exceeds its published byte budget"
            )
        value = implementation.extract(extractor.configuration, payload)
        if value is None:
            return "no_candidate", None, size
        if not self._matches_schema(value, extractor.output_schema):
            return "invalid", value, size
        return "candidate", value, size

    @staticmethod
    def _matches_schema(value: Any, schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        expected = schema.get("type")
        checks = {
            "string": lambda item: isinstance(item, str),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
        }
        return expected in checks and checks[expected](value)

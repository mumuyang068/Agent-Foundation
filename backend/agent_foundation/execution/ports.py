from __future__ import annotations

from typing import Any, Protocol

from .contracts import AlgorithmDescriptor


class CandidateAlgorithm(Protocol):
    descriptor: AlgorithmDescriptor

    def execute(
        self,
        payload: Any,
        *,
        parameters: dict[str, Any],
        budget: dict[str, int],
        query: str,
    ) -> Any: ...

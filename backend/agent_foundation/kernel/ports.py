from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from .primitives import ContractModel


class VersionedDocument(ContractModel):
    document_id: str
    revision: int = Field(ge=1)
    payload: dict[str, Any]


class DocumentRepository(Protocol):
    def put(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int | None,
    ) -> VersionedDocument: ...

    def get(self, collection: str, document_id: str) -> VersionedDocument | None: ...

    def list(self, collection: str, *, limit: int = 1000) -> tuple[VersionedDocument, ...]: ...


class ExecutionRecordRepository(Protocol):
    def append(self, payload: dict[str, Any]) -> None: ...

    def list_execution_records(self, *, limit: int = 1000) -> tuple[dict[str, Any], ...]: ...

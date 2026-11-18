from __future__ import annotations

from typing import Protocol

from agent_foundation.kernel.primitives import ResourceRef, ScopeRef

from .contracts import (
    EvidenceAlgorithmIdentity,
    EvidenceOperationReceipt,
    EvidenceRetrievalStatus,
    EvidenceRevision,
    EvidenceRevisionPage,
    EvidenceSearchResult,
    EvidenceSupport,
    SourceRange,
)


class EvidenceCandidateAlgorithm(Protocol):
    descriptor: EvidenceAlgorithmIdentity

    def rank(self, revisions: tuple[EvidenceRevision, ...], *, query: str, limit: int) -> tuple[tuple[str, float], ...]: ...


class EvidenceAuthorityReader(Protocol):
    def get_revision(self, scope: ScopeRef, revision_id: str) -> EvidenceRevision | None: ...

    def list_revisions(
        self,
        scope: ScopeRef,
        *,
        states: tuple[str, ...],
        source_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> EvidenceRevisionPage: ...

    def authority_watermark(self, scope: ScopeRef) -> int: ...

    def is_current(self, ref: ResourceRef) -> bool: ...

    def get_support(self, scope: ScopeRef, support_id: str) -> EvidenceSupport | None: ...


class EvidenceCandidateSource(Protocol):

    def candidates(
        self,
        scope: ScopeRef,
        *,
        query: str,
        source_ids: tuple[str, ...],
        revision_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[ResourceRef, ...]: ...


class EvidenceAuthority(EvidenceAuthorityReader, Protocol):
    def admit(
        self,
        scope: ScopeRef,
        *,
        source_id: str,
        content: str,
        source_range: SourceRange | None,
        operation_id: str,
        command_digest: str,
    ) -> EvidenceRevision: ...

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> EvidenceOperationReceipt | None: ...

    def revoke(
        self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str
    ) -> EvidenceRevision: ...

    def revalidate(
        self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str
    ) -> EvidenceRevision: ...

    def put_support(
        self, support: EvidenceSupport, *, operation_id: str, command_digest: str
    ) -> EvidenceSupport: ...

    def close(self) -> None: ...


class EvidenceService(Protocol):
    def admit(
        self,
        token: str,
        *,
        source_id: str,
        content: str,
        source_range: SourceRange | None = None,
        operation_id: str,
    ) -> EvidenceRevision: ...

    def get(self, token: str, revision_id: str) -> EvidenceRevision: ...

    def list(
        self,
        token: str,
        *,
        states: tuple[str, ...] = ("active",),
        source_id: str | None = None,
        limit: int = 8,
        cursor: str | None = None,
    ) -> EvidenceRevisionPage: ...

    def get_operation_receipt(self, token: str, operation_id: str) -> EvidenceOperationReceipt: ...

    def retrieval_status(self, token: str) -> EvidenceRetrievalStatus: ...

    def retrieval_watermark(self, token: str) -> int: ...

    def search(
        self,
        token: str,
        *,
        query: str,
        limit: int = 8,
        source_ids: tuple[str, ...] = (),
        revision_ids: tuple[str, ...] = (),
    ) -> EvidenceSearchResult: ...

    def revoke(self, token: str, revision_id: str, *, operation_id: str) -> EvidenceRevision: ...

    def revalidate(self, token: str, revision_id: str, *, operation_id: str) -> EvidenceRevision: ...

    def support(
        self,
        token: str,
        *,
        target: ResourceRef,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> EvidenceSupport: ...

    def get_support(self, token: str, support_id: str) -> EvidenceSupport: ...

from __future__ import annotations

from typing import Protocol

from agent_foundation.kernel.primitives import ResourceRef, ScopeRef

from .contracts import (
    ContextAlgorithmIdentity,
    ContextCandidate,
    ContextOperationReceipt,
    ContextSnapshot,
    ResidentContext,
)


class ContextSource(Protocol):
    domain: str

    def search(self, token: str, *, query: str, limit: int) -> tuple[ContextCandidate, ...]: ...

    def resolve(self, token: str, ref: ResourceRef) -> ContextCandidate: ...


class ContextCombinationAlgorithm(Protocol):
    descriptor: ContextAlgorithmIdentity

    def rank(
        self,
        candidates: tuple[ContextCandidate, ...],
        *,
        query: str,
        limit: int,
    ) -> tuple[tuple[ResourceRef, float], ...]: ...


class ContextAuthority(Protocol):
    def put_snapshot(
        self, snapshot: ContextSnapshot, *, operation_id: str, command_digest: str
    ) -> ContextSnapshot: ...

    def get_snapshot(self, scope: ScopeRef, snapshot_id: str) -> ContextSnapshot | None: ...

    def list_snapshots(self, scope: ScopeRef, *, limit: int) -> tuple[ContextSnapshot, ...]: ...

    def get_resident(self, scope: ScopeRef) -> ResidentContext | None: ...

    def replace_resident(
        self,
        resident: ResidentContext,
        *,
        expected_revision: int | None,
        operation_id: str,
        command_digest: str,
    ) -> ResidentContext: ...

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> ContextOperationReceipt | None: ...

    def close(self) -> None: ...

from __future__ import annotations

from typing import Protocol

from agent_foundation.kernel.primitives import DomainName, OperationContext, Principal, ResourceRef

from .contracts import (
    AccessGrant,
    CredentialSession,
    DelegationGrant,
    IssuedCredential,
    TrustedCallContext,
)
from .domain import AccessAuditEvent, AccessState


class VersionedAccessState(Protocol):
    revision: int
    state: AccessState


class AccessStateRepository(Protocol):
    def read(self) -> VersionedAccessState: ...

    def compare_and_swap(self, expected_revision: int, state: AccessState) -> VersionedAccessState: ...

    def close(self) -> None: ...


class AccessAuthorization(Protocol):
    def authorize_token(
        self,
        token: str,
        *,
        domain: DomainName,
        action: str,
        policy_id: str,
        policy_version: int,
        resource: ResourceRef | None = None,
        execution_profile_id: str | None = None,
        execution_profile_version: int | None = None,
    ) -> tuple[OperationContext, AccessGrant, TrustedCallContext]: ...


class AccessService(AccessAuthorization, Protocol):
    def put_grant(self, grant: AccessGrant) -> AccessGrant: ...

    def revoke_grant(self, grant_id: str) -> None: ...

    def get_grant(self, grant_id: str) -> tuple[AccessGrant, bool]: ...

    def list_grants(self) -> tuple[tuple[AccessGrant, bool], ...]: ...

    def put_delegation(self, delegation: DelegationGrant) -> DelegationGrant: ...

    def get_delegation(self, delegation_id: str) -> DelegationGrant: ...

    def revoke_delegation(self, delegation_id: str) -> DelegationGrant: ...

    def issue_session(
        self,
        principal: Principal,
        grant_id: str,
        *,
        ttl_seconds: int = 900,
    ) -> IssuedCredential: ...

    def get_session(self, session_id: str) -> CredentialSession: ...

    def revoke_session(self, session_id: str) -> CredentialSession: ...

    def audit_events(self) -> tuple[AccessAuditEvent, ...]: ...

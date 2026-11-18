from __future__ import annotations

from datetime import datetime

from pydantic import Field

from agent_foundation.kernel.primitives import ContractModel, DomainName

from .contracts import AccessGrant, CredentialSession, DelegationGrant


class AccessAuditEvent(ContractModel):
    event: str
    occurred_at: datetime
    grant_id: str | None = None
    delegation_id: str | None = None
    session_id: str | None = None
    action: str | None = None
    domain: DomainName | None = None


class AccessState(ContractModel):
    grants: dict[str, AccessGrant] = Field(default_factory=dict)
    revoked_grants: frozenset[str] = frozenset()
    delegations: dict[str, DelegationGrant] = Field(default_factory=dict)
    sessions: dict[str, CredentialSession] = Field(default_factory=dict)
    audit: tuple[AccessAuditEvent, ...] = ()

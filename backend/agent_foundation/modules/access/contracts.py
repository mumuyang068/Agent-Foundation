from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from agent_foundation.kernel.primitives import (
    ContractModel,
    DomainName,
    Principal,
    ScopeRef,
    stable_fingerprint,
)


class AccessGrant(ContractModel):
    schema_version: Literal["agent-foundation.access-grant.v1"] = "agent-foundation.access-grant.v1"
    grant_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=240)
    version: int = Field(default=1, ge=1)
    principal_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    domains: tuple[DomainName, ...] = Field(min_length=1)
    actions: tuple[str, ...] = Field(min_length=1, max_length=64)
    resource_types: tuple[str, ...] = ()
    limits: dict[str, int] = Field(default_factory=dict)
    expires_at: datetime | None = None
    fingerprint: str = ""

    @field_validator("domains", "actions", "resource_types")
    @classmethod
    def unique_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("Grant values cannot be empty")
        return tuple(sorted(set(value)))

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Grant expiry must be timezone-aware")
        return value

    @model_validator(mode="after")
    def set_fingerprint(self) -> "AccessGrant":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Access Grant fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class DelegationGrant(ContractModel):
    schema_version: Literal["agent-foundation.delegation-grant.v1"] = "agent-foundation.delegation-grant.v1"
    delegation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=240)
    actor_id: str = Field(min_length=1, max_length=240)
    represented_principal_id: str = Field(min_length=1, max_length=240)
    grant_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    expires_at: datetime
    revoked: bool = False
    fingerprint: str = ""

    @field_validator("expires_at")
    @classmethod
    def delegation_expiry_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Delegation expiry must be timezone-aware")
        return value

    @model_validator(mode="after")
    def set_fingerprint(self) -> "DelegationGrant":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Delegation Grant fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class CredentialSession(ContractModel):
    schema_version: Literal["agent-foundation.credential-session.v1"] = "agent-foundation.credential-session.v1"
    session_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=240)
    principal: Principal
    grant_id: str = Field(min_length=1, max_length=240)
    token_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    revoked: bool = False

    @field_validator("expires_at")
    @classmethod
    def session_expiry_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Session expiry must be timezone-aware")
        return value


class IssuedCredential(ContractModel):
    session_id: str
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: datetime
    principal: Principal
    grant_id: str


class TrustedCallContext(ContractModel):
    """Identity and scope derived from a live credential by Access."""

    session_id: str
    principal: Principal
    grant_id: str
    scope: ScopeRef
    grant_fingerprint: str

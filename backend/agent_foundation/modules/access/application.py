from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Callable, TypeVar

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import DomainName, OperationContext, Principal, ResourceRef, ScopeRef

from .contracts import (
    AccessGrant,
    CredentialSession,
    DelegationGrant,
    IssuedCredential,
    TrustedCallContext,
)
from .domain import AccessAuditEvent, AccessState
from .ports import AccessStateRepository


class AccessDenied(FoundationError):
    pass


T = TypeVar("T")


class AccessApplication:
    """Access authority that reloads durable state for every decision."""

    def __init__(self, repository: AccessStateRepository, *, max_conflict_retries: int = 4) -> None:
        self._repository = repository
        self._max_conflict_retries = max_conflict_retries

    def put_grant(self, grant: AccessGrant) -> AccessGrant:
        def change(state: AccessState, now: datetime) -> tuple[AccessState, AccessGrant]:
            current = state.grants.get(grant.grant_id)
            if current is not None and current != grant:
                raise FoundationError("grant_conflict", "Grant ID already has different content")
            grants = dict(state.grants)
            grants[grant.grant_id] = grant
            event = AccessAuditEvent(event="grant_put", occurred_at=now, grant_id=grant.grant_id)
            return state.model_copy(update={"grants": grants, "audit": (*state.audit, event)}), grant

        return self._mutate(change)

    def revoke_grant(self, grant_id: str) -> None:
        def change(state: AccessState, now: datetime) -> tuple[AccessState, None]:
            if grant_id not in state.grants:
                raise FoundationError("grant_not_found", "Grant does not exist")
            revoked = set(state.revoked_grants)
            revoked.add(grant_id)
            event = AccessAuditEvent(event="grant_revoked", occurred_at=now, grant_id=grant_id)
            return state.model_copy(update={"revoked_grants": frozenset(revoked), "audit": (*state.audit, event)}), None

        self._mutate(change)

    def get_grant(self, grant_id: str) -> tuple[AccessGrant, bool]:
        state = self._repository.read().state
        grant = state.grants.get(grant_id)
        if grant is None:
            raise FoundationError("grant_not_found", "Grant does not exist")
        return grant, grant_id in state.revoked_grants

    def list_grants(self) -> tuple[tuple[AccessGrant, bool], ...]:
        state = self._repository.read().state
        return tuple(
            (grant, grant_id in state.revoked_grants)
            for grant_id, grant in sorted(state.grants.items())
        )

    def put_delegation(self, delegation: DelegationGrant) -> DelegationGrant:
        def change(state: AccessState, now: datetime) -> tuple[AccessState, DelegationGrant]:
            grant = self._active_grant(state, delegation.grant_id)
            if grant.principal_id != delegation.represented_principal_id or grant.scope != delegation.scope:
                raise AccessDenied("delegation_grant_mismatch", "Delegation does not match the represented Grant")
            current = state.delegations.get(delegation.delegation_id)
            if current is not None and current != delegation:
                raise FoundationError("delegation_conflict", "Delegation ID already has different content")
            delegations = dict(state.delegations)
            delegations[delegation.delegation_id] = delegation
            event = AccessAuditEvent(
                event="delegation_put", occurred_at=now, grant_id=delegation.grant_id,
                delegation_id=delegation.delegation_id,
            )
            return state.model_copy(update={"delegations": delegations, "audit": (*state.audit, event)}), delegation

        return self._mutate(change)

    def get_delegation(self, delegation_id: str) -> DelegationGrant:
        delegation = self._repository.read().state.delegations.get(delegation_id)
        if delegation is None:
            raise FoundationError("delegation_not_found", "Delegation does not exist")
        return delegation

    def revoke_delegation(self, delegation_id: str) -> DelegationGrant:
        def change(state: AccessState, now: datetime) -> tuple[AccessState, DelegationGrant]:
            current = state.delegations.get(delegation_id)
            if current is None:
                raise FoundationError("delegation_not_found", "Delegation does not exist")
            payload = current.model_dump(mode="json")
            payload.update({"revoked": True, "fingerprint": ""})
            revoked = DelegationGrant.model_validate(payload)
            delegations = dict(state.delegations)
            delegations[delegation_id] = revoked
            event = AccessAuditEvent(
                event="delegation_revoked", occurred_at=now,
                grant_id=current.grant_id, delegation_id=delegation_id,
            )
            return state.model_copy(update={"delegations": delegations, "audit": (*state.audit, event)}), revoked

        return self._mutate(change)

    def issue_session(
        self,
        principal: Principal,
        grant_id: str,
        *,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> IssuedCredential:
        if not 1 <= ttl_seconds <= 86400:
            raise FoundationError("session_ttl_invalid", "Session TTL must be 1..86400 seconds")
        access_token = secrets.token_urlsafe(32)

        def change(state: AccessState, instant: datetime) -> tuple[AccessState, IssuedCredential]:
            grant = self._active_grant(state, grant_id)
            self._assert_principal(state, grant, principal, instant)
            expires_at = instant + timedelta(seconds=ttl_seconds)
            if grant.expires_at is not None:
                expires_at = min(expires_at, grant.expires_at)
            session = CredentialSession(
                principal=principal,
                grant_id=grant_id,
                token_digest=hashlib.sha256(access_token.encode()).hexdigest(),
                expires_at=expires_at,
            )
            sessions = dict(state.sessions)
            sessions[session.session_id] = session
            event = AccessAuditEvent(
                event="session_issued", occurred_at=instant, grant_id=grant_id, session_id=session.session_id
            )
            issued = IssuedCredential(
                session_id=session.session_id,
                access_token=access_token,
                expires_at=expires_at,
                principal=principal,
                grant_id=grant_id,
            )
            return state.model_copy(update={"sessions": sessions, "audit": (*state.audit, event)}), issued

        return self._mutate(change, now=now)

    def get_session(self, session_id: str) -> CredentialSession:
        session = self._repository.read().state.sessions.get(session_id)
        if session is None:
            raise FoundationError("session_not_found", "Credential session does not exist")
        return session

    def revoke_session(self, session_id: str) -> CredentialSession:
        def change(state: AccessState, now: datetime) -> tuple[AccessState, CredentialSession]:
            current = state.sessions.get(session_id)
            if current is None:
                raise FoundationError("session_not_found", "Credential session does not exist")
            revoked = current.model_copy(update={"revoked": True})
            sessions = dict(state.sessions)
            sessions[session_id] = revoked
            event = AccessAuditEvent(
                event="session_revoked", occurred_at=now,
                grant_id=current.grant_id, session_id=session_id,
            )
            return state.model_copy(update={"sessions": sessions, "audit": (*state.audit, event)}), revoked

        return self._mutate(change)

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
        now: datetime | None = None,
    ) -> tuple[OperationContext, AccessGrant, TrustedCallContext]:
        digest = hashlib.sha256(token.encode()).hexdigest()

        def change(state: AccessState, instant: datetime):
            session = next(
                (value for value in state.sessions.values() if secrets.compare_digest(value.token_digest, digest)),
                None,
            )
            if session is None or session.revoked or session.expires_at <= instant:
                raise AccessDenied("session_inactive", "Credential session is unavailable")
            grant = self._active_grant(state, session.grant_id)
            self._assert_principal(state, grant, session.principal, instant)
            self._assert_operation(grant, domain=domain, action=action, scope=grant.scope, resource=resource, now=instant)
            trusted = TrustedCallContext(
                session_id=session.session_id,
                principal=session.principal,
                grant_id=grant.grant_id,
                scope=grant.scope,
                grant_fingerprint=grant.fingerprint,
            )
            operation = OperationContext(
                principal=trusted.principal,
                grant_id=trusted.grant_id,
                scope=trusted.scope,
                policy_id=policy_id,
                policy_version=policy_version,
                execution_profile_id=execution_profile_id,
                execution_profile_version=execution_profile_version,
            )
            event = AccessAuditEvent(
                event="grant_authorized", occurred_at=instant, grant_id=grant.grant_id,
                session_id=session.session_id, action=action, domain=domain,
            )
            return state.model_copy(update={"audit": (*state.audit, event)}), (operation, grant, trusted)

        return self._mutate(change, now=now)

    def authorize(
        self,
        principal: Principal,
        grant_id: str,
        *,
        domain: DomainName,
        action: str,
        scope: ScopeRef,
        resource: ResourceRef | None = None,
        now: datetime | None = None,
    ) -> AccessGrant:
        def change(state: AccessState, instant: datetime) -> tuple[AccessState, AccessGrant]:
            grant = self._active_grant(state, grant_id)
            self._assert_principal(state, grant, principal, instant)
            self._assert_operation(grant, domain=domain, action=action, scope=scope, resource=resource, now=instant)
            event = AccessAuditEvent(
                event="grant_authorized", occurred_at=instant, grant_id=grant_id, action=action, domain=domain
            )
            return state.model_copy(update={"audit": (*state.audit, event)}), grant

        return self._mutate(change, now=now)

    def audit_events(self) -> tuple[AccessAuditEvent, ...]:
        return self._repository.read().state.audit

    def close(self) -> None:
        self._repository.close()

    def _mutate(
        self,
        change: Callable[[AccessState, datetime], tuple[AccessState, T]],
        *,
        now: datetime | None = None,
    ) -> T:
        instant = now or datetime.now(timezone.utc)
        for attempt in range(self._max_conflict_retries + 1):
            current = self._repository.read()
            updated, result = change(current.state, instant)
            try:
                self._repository.compare_and_swap(current.revision, updated)
                return result
            except FoundationError as error:
                if error.code != "access_state_conflict" or attempt == self._max_conflict_retries:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _active_grant(state: AccessState, grant_id: str) -> AccessGrant:
        grant = state.grants.get(grant_id)
        if grant is None or grant_id in state.revoked_grants:
            raise AccessDenied("grant_inactive", "Grant is unavailable")
        return grant

    @staticmethod
    def _assert_operation(
        grant: AccessGrant,
        *,
        domain: DomainName,
        action: str,
        scope: ScopeRef,
        resource: ResourceRef | None,
        now: datetime,
    ) -> None:
        if grant.scope != scope or domain not in grant.domains or action not in grant.actions:
            raise AccessDenied("grant_scope_denied", "Grant does not permit this operation")
        if resource is not None:
            if resource.domain != domain or resource.scope != scope:
                raise AccessDenied("grant_resource_denied", "Resource is outside the grant")
            if grant.resource_types and resource.resource_type not in grant.resource_types:
                raise AccessDenied("grant_resource_denied", "Resource type is outside the grant")
        if grant.expires_at is not None and grant.expires_at <= now:
            raise AccessDenied("grant_expired", "Grant has expired")

    @staticmethod
    def _assert_principal(
        state: AccessState,
        grant: AccessGrant,
        principal: Principal,
        now: datetime,
    ) -> None:
        if principal.delegation_id is None:
            if grant.principal_id != principal.principal_id:
                raise AccessDenied("grant_principal_mismatch", "Grant does not belong to the principal")
            return
        delegation = state.delegations.get(principal.delegation_id)
        if (
            delegation is None
            or delegation.revoked
            or delegation.expires_at <= now
            or delegation.actor_id != principal.principal_id
            or delegation.represented_principal_id != grant.principal_id
            or delegation.grant_id != grant.grant_id
            or delegation.scope != grant.scope
        ):
            raise AccessDenied("delegation_inactive", "Delegation does not authorize the principal")

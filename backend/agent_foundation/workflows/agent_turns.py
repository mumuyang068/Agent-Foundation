from __future__ import annotations

from datetime import datetime, timezone

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization
from agent_foundation.modules.applications.contracts import AgentTurn, ApplicationPolicy
from agent_foundation.modules.applications.ports import ContextTurnPort, MemoryTurnPort, TurnAuthority


class AgentTurnWorkflow:
    """Product-neutral turn workflow with recoverable domain command identities."""

    def __init__(
        self,
        authority: TurnAuthority,
        access: AccessAuthorization,
        policy: ApplicationPolicy,
        *,
        memory: MemoryTurnPort,
        context: ContextTurnPort | None = None,
    ) -> None:
        self._authority = authority
        self._access = access
        self._policy = policy
        self._memory = memory
        self._context = context

    def prepare(
        self,
        token: str,
        *,
        application_id: str,
        query: str,
        use_context: bool,
        source_domains: tuple[str, ...] = (),
        pinned_refs: tuple[ResourceRef, ...] = (),
        operation_id: str,
    ):
        operation, _grant, _trusted = self._authorize(token, "prepare")
        assembly = None
        if use_context:
            if self._context is None:
                raise FoundationError("application_context_unavailable", "Context is not enabled")
            assembly = self._context.assemble(
                token, query=query, source_domains=source_domains, pinned_refs=pinned_refs
            )
        digest = stable_fingerprint({
            "action": "prepare", "scope": operation.scope.model_dump(mode="json"),
            "application_id": application_id, "query": query, "use_context": use_context,
            "source_domains": list(source_domains),
            "pinned_refs": [ref.model_dump(mode="json") for ref in pinned_refs],
        })
        turn = AgentTurn(
            scope=operation.scope,
            principal_id=operation.principal.principal_id,
            application_id=application_id,
            context_fingerprint=assembly.fingerprint if assembly is not None else None,
            context_refs=tuple(item.ref for item in assembly.items) if assembly is not None else (),
        )
        return self._authority.create(turn, operation_id=operation_id, command_digest=digest), assembly

    def observe(
        self,
        token: str,
        turn_id: str,
        *,
        content: str,
        source_refs: tuple[ResourceRef, ...] = (),
        operation_id: str,
    ):
        operation, _grant, _trusted = self._authorize(token, "observe")
        turn = self._owned(operation.scope, operation.principal.principal_id, turn_id)
        if turn.state != "prepared":
            receipt = self._authority.get_receipt(operation.scope, operation_id)
            if receipt is not None:
                return turn, self._memory.get(token, turn.memory_id)
            raise FoundationError("application_turn_invalid", "Turn is not ready for observation")
        memory = self._memory.propose(
            token, content=content, source_refs=source_refs, operation_id=f"turn-memory:{operation_id}"
        )
        digest = stable_fingerprint({
            "action": "observe", "turn_id": turn_id, "content": content,
            "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
        })
        updated = turn.model_copy(update={
            "revision": turn.revision + 1,
            "state": "observed",
            "memory_id": memory.memory_id,
            "memory_revision": memory.revision,
            "updated_at": datetime.now(timezone.utc),
        })
        stored = self._authority.record_observation(
            updated, expected_revision=turn.revision,
            operation_id=operation_id, command_digest=digest,
        )
        return stored, memory

    def record_outcome(
        self,
        token: str,
        turn_id: str,
        *,
        value: float,
        expected_memory_revision: int,
        operation_id: str,
    ):
        operation, _grant, _trusted = self._authorize(token, "outcome")
        turn = self._owned(operation.scope, operation.principal.principal_id, turn_id)
        if turn.state != "observed" or turn.memory_id is None:
            receipt = self._authority.get_receipt(operation.scope, operation_id)
            if receipt is not None:
                return turn
            raise FoundationError("application_turn_invalid", "Turn has no observed Memory")
        self._memory.feedback(
            token,
            turn.memory_id,
            expected_revision=expected_memory_revision,
            value=value,
            operation_id=f"turn-feedback:{operation_id}",
        )
        digest = stable_fingerprint({
            "action": "outcome", "turn_id": turn_id, "value": value,
            "expected_memory_revision": expected_memory_revision,
        })
        updated = turn.model_copy(update={
            "revision": turn.revision + 1,
            "state": "completed",
            "updated_at": datetime.now(timezone.utc),
        })
        return self._authority.record_outcome(
            updated, expected_revision=turn.revision,
            operation_id=operation_id, command_digest=digest,
        )

    def get(self, token: str, turn_id: str):
        operation, _grant, _trusted = self._authorize(token, "read")
        return self._owned(operation.scope, operation.principal.principal_id, turn_id)

    def close(self):
        self._authority.close()

    def _authorize(self, token, action):
        return self._access.authorize_token(
            token,
            domain="applications",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
        )

    def _owned(self, scope, principal_id, turn_id):
        turn = self._authority.get(scope, turn_id)
        if turn is None:
            raise FoundationError("application_turn_not_found", "Agent turn does not exist")
        if turn.principal_id != principal_id:
            raise FoundationError("application_turn_authority_invalid", "Agent turn belongs to another principal")
        return turn

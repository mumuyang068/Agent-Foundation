from __future__ import annotations

from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import (
    ContextAssembly,
    ContextCandidate,
    ContextComparison,
    ContextExecutionRecord,
    ContextItem,
    ContextPolicy,
    ContextSnapshot,
    ContextSnapshotPage,
    ResidentContext,
)
from .ports import ContextAuthority, ContextCombinationAlgorithm, ContextSource


class ContextApplication:
    def __init__(
        self,
        authority: ContextAuthority,
        access: AccessAuthorization,
        policy: ContextPolicy,
        profile: ExecutionProfile,
        algorithm: ContextCombinationAlgorithm,
        sources: tuple[ContextSource, ...] = (),
    ) -> None:
        if profile.domain != "context":
            raise FoundationError("execution_profile_domain_invalid", "Context needs a Context profile")
        if (
            algorithm.descriptor.algorithm_id != profile.algorithm_id
            or algorithm.descriptor.implementation_version != profile.implementation_version
        ):
            raise FoundationError("algorithm_unavailable", "Context profile does not select this algorithm")
        by_domain = {source.domain: source for source in sources}
        if len(by_domain) != len(sources) or any(
            domain not in {"memory", "evidence", "graph"} for domain in by_domain
        ):
            raise FoundationError("context_source_invalid", "Context sources must have unique supported domains")
        self._authority = authority
        self._access = access
        self._policy = policy
        self._profile = profile
        self._algorithm = algorithm
        self._sources = by_domain

    @property
    def source_domains(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def assemble(
        self,
        token: str,
        *,
        query: str,
        source_domains: tuple[str, ...],
        pinned_refs: tuple[ResourceRef, ...] = (),
        max_items: int | None = None,
        max_chars: int | None = None,
    ) -> ContextAssembly:
        operation, grant, _trusted = self._authorize(token, "assemble")
        if not source_domains:
            raise FoundationError("context_sources_required", "Context assembly needs explicit sources")
        if len(set(source_domains)) != len(source_domains):
            raise FoundationError("context_selection_invalid", "Context sources must be unique")
        ref_keys = tuple(self._ref_key(ref) for ref in pinned_refs)
        if len(set(ref_keys)) != len(ref_keys):
            raise FoundationError("context_selection_invalid", "Pinned references must be unique")
        maximum_items = min(
            grant.limits.get("max_items", 1000),
            self._profile.budget.get("max_items", 1000),
            self._policy.max_items,
        )
        maximum_chars = min(
            grant.limits.get("max_chars", 10_000_000),
            self._profile.budget.get("max_chars", 10_000_000),
            self._policy.max_chars,
        )
        max_items = maximum_items if max_items is None else max_items
        max_chars = maximum_chars if max_chars is None else max_chars
        if not 1 <= max_items <= maximum_items or not 1 <= max_chars <= maximum_chars:
            raise FoundationError("context_budget_exceeded", "Context budget exceeds an authorized limit")

        selected = {domain: self._source(domain) for domain in source_domains}
        pinned: list[ContextCandidate] = []
        for ref in pinned_refs:
            if ref.scope != operation.scope or ref.domain not in selected or ref.revision is None:
                raise FoundationError("context_pin_scope_invalid", "Pinned reference is outside the exact selection")
            candidate = selected[ref.domain].resolve(token, ref)
            if candidate.ref != ref:
                raise FoundationError("context_resource_not_found", "Pinned reference is not current")
            pinned.append(candidate)
        if len(pinned) > max_items or sum(len(item.content) for item in pinned) > max_chars:
            raise FoundationError("context_pin_budget_exceeded", "Pinned resources must fit without truncation")

        candidates: dict[str, ContextCandidate] = {}
        pinned_keys = {self._ref_key(item.ref) for item in pinned}
        for domain, source in selected.items():
            for candidate in source.search(token, query=query, limit=max_items):
                key = self._ref_key(candidate.ref)
                if (
                    candidate.ref.domain != domain
                    or candidate.ref.scope != operation.scope
                    or candidate.ref.revision is None
                    or key in pinned_keys
                ):
                    continue
                current = candidates.get(key)
                if current is None or candidate.score > current.score:
                    candidates[key] = candidate
        ranked = self._algorithm.rank(tuple(candidates.values()), query=query, limit=max_items)

        # Candidate algorithms are untrusted. Reauthorize Context and resolve every exact source
        # revision again before content crosses the public boundary.
        operation, grant, _trusted = self._authorize(token, "assemble")
        current_ranked: list[ContextCandidate] = []
        for ref, score in ranked:
            candidate = candidates.get(self._ref_key(ref))
            if candidate is None or ref.domain not in selected:
                continue
            try:
                resolved = selected[ref.domain].resolve(token, ref)
            except FoundationError:
                continue
            if resolved.ref == ref:
                current_ranked.append(resolved.model_copy(update={"score": max(0.0, score)}))

        items: list[ContextItem] = []
        used_chars = 0
        for candidate, is_pinned in [*((value, True) for value in pinned), *((value, False) for value in current_ranked)]:
            if len(items) == max_items:
                break
            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            if is_pinned and len(candidate.content) > remaining:
                raise FoundationError("context_pin_budget_exceeded", "Pinned resources must fit without truncation")
            content = candidate.content if is_pinned else candidate.content[:remaining]
            if not content:
                continue
            items.append(ContextItem(ref=candidate.ref, content=content, score=candidate.score, pinned=is_pinned))
            used_chars += len(content)

        execution = ContextExecutionRecord(
            request_id=operation.request_id,
            grant_fingerprint=grant.fingerprint,
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            output_refs=tuple(item.ref for item in items),
            diagnostics={
                "source_domains": list(source_domains),
                "pinned_count": len(pinned),
                "used_items": len(items),
                "used_chars": used_chars,
                "algorithm_id": self._algorithm.descriptor.algorithm_id,
            },
        )
        return ContextAssembly(
            scope=operation.scope,
            assembled_by=operation.principal.principal_id,
            query=query,
            items=tuple(items),
            model_context="\n\n".join(
                f"[{item.ref.domain}:{item.ref.resource_id}@{item.ref.revision}]\n{item.content}"
                for item in items
            ),
            execution=execution,
        )

    def create_snapshot(
        self,
        token: str,
        assembly: ContextAssembly,
        *,
        retention: str = "provenance_only",
        operation_id: str,
    ) -> ContextSnapshot:
        operation, _grant, _trusted = self._authorize(token, "snapshot")
        self._validate_assembly_owner(operation.scope, operation.principal.principal_id, assembly)
        if retention not in {"full", "redacted", "provenance_only"}:
            raise FoundationError("context_snapshot_retention_invalid", "Snapshot retention is invalid")
        if retention == "full" and not self._policy.allow_full_snapshot:
            raise FoundationError("context_snapshot_retention_denied", "Full snapshots are disabled by policy")
        if retention == "full":
            retained_items, retained_context = assembly.items, assembly.model_context
        elif retention == "redacted":
            retained_items = tuple(item.model_copy(update={"content": "[redacted]"}) for item in assembly.items)
            retained_context = "[redacted]"
        else:
            retained_items, retained_context = (), ""
        snapshot = ContextSnapshot(
            scope=operation.scope,
            created_by=operation.principal.principal_id,
            source_refs=tuple(item.ref for item in assembly.items),
            source_domains=tuple(dict.fromkeys(item.ref.domain for item in assembly.items)),
            retention=retention,
            assembly_fingerprint=assembly.fingerprint,
            item_char_counts=tuple(len(item.content) for item in assembly.items),
            retained_items=retained_items,
            retained_model_context=retained_context,
        )
        digest = stable_fingerprint({
            "action": "create_snapshot",
            "scope": operation.scope.model_dump(mode="json"),
            "assembly_fingerprint": assembly.fingerprint,
            "retention": retention,
        })
        return self._authority.put_snapshot(snapshot, operation_id=operation_id, command_digest=digest)

    def create_snapshot_metadata(
        self,
        token: str,
        *,
        assembly_fingerprint: str,
        operation_id: str,
    ) -> ContextSnapshot:
        operation, _grant, _trusted = self._authorize(token, "snapshot")
        snapshot = ContextSnapshot(
            scope=operation.scope,
            created_by=operation.principal.principal_id,
            source_refs=(),
            source_domains=(),
            retention="provenance_only",
            assembly_fingerprint=assembly_fingerprint,
            item_char_counts=(),
        )
        digest = stable_fingerprint({
            "action": "create_snapshot_metadata",
            "scope": operation.scope.model_dump(mode="json"),
            "assembly_fingerprint": assembly_fingerprint,
        })
        return self._authority.put_snapshot(snapshot, operation_id=operation_id, command_digest=digest)

    def get_snapshot(self, token: str, snapshot_id: str) -> ContextSnapshot:
        operation, _grant, _trusted = self._authorize(token, "snapshot_read")
        snapshot = self._authority.get_snapshot(operation.scope, snapshot_id)
        if snapshot is None:
            raise FoundationError("context_snapshot_not_found", "Context snapshot does not exist")
        return snapshot

    def list_snapshots(self, token: str, *, limit: int = 20) -> ContextSnapshotPage:
        operation, grant, _trusted = self._authorize(token, "snapshot_read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("context_budget_exceeded", "Snapshot page exceeds budget")
        return ContextSnapshotPage(items=self._authority.list_snapshots(operation.scope, limit=limit))

    def replay(self, token: str, snapshot_id: str) -> ContextAssembly:
        snapshot = self.get_snapshot(token, snapshot_id)
        if not snapshot.source_domains:
            raise FoundationError("context_sources_required", "Snapshot has no source selection to replay")
        return self.assemble(
            token,
            query="snapshot replay",
            source_domains=tuple(snapshot.source_domains),
            pinned_refs=snapshot.source_refs,
            max_items=max(1, len(snapshot.source_refs)),
            max_chars=max(1, sum(snapshot.item_char_counts)),
        )

    def compare(self, token: str, left_snapshot_id: str, right_snapshot_id: str) -> ContextComparison:
        left = self.get_snapshot(token, left_snapshot_id)
        right = self.get_snapshot(token, right_snapshot_id)
        left_keys = {self._ref_key(ref) for ref in left.source_refs}
        right_keys = {self._ref_key(ref) for ref in right.source_refs}
        return ContextComparison(
            left_snapshot_id=left_snapshot_id,
            right_snapshot_id=right_snapshot_id,
            added_refs=tuple(ref for ref in right.source_refs if self._ref_key(ref) not in left_keys),
            removed_refs=tuple(ref for ref in left.source_refs if self._ref_key(ref) not in right_keys),
            order_changed=left.source_refs != right.source_refs,
            assembly_changed=left.assembly_fingerprint != right.assembly_fingerprint,
        )

    def preview_resident(self, token: str, assembly: ContextAssembly) -> ResidentContext:
        operation, _grant, _trusted = self._authorize(token, "resident_read")
        self._validate_assembly_owner(operation.scope, operation.principal.principal_id, assembly)
        current = self._authority.get_resident(operation.scope)
        return ResidentContext(
            scope=operation.scope,
            revision=1 if current is None else current.revision + 1,
            source_refs=tuple(item.ref for item in assembly.items),
            source_domains=tuple(dict.fromkeys(item.ref.domain for item in assembly.items)),
            assembly_fingerprint=assembly.fingerprint,
            replaced_by=operation.principal.principal_id,
        )

    def replace_resident(
        self,
        token: str,
        assembly: ContextAssembly,
        *,
        expected_revision: int | None,
        operation_id: str,
    ) -> ResidentContext:
        operation, _grant, _trusted = self._authorize(token, "resident_replace")
        self._validate_assembly_owner(operation.scope, operation.principal.principal_id, assembly)
        resident = self.preview_resident(token, assembly)
        digest = stable_fingerprint({
            "action": "replace_resident",
            "scope": operation.scope.model_dump(mode="json"),
            "assembly_fingerprint": assembly.fingerprint,
            "expected_revision": expected_revision,
        })
        return self._authority.replace_resident(
            resident,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
        )

    def get_resident(self, token: str) -> ResidentContext:
        operation, _grant, _trusted = self._authorize(token, "resident_read")
        resident = self._authority.get_resident(operation.scope)
        if resident is None:
            raise FoundationError("context_resident_not_found", "Resident Context does not exist")
        return resident

    def replay_resident(self, token: str) -> ContextAssembly:
        resident = self.get_resident(token)
        if not resident.source_domains:
            raise FoundationError("context_sources_required", "Resident Context has no source selection")
        return self.assemble(
            token,
            query="resident context replay",
            source_domains=tuple(resident.source_domains),
            pinned_refs=resident.source_refs,
            max_items=max(1, len(resident.source_refs)),
        )

    def get_operation_receipt(self, token: str, operation_id: str):
        operation, _grant, _trusted = self._authorize(token, "snapshot_read")
        receipt = self._authority.get_operation_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("context_receipt_not_found", "Context operation receipt does not exist")
        return receipt

    def close(self) -> None:
        self._authority.close()

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token,
            domain="context",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            execution_profile_id=self._profile.profile_id,
            execution_profile_version=self._profile.version,
        )

    def _source(self, domain: str) -> ContextSource:
        if domain not in {"memory", "evidence", "graph"}:
            raise FoundationError("context_source_unsupported", f"Context source {domain!r} is unsupported")
        source = self._sources.get(domain)
        if source is None:
            raise FoundationError("context_source_unavailable", f"Context source {domain!r} is not configured")
        return source

    @staticmethod
    def _ref_key(ref: ResourceRef) -> str:
        return ref.model_dump_json()

    @staticmethod
    def _validate_assembly_owner(scope, principal_id: str, assembly: ContextAssembly) -> None:
        if assembly.scope != scope or assembly.assembled_by != principal_id:
            raise FoundationError("context_assembly_authority_invalid", "Assembly belongs to another authority")

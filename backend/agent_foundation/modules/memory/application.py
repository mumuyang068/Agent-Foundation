from __future__ import annotations

from datetime import datetime, timezone
from itertools import islice
from math import isfinite

from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import (
    MemoryChangeSet,
    MemoryDreamingWindow,
    MemoryDreamingWindowPage,
    MemoryActivation,
    MemoryEntry,
    MemoryEntryPage,
    MemoryExecutionRecord,
    MemoryFeedbackEvent,
    MemoryFeedbackPage,
    MemoryFormationStatus,
    MemoryMaintenanceAction,
    MemoryMaintenancePlan,
    MemoryMaintenancePlanPage,
    MemoryMaintenancePlanRecord,
    MemoryOperationReceipt,
    MemoryPolicy,
    MemoryRecallHit,
    MemoryRecallResult,
    MemoryReinforcementDiagnostic,
    MemoryTrace,
)
from .ports import (
    MemoryAuthority,
    MemoryAuthorityReader,
    MemoryCandidateAlgorithm,
    MemoryCandidateSource,
    MemoryLinkPort,
    ResourceCurrentness,
)


class MemoryApplication:
    def __init__(
        self,
        authority: MemoryAuthority,
        access: AccessAuthorization,
        policy: MemoryPolicy,
        profile: ExecutionProfile,
        algorithm: MemoryCandidateAlgorithm,
        source_authority: ResourceCurrentness | None = None,
        *,
        reader: MemoryAuthorityReader | None = None,
        candidate_source: MemoryCandidateSource | None = None,
        link_port: MemoryLinkPort | None = None,
    ) -> None:
        if profile.domain != "memory":
            raise FoundationError("execution_profile_domain_invalid", "Memory needs a Memory profile")
        if (
            algorithm.descriptor.algorithm_id != profile.algorithm_id
            or algorithm.descriptor.implementation_version != profile.implementation_version
        ):
            raise FoundationError("algorithm_unavailable", "Memory profile does not select this algorithm")
        self._authority = authority
        self._access = access
        self._policy = policy
        self._profile = profile
        self._algorithm = algorithm
        self._source_authority = source_authority
        self._reader = reader or authority
        self._candidate_source = candidate_source or authority
        self._link_port = link_port

    def propose(
        self,
        token: str,
        *,
        content: str,
        operation_id: str,
        source_refs: tuple[ResourceRef, ...] = (),
    ) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "propose")
        if len(content) > self._policy.max_content_chars:
            raise FoundationError("memory_content_too_large", "Memory exceeds policy size")
        if any(ref.scope != operation.scope for ref in source_refs):
            raise FoundationError("memory_source_scope_invalid", "Memory source is outside scope")
        self._require_sources_current(source_refs)
        entry = MemoryEntry(
            scope=operation.scope,
            content=content,
            status="active" if self._policy.auto_accept else "candidate",
            source_refs=source_refs,
        )
        digest = stable_fingerprint({
            "action": "propose",
            "scope": operation.scope.model_dump(mode="json"),
            "content": content,
            "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
            "auto_accept": self._policy.auto_accept,
        })
        return self._authority.create(entry, operation_id=operation_id, command_digest=digest)

    def review(
        self,
        token: str,
        memory_id: str,
        *,
        approve: bool,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "review")
        current = self._required(operation.scope, memory_id)
        if current.revision == expected_revision and current.status != "candidate":
            raise FoundationError("memory_not_reviewable", "Only candidate Memory can be reviewed")
        updated = self._next(current, status="active" if approve else "rejected")
        digest = stable_fingerprint({
            "action": "review",
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "approve": approve,
        })
        return self._authority.update(
            updated,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
            action="review",
        )

    def get(self, token: str, memory_id: str) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "read")
        entry = self._required(operation.scope, memory_id)
        if entry.status != "active" or not self._sources_current(entry.source_refs):
            raise FoundationError("memory_not_found", "Memory is not currently usable")
        return entry

    def get_revision(self, token: str, memory_id: str, revision: int) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "read")
        entry = self._reader.get_revision(operation.scope, memory_id, revision)
        if entry is None:
            raise FoundationError("memory_not_found", "Memory revision does not exist")
        return entry

    def list(
        self,
        token: str,
        *,
        states: tuple[str, ...] = ("active",),
        limit: int = 8,
    ) -> MemoryEntryPage:
        operation, grant, _trusted = self._authorize(token, "read")
        allowed = {"candidate", "active", "rejected", "superseded", "revoked"}
        normalized = tuple(sorted(set(states)))
        if not normalized or any(state not in allowed for state in normalized):
            raise FoundationError("memory_query_invalid", "Memory states are invalid")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("memory_budget_exceeded", "Memory page exceeds the authorized budget")
        items = self._reader.list_current(operation.scope, states=normalized, limit=limit)
        current_items = tuple(
            entry
            for entry in items
            if entry.status != "active" or self._sources_current(entry.source_refs)
        )
        return MemoryEntryPage(
            items=current_items,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def trace(self, token: str, memory_id: str) -> MemoryTrace:
        operation, _grant, _trusted = self._authorize(token, "read")
        revisions = self._reader.history(operation.scope, memory_id)
        if not revisions:
            raise FoundationError("memory_not_found", "Memory does not exist")
        return MemoryTrace(memory_id=memory_id, revisions=revisions)

    def activation(self, token: str, memory_id: str) -> MemoryActivation:
        operation, _grant, _trusted = self._authorize(token, "read")
        entry = self._required(operation.scope, memory_id)
        sources_current = self._sources_current(entry.source_refs)
        return MemoryActivation(
            memory_id=entry.memory_id,
            revision=entry.revision,
            status=entry.status,
            sources_current=sources_current,
            feedback_count=entry.feedback_count,
            feedback_average=(entry.feedback_total / entry.feedback_count) if entry.feedback_count else 0,
        )

    def formation_status(self, token: str) -> MemoryFormationStatus:
        operation, _grant, _trusted = self._authorize(token, "read")
        counts = self._reader.state_counts(operation.scope)
        return MemoryFormationStatus(
            candidates=counts.get("candidate", 0),
            active=counts.get("active", 0),
            rejected=counts.get("rejected", 0),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def correct(
        self,
        token: str,
        memory_id: str,
        *,
        content: str,
        expected_revision: int,
        operation_id: str,
        source_refs: tuple[ResourceRef, ...] | None = None,
    ) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "correct")
        current = self._required(operation.scope, memory_id)
        if current.revision == expected_revision and current.status != "active":
            raise FoundationError("memory_not_correctable", "Only active Memory can be corrected")
        selected_sources = current.source_refs if source_refs is None else source_refs
        if any(ref.scope != operation.scope for ref in selected_sources):
            raise FoundationError("memory_source_scope_invalid", "Memory source is outside scope")
        self._require_sources_current(selected_sources)
        replacement = MemoryEntry(
            scope=operation.scope,
            content=content,
            status="active",
            source_refs=selected_sources,
        )
        digest = stable_fingerprint({
            "action": "correct",
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "content": content,
            "source_refs": [ref.model_dump(mode="json") for ref in selected_sources],
        })
        return self._authority.replace(
            current,
            replacement,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
            action="correct",
        )

    def revoke(
        self,
        token: str,
        memory_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "revoke")
        current = self._required(operation.scope, memory_id)
        if current.revision == expected_revision and current.status == "revoked":
            raise FoundationError("memory_not_revocable", "Memory is already revoked")
        updated = self._next(current, status="revoked")
        digest = stable_fingerprint({
            "action": "revoke", "memory_id": memory_id, "expected_revision": expected_revision,
        })
        return self._authority.update(
            updated,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
            action="revoke",
        )

    def feedback(
        self,
        token: str,
        memory_id: str,
        *,
        value: float,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry:
        if not -1 <= value <= 1:
            raise FoundationError("memory_feedback_invalid", "Feedback must be between -1 and 1")
        operation, _grant, _trusted = self._authorize(token, "feedback")
        current = self._required(operation.scope, memory_id)
        if current.revision == expected_revision and current.status != "active":
            raise FoundationError("memory_feedback_target_invalid", "Feedback target must be active")
        updated = self._next(
            current,
            feedback_total=current.feedback_total + value,
            feedback_count=current.feedback_count + 1,
        )
        event = MemoryFeedbackEvent(
            scope=operation.scope,
            memory_id=memory_id,
            value=value,
        )
        digest = stable_fingerprint({
            "action": "feedback",
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "value": value,
        })
        return self._authority.record_feedback(
            updated,
            event,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
        )

    def list_feedback(
        self, token: str, *, memory_id: str | None = None, limit: int = 8
    ) -> MemoryFeedbackPage:
        operation, grant, _trusted = self._authorize(token, "read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("memory_budget_exceeded", "Feedback page exceeds the authorized budget")
        if memory_id is not None and self._reader.get_current(operation.scope, memory_id) is None:
            raise FoundationError("memory_not_found", "Memory does not exist")
        return MemoryFeedbackPage(
            items=self._authority.list_feedback_events(
                operation.scope, memory_id=memory_id, limit=limit
            ),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def transition_feedback(
        self,
        token: str,
        event_id: str,
        *,
        status: str,
        reason: str,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryFeedbackEvent:
        operation, _grant, _trusted = self._authorize(token, "feedback")
        if status not in {"active", "contested", "retracted"}:
            raise FoundationError("memory_feedback_status_invalid", "Feedback status is invalid")
        current = self._authority.get_feedback_event(operation.scope, event_id)
        if current is None:
            raise FoundationError("memory_feedback_event_not_found", "Feedback event does not exist")
        allowed = {
            "active": {"contested", "retracted"},
            "contested": {"active", "retracted"},
            "retracted": set(),
        }
        if current.revision == expected_revision and status not in allowed[current.status]:
            raise FoundationError("memory_feedback_transition_invalid", "Feedback transition is invalid")
        updated = current.model_copy(update={
            "revision": current.revision + 1,
            "status": status,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc),
        })
        digest = stable_fingerprint({
            "action": "feedback_transition",
            "event_id": event_id,
            "status": status,
            "reason": reason,
            "expected_revision": expected_revision,
        })
        return self._authority.transition_feedback(
            updated,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
        )

    def reinforcement_diagnostic(
        self, token: str, memory_id: str
    ) -> MemoryReinforcementDiagnostic:
        operation, _grant, _trusted = self._authorize(token, "read")
        if self._reader.get_current(operation.scope, memory_id) is None:
            raise FoundationError("memory_not_found", "Memory does not exist")
        events = self._authority.list_feedback_events(
            operation.scope, memory_id=memory_id, limit=10_000
        )
        return MemoryReinforcementDiagnostic(
            memory_id=memory_id,
            active_count=sum(item.status == "active" for item in events),
            contested_count=sum(item.status == "contested" for item in events),
            retracted_count=sum(item.status == "retracted" for item in events),
            active_total=sum(item.value for item in events if item.status == "active"),
        )

    def freeze_dreaming_window(
        self, token: str, *, memory_ids: tuple[str, ...], operation_id: str
    ) -> MemoryDreamingWindow:
        operation, _grant, _trusted = self._authorize(token, "maintain_plan")
        normalized = tuple(sorted(set(memory_ids)))
        if len(normalized) != len(memory_ids) or not 2 <= len(normalized) <= 8:
            raise FoundationError(
                "memory_dreaming_window_invalid", "Dreaming window needs 2..8 unique Memory IDs"
            )
        digest = stable_fingerprint({
            "action": "maintain_window_dreaming",
            "scope": operation.scope.model_dump(mode="json"),
            "memory_ids": normalized,
        })
        replayed = self._replayed_window(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        entries = tuple(self._required(operation.scope, memory_id) for memory_id in normalized)
        if any(
            entry.status != "active" or not self._sources_current(entry.source_refs)
            for entry in entries
        ):
            raise FoundationError(
                "memory_dreaming_window_invalid", "Dreaming window needs current active Memory"
            )
        window = MemoryDreamingWindow(
            scope=operation.scope,
            source_refs=tuple(entry.ref() for entry in entries),
            basis_watermark=self._reader.authority_watermark(operation.scope),
        )
        return self._authority.save_dreaming_window(
            window,
            operation_id=operation_id,
            command_digest=digest,
        )

    def get_dreaming_window(self, token: str, window_id: str) -> MemoryDreamingWindow:
        operation, _grant, _trusted = self._authorize(token, "read")
        window = self._authority.get_dreaming_window(operation.scope, window_id)
        if window is None:
            raise FoundationError(
                "memory_dreaming_window_not_found", "Dreaming window does not exist"
            )
        return window

    def list_dreaming_windows(
        self, token: str, *, limit: int = 8
    ) -> MemoryDreamingWindowPage:
        operation, grant, _trusted = self._authorize(token, "read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("memory_budget_exceeded", "Dreaming window page exceeds budget")
        return MemoryDreamingWindowPage(
            items=self._authority.list_dreaming_windows(operation.scope, limit=limit),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def restore(
        self,
        token: str,
        memory_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry:
        operation, _grant, _trusted = self._authorize(token, "correct")
        current = self._required(operation.scope, memory_id)
        if current.revision == expected_revision and current.status != "revoked":
            raise FoundationError("memory_not_restorable", "Only revoked Memory can be restored")
        self._require_sources_current(current.source_refs)
        updated = self._next(current, status="active")
        digest = stable_fingerprint({
            "action": "restore", "memory_id": memory_id, "expected_revision": expected_revision,
        })
        return self._authority.update(
            updated,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
            action="restore",
        )

    def recall(self, token: str, *, query: str, limit: int = 8) -> MemoryRecallResult:
        operation, grant, _trusted = self._authorize(token, "read")
        maximum = min(grant.limits.get("max_items", 1000), self._profile.budget.get("max_items", 1000))
        if not 1 <= limit <= maximum:
            raise FoundationError("memory_budget_exceeded", "Memory limit exceeds the authorized budget")
        scan_limit = min(max(limit * 4, limit), maximum * 4, 4_000)
        proposed = self._candidate_source.candidates(
            operation.scope, query=query, limit=scan_limit
        )
        eligible: dict[str, MemoryEntry] = {}
        for ref in islice(iter(proposed), scan_limit):
            if (
                not isinstance(ref, ResourceRef)
                or ref.domain != "memory"
                or ref.scope != operation.scope
                or ref.resource_type != "memory"
                or ref.revision is None
            ):
                continue
            entry = self._reader.get_current(operation.scope, ref.resource_id)
            if (
                entry is not None
                and entry.ref() == ref
                and entry.status == "active"
                and self._sources_current(entry.source_refs)
            ):
                eligible.setdefault(entry.memory_id, entry)
        bounded = tuple(eligible.values())
        ranked = self._algorithm.rank(bounded, query=query, limit=limit)
        try:
            ranked_output = tuple(islice(iter(ranked), scan_limit))
        except TypeError as error:
            raise FoundationError(
                "memory_algorithm_output_invalid", "Memory algorithm output is not iterable"
            ) from error
        self._authorize(token, "read")
        hits = []
        seen: set[str] = set()
        invalid = duplicates = stale = 0
        for item in ranked_output:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], (int, float))
            ):
                invalid += 1
                continue
            memory_id = item[0]
            try:
                score = float(item[1])
            except (OverflowError, ValueError):
                invalid += 1
                continue
            if not isfinite(score) or score < 0:
                invalid += 1
                continue
            if memory_id in seen:
                duplicates += 1
                continue
            seen.add(memory_id)
            candidate = eligible.get(memory_id)
            if candidate is None:
                invalid += 1
                continue
            current = self._reader.get_current(operation.scope, memory_id)
            if (
                current is None
                or current.revision != candidate.revision
                or current.status != "active"
                or not self._sources_current(current.source_refs)
            ):
                stale += 1
                continue
            hits.append(MemoryRecallHit(entry=current, score=score))
            if len(hits) == limit:
                break
        self._authorize(token, "read")
        execution = MemoryExecutionRecord(
            request_id=operation.request_id,
            grant_fingerprint=grant.fingerprint,
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            output_refs=tuple(hit.entry.ref() for hit in hits),
            diagnostics={
                "eligible": len(bounded),
                "algorithm_id": self._algorithm.descriptor.algorithm_id,
                "scan_limit": scan_limit,
                "scanned": len(bounded),
                "algorithm_items_materialized": len(ranked_output),
                "duplicate_algorithm_items": duplicates,
                "invalid_algorithm_items": invalid,
                "stale_algorithm_items": stale,
            },
        )
        return MemoryRecallResult(
            hits=tuple(hits),
            execution=execution,
            algorithm=self._algorithm.descriptor,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def plan_capacity_maintenance(
        self, token: str, *, max_active: int, operation_id: str
    ) -> MemoryMaintenancePlanRecord:
        operation, _grant, _trusted = self._authorize(token, "maintain_plan")
        if not 1 <= max_active <= 10_000:
            raise FoundationError("memory_capacity_invalid", "Active capacity must be 1..10000")
        digest = stable_fingerprint({
            "action": "maintain_plan_capacity",
            "scope": operation.scope.model_dump(mode="json"),
            "max_active": max_active,
        })
        replayed = self._replayed_plan(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        active = sorted(
            self._reader.list_current(operation.scope, states=("active",), limit=10_001),
            key=lambda entry: (entry.feedback_total, entry.updated_at, entry.memory_id),
        )
        actions = tuple(
            MemoryMaintenanceAction(
                action="revoke", memory_id=entry.memory_id, expected_revision=entry.revision
            )
            for entry in active[: max(0, len(active) - max_active)]
        )
        plan = MemoryMaintenancePlan(
            kind="capacity_forgetting",
            actions=actions,
            basis_watermark=self._reader.authority_watermark(operation.scope),
        )
        return self._authority.save_maintenance_plan(
            MemoryMaintenancePlanRecord(scope=operation.scope, plan=plan),
            operation_id=operation_id,
            command_digest=digest,
        )

    def plan_rewrite_maintenance(
        self,
        token: str,
        *,
        kind: str,
        replacements: dict[str, str],
        rationale: str,
        operation_id: str,
    ) -> MemoryMaintenancePlanRecord:
        operation, _grant, _trusted = self._authorize(token, "maintain_plan")
        if kind not in {"reflective_rewrite", "normalization", "revision_set"}:
            raise FoundationError("memory_maintenance_invalid", "Memory maintenance kind is invalid")
        if not replacements:
            raise FoundationError("memory_maintenance_empty", "Rewrite maintenance needs targets")
        digest = stable_fingerprint({
            "action": "maintain_plan_rewrite",
            "scope": operation.scope.model_dump(mode="json"),
            "kind": kind,
            "replacements": replacements,
            "rationale": rationale,
        })
        replayed = self._replayed_plan(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        actions = []
        for memory_id, content in sorted(replacements.items()):
            current = self._required(operation.scope, memory_id)
            if current.status != "active":
                raise FoundationError("memory_maintenance_target_invalid", "Rewrite targets must be active")
            actions.append(MemoryMaintenanceAction(
                action="correct",
                memory_id=memory_id,
                expected_revision=current.revision,
                replacement_content=content,
            ))
        plan = MemoryMaintenancePlan(
            kind=kind,
            actions=tuple(actions),
            basis_watermark=self._reader.authority_watermark(operation.scope),
            rationale=rationale,
        )
        return self._authority.save_maintenance_plan(
            MemoryMaintenancePlanRecord(scope=operation.scope, plan=plan),
            operation_id=operation_id,
            command_digest=digest,
        )

    def plan_dreaming_maintenance(
        self,
        token: str,
        *,
        window_id: str,
        replacements: dict[str, str] | None = None,
        actions: tuple[MemoryMaintenanceAction | dict, ...] | None = None,
        rationale: str,
        operation_id: str,
        planner_id: str | None = None,
        planner_output: dict | None = None,
        draft: dict | None = None,
        proposal_only: bool = False,
    ) -> MemoryMaintenancePlanRecord:
        operation, _grant, _trusted = self._authorize(token, "maintain_plan")
        replacements = replacements or {}
        if bool(replacements) == bool(actions):
            raise FoundationError("memory_maintenance_empty", "Dreaming plan needs targets")
        try:
            explicit_actions = tuple(
                MemoryMaintenanceAction.model_validate(item) for item in (actions or ())
            )
        except ValueError as error:
            raise FoundationError(
                "memory_dreaming_action_invalid", "Dreaming action is invalid"
            ) from error
        if len({item.action_id for item in explicit_actions}) != len(explicit_actions):
            raise FoundationError(
                "memory_dreaming_action_duplicate", "Dreaming action IDs must be unique"
            )
        digest = stable_fingerprint({
            "action": "maintain_plan_dreaming",
            "scope": operation.scope.model_dump(mode="json"),
            "window_id": window_id,
            "replacements": replacements,
            "actions": [item.model_dump(mode="json") for item in explicit_actions],
            "rationale": rationale,
            "planner_id": planner_id,
            "planner_output": planner_output,
            "draft": draft,
            "proposal_only": proposal_only,
        })
        replayed = self._replayed_plan(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        window = self._authority.get_dreaming_window(operation.scope, window_id)
        if window is None:
            raise FoundationError(
                "memory_dreaming_window_not_found", "Dreaming window does not exist"
            )
        if self._reader.authority_watermark(operation.scope) != window.basis_watermark:
            raise FoundationError(
                "memory_dreaming_window_stale", "Memory changed after window freeze"
            )
        by_id = {ref.resource_id: ref for ref in window.source_refs}
        target_ids = set(replacements)
        for item in explicit_actions:
            if item.memory_id is not None:
                target_ids.add(item.memory_id)
            target_ids.update(item.memory_ids)
            if item.link_target is not None and (
                item.link_target.scope != operation.scope
                or item.link_target.domain != "graph"
            ):
                raise FoundationError(
                    "memory_dreaming_link_invalid",
                    "Dreaming link target must be a scoped Graph resource",
                )
        if not target_ids.issubset(by_id):
            raise FoundationError(
                "memory_dreaming_plan_invalid", "Dreaming targets are outside window"
            )
        planned_actions = []
        for memory_id, content in sorted(replacements.items()):
            current = self._required(operation.scope, memory_id)
            ref = by_id[memory_id]
            if current.status != "active" or ref.revision != str(current.revision):
                raise FoundationError(
                    "memory_dreaming_window_stale", "Dreaming input revision changed"
                )
            planned_actions.append(MemoryMaintenanceAction(
                action="correct",
                memory_id=memory_id,
                expected_revision=current.revision,
                replacement_content=content,
                disposition="plan_only" if proposal_only else "owner_decision",
            ))
        for item in explicit_actions:
            expected = (
                {item.memory_id: item.expected_revision}
                if item.memory_id is not None else item.expected_revisions
            )
            for memory_id, revision in expected.items():
                current = self._required(operation.scope, memory_id)
                ref = by_id[memory_id]
                if (
                    current.status != "active"
                    or revision != current.revision
                    or ref.revision != str(current.revision)
                ):
                    raise FoundationError(
                        "memory_dreaming_window_stale", "Dreaming input revision changed"
                    )
            planned_actions.append(
                item.model_copy(update={"disposition": "plan_only"})
                if proposal_only else item
            )
        plan = MemoryMaintenancePlan(
            kind="dreaming_v1",
            actions=tuple(planned_actions),
            basis_watermark=window.basis_watermark,
            rationale=rationale,
            input_window_id=window.window_id,
            planner_id=planner_id,
            planner_output=planner_output,
            draft=draft,
        )
        return self._authority.save_maintenance_plan(
            MemoryMaintenancePlanRecord(scope=operation.scope, plan=plan),
            operation_id=operation_id,
            command_digest=digest,
        )

    def get_maintenance_plan(self, token: str, plan_id: str) -> MemoryMaintenancePlanRecord:
        operation, _grant, _trusted = self._authorize(token, "read")
        record = self._authority.get_maintenance_plan(operation.scope, plan_id)
        if record is None:
            raise FoundationError("memory_maintenance_plan_not_found", "Maintenance plan does not exist")
        return record

    def list_maintenance_plans(
        self, token: str, *, limit: int = 8
    ) -> MemoryMaintenancePlanPage:
        operation, grant, _trusted = self._authorize(token, "read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("memory_budget_exceeded", "Maintenance plan page exceeds budget")
        return MemoryMaintenancePlanPage(
            items=self._authority.list_maintenance_plans(operation.scope, limit=limit),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def apply_maintenance(
        self,
        token: str,
        plan_id: str,
        *,
        operation_id: str,
    ) -> MemoryChangeSet:
        operation, _grant, _trusted = self._authorize(token, "maintain_apply")
        digest = stable_fingerprint({
            "action": "maintain_apply",
            "scope": operation.scope.model_dump(mode="json"),
            "plan_id": plan_id,
        })
        result = self._authority.apply_maintenance(
            operation.scope,
            plan_id,
            operation_id=operation_id,
            command_digest=digest,
        )
        if self._link_port is None:
            return result
        record = self._authority.get_maintenance_plan(operation.scope, plan_id)
        if record is None:
            raise FoundationError(
                "memory_maintenance_plan_not_found", "Maintenance plan does not exist"
            )
        actions = {item.action_id: item for item in record.plan.actions}
        for receipt in result.action_receipts:
            if receipt.status != "graph_pending":
                continue
            action = actions[receipt.action_id]
            source = self._required(operation.scope, action.memory_id)
            try:
                status, proposal_ref = self._link_port.propose_link(
                    token, source, action, operation_id=receipt.operation_id
                )
            except FoundationError:
                continue
            self._authorize(token, "maintain_apply")
            result = self._authority.record_maintenance_link(
                operation.scope,
                plan_id,
                action.action_id,
                status=status,
                proposal_ref=proposal_ref,
            )
        return result

    def rollback_maintenance(
        self,
        token: str,
        applied_operation_id: str,
        *,
        operation_id: str,
    ) -> MemoryChangeSet:
        operation, _grant, _trusted = self._authorize(token, "maintain_apply")
        digest = stable_fingerprint({
            "action": "maintain_rollback",
            "scope": operation.scope.model_dump(mode="json"),
            "applied_operation_id": applied_operation_id,
        })
        return self._authority.rollback_maintenance(
            operation.scope,
            applied_operation_id,
            operation_id=operation_id,
            command_digest=digest,
        )

    def get_operation_receipt(self, token: str, operation_id: str) -> MemoryOperationReceipt:
        operation, _grant, _trusted = self._authorize(token, "read")
        receipt = self._authority.get_operation_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("memory_receipt_not_found", "Memory operation receipt does not exist")
        return receipt

    def is_current(self, ref: ResourceRef) -> bool:
        if ref.domain != "memory" or ref.resource_type != "memory" or ref.revision is None:
            return False
        entry = self._reader.get_current(ref.scope, ref.resource_id)
        return bool(
            entry is not None
            and entry.status == "active"
            and str(entry.revision) == ref.revision
            and self._sources_current(entry.source_refs)
        )

    def close(self) -> None:
        try:
            if self._candidate_source is not self._authority:
                close = getattr(self._candidate_source, "close", None)
                if close is not None:
                    close()
        finally:
            self._authority.close()

    def _replayed_plan(
        self, scope: ScopeRef, operation_id: str, command_digest: str
    ) -> MemoryMaintenancePlanRecord | None:
        receipt = self._authority.get_operation_receipt(scope, operation_id)
        if receipt is None:
            return None
        if receipt.command_digest != command_digest:
            raise FoundationError("operation_id_conflict", "Operation ID was used for another command")
        if len(receipt.resource_ids) != 1:
            raise FoundationError("memory_maintenance_receipt_invalid", "Plan receipt is invalid")
        record = self._authority.get_maintenance_plan(scope, receipt.resource_ids[0])
        if record is None:
            raise FoundationError("memory_maintenance_plan_not_found", "Maintenance plan does not exist")
        return record

    def _replayed_window(
        self, scope: ScopeRef, operation_id: str, command_digest: str
    ) -> MemoryDreamingWindow | None:
        receipt = self._authority.get_operation_receipt(scope, operation_id)
        if receipt is None:
            return None
        if receipt.command_digest != command_digest:
            raise FoundationError("operation_id_conflict", "Operation ID was used for another command")
        if len(receipt.resource_ids) != 1:
            raise FoundationError("memory_maintenance_receipt_invalid", "Window receipt is invalid")
        window = self._authority.get_dreaming_window(scope, receipt.resource_ids[0])
        if window is None:
            raise FoundationError(
                "memory_dreaming_window_not_found", "Dreaming window does not exist"
            )
        return window

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token,
            domain="memory",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            execution_profile_id=self._profile.profile_id,
            execution_profile_version=self._profile.version,
        )

    def _required(self, scope, memory_id: str) -> MemoryEntry:
        entry = self._reader.get_current(scope, memory_id)
        if entry is None:
            raise FoundationError("memory_not_found", "Memory does not exist")
        return entry

    def _sources_current(self, refs: tuple[ResourceRef, ...]) -> bool:
        if not self._policy.require_current_sources or not refs:
            return True
        return self._source_authority is not None and all(
            self._source_authority.is_current(ref) for ref in refs
        )

    def _require_sources_current(self, refs: tuple[ResourceRef, ...]) -> None:
        if refs and not self._sources_current(refs):
            code = "memory_source_authority_unavailable" if self._source_authority is None else "memory_source_stale"
            raise FoundationError(code, "Memory sources are unavailable")

    @staticmethod
    def _next(entry: MemoryEntry, **changes) -> MemoryEntry:
        return entry.model_copy(update={
            **changes,
            "revision": entry.revision + 1,
            "updated_at": datetime.now(timezone.utc),
        })

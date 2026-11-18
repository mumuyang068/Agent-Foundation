from __future__ import annotations

from typing import Protocol

from agent_foundation.kernel.primitives import ResourceRef, ScopeRef

from .contracts import (
    MemoryAlgorithmIdentity,
    MemoryActivation,
    MemoryChangeSet,
    MemoryDreamingWindow,
    MemoryDreamingWindowPage,
    MemoryEntry,
    MemoryEntryPage,
    MemoryFeedbackEvent,
    MemoryFeedbackPage,
    MemoryFormationStatus,
    MemoryMaintenancePlanPage,
    MemoryMaintenancePlanRecord,
    MemoryMaintenanceAction,
    MemoryOperationReceipt,
    MemoryRecallResult,
    MemoryReinforcementDiagnostic,
    MemoryTrace,
)


class ResourceCurrentness(Protocol):
    def is_current(self, ref: ResourceRef) -> bool: ...


class MemoryCandidateAlgorithm(Protocol):
    descriptor: MemoryAlgorithmIdentity

    def rank(
        self,
        entries: tuple[MemoryEntry, ...],
        *,
        query: str,
        limit: int,
    ) -> tuple[tuple[str, float], ...]: ...


class MemoryAuthorityReader(Protocol):
    def get_current(self, scope: ScopeRef, memory_id: str) -> MemoryEntry | None: ...

    def get_revision(self, scope: ScopeRef, memory_id: str, revision: int) -> MemoryEntry | None: ...

    def list_current(
        self, scope: ScopeRef, *, states: tuple[str, ...], limit: int
    ) -> tuple[MemoryEntry, ...]: ...

    def state_counts(self, scope: ScopeRef) -> dict[str, int]: ...

    def history(self, scope: ScopeRef, memory_id: str) -> tuple[MemoryEntry, ...]: ...

    def authority_watermark(self, scope: ScopeRef) -> int: ...


class MemoryCandidateSource(Protocol):
    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]: ...


class MemoryLinkPort(Protocol):
    def propose_link(
        self,
        token: str,
        source: MemoryEntry,
        action: MemoryMaintenanceAction,
        *,
        operation_id: str,
    ) -> tuple[str, ResourceRef]: ...


class MemoryAuthority(MemoryAuthorityReader, Protocol):
    def create(
        self,
        entry: MemoryEntry,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryEntry: ...

    def replace(
        self,
        current: MemoryEntry,
        replacement: MemoryEntry,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        action: str,
    ) -> MemoryEntry: ...

    def update(
        self,
        entry: MemoryEntry,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        action: str,
    ) -> MemoryEntry: ...

    def record_feedback(
        self,
        entry: MemoryEntry,
        event: MemoryFeedbackEvent,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> MemoryEntry: ...

    def get_feedback_event(
        self, scope: ScopeRef, event_id: str
    ) -> MemoryFeedbackEvent | None: ...

    def list_feedback_events(
        self, scope: ScopeRef, *, memory_id: str | None, limit: int
    ) -> tuple[MemoryFeedbackEvent, ...]: ...

    def transition_feedback(
        self,
        event: MemoryFeedbackEvent,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> MemoryFeedbackEvent: ...

    def save_dreaming_window(
        self,
        window: MemoryDreamingWindow,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryDreamingWindow: ...

    def get_dreaming_window(
        self, scope: ScopeRef, window_id: str
    ) -> MemoryDreamingWindow | None: ...

    def list_dreaming_windows(
        self, scope: ScopeRef, *, limit: int
    ) -> tuple[MemoryDreamingWindow, ...]: ...

    def save_maintenance_plan(
        self,
        record: MemoryMaintenancePlanRecord,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryMaintenancePlanRecord: ...

    def get_maintenance_plan(
        self, scope: ScopeRef, plan_id: str
    ) -> MemoryMaintenancePlanRecord | None: ...

    def list_maintenance_plans(
        self, scope: ScopeRef, *, limit: int
    ) -> tuple[MemoryMaintenancePlanRecord, ...]: ...

    def apply_maintenance(
        self,
        scope: ScopeRef,
        plan_id: str,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryChangeSet: ...

    def record_maintenance_link(
        self,
        scope: ScopeRef,
        plan_id: str,
        action_id: str,
        *,
        status: str,
        proposal_ref: ResourceRef,
    ) -> MemoryChangeSet: ...

    def rollback_maintenance(
        self,
        scope: ScopeRef,
        applied_operation_id: str,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryChangeSet: ...

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> MemoryOperationReceipt | None: ...

    def close(self) -> None: ...


class MemoryService(Protocol):
    def propose(
        self,
        token: str,
        *,
        content: str,
        operation_id: str,
        source_refs: tuple[ResourceRef, ...] = (),
    ) -> MemoryEntry: ...

    def review(
        self,
        token: str,
        memory_id: str,
        *,
        approve: bool,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry: ...

    def get(self, token: str, memory_id: str) -> MemoryEntry: ...

    def get_revision(self, token: str, memory_id: str, revision: int) -> MemoryEntry: ...

    def list(
        self,
        token: str,
        *,
        states: tuple[str, ...] = ("active",),
        limit: int = 8,
    ) -> MemoryEntryPage: ...

    def trace(self, token: str, memory_id: str) -> MemoryTrace: ...

    def activation(self, token: str, memory_id: str) -> MemoryActivation: ...

    def formation_status(self, token: str) -> MemoryFormationStatus: ...

    def correct(
        self,
        token: str,
        memory_id: str,
        *,
        content: str,
        expected_revision: int,
        operation_id: str,
        source_refs: tuple[ResourceRef, ...] | None = None,
    ) -> MemoryEntry: ...

    def revoke(
        self,
        token: str,
        memory_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry: ...

    def restore(
        self,
        token: str,
        memory_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry: ...

    def feedback(
        self,
        token: str,
        memory_id: str,
        *,
        value: float,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryEntry: ...

    def list_feedback(
        self, token: str, *, memory_id: str | None = None, limit: int = 8
    ) -> MemoryFeedbackPage: ...

    def transition_feedback(
        self,
        token: str,
        event_id: str,
        *,
        status: str,
        reason: str,
        expected_revision: int,
        operation_id: str,
    ) -> MemoryFeedbackEvent: ...

    def reinforcement_diagnostic(
        self, token: str, memory_id: str
    ) -> MemoryReinforcementDiagnostic: ...

    def freeze_dreaming_window(
        self, token: str, *, memory_ids: tuple[str, ...], operation_id: str
    ) -> MemoryDreamingWindow: ...

    def get_dreaming_window(self, token: str, window_id: str) -> MemoryDreamingWindow: ...

    def list_dreaming_windows(
        self, token: str, *, limit: int = 8
    ) -> MemoryDreamingWindowPage: ...

    def recall(self, token: str, *, query: str, limit: int = 8) -> MemoryRecallResult: ...

    def plan_capacity_maintenance(
        self, token: str, *, max_active: int, operation_id: str
    ) -> MemoryMaintenancePlanRecord: ...

    def plan_rewrite_maintenance(
        self,
        token: str,
        *,
        kind: str,
        replacements: dict[str, str],
        rationale: str,
        operation_id: str,
    ) -> MemoryMaintenancePlanRecord: ...

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
    ) -> MemoryMaintenancePlanRecord: ...

    def get_maintenance_plan(self, token: str, plan_id: str) -> MemoryMaintenancePlanRecord: ...

    def list_maintenance_plans(
        self, token: str, *, limit: int = 8
    ) -> MemoryMaintenancePlanPage: ...

    def apply_maintenance(
        self,
        token: str,
        plan_id: str,
        *,
        operation_id: str,
    ) -> MemoryChangeSet: ...

    def rollback_maintenance(
        self,
        token: str,
        applied_operation_id: str,
        *,
        operation_id: str,
    ) -> MemoryChangeSet: ...

    def get_operation_receipt(self, token: str, operation_id: str) -> MemoryOperationReceipt: ...

from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import RLock

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.memory.contracts import (
    MemoryChangeSet,
    MemoryDreamingWindow,
    MemoryEntry,
    MemoryFeedbackEvent,
    MemoryMaintenanceActionReceipt,
    MemoryMaintenancePlanRecord,
    MemoryOperationReceipt,
)


def scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class RelationalMemoryAuthority:
    """Store-neutral Memory transaction behavior over a small SQL connection port."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self._lock = RLock()

    def create(
        self,
        entry: MemoryEntry,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryEntry:
        with self._lock, self._connection:
            existing = self._existing(entry.scope, operation_id, command_digest, "entry", MemoryEntry)
            if existing is not None:
                return existing
            if self.get_current(entry.scope, entry.memory_id) is not None:
                raise FoundationError("memory_identity_conflict", "Memory identity already exists")
            self._insert(entry)
            self._record(
                entry.scope,
                operation_id,
                command_digest,
                "propose",
                "entry",
                entry,
                (entry.memory_id,),
            )
            return entry

    def get_current(self, scope: ScopeRef, memory_id: str) -> MemoryEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_memory_revisions WHERE scope_key=? AND memory_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (scope_key(scope), memory_id),
            ).fetchone()
        return None if row is None else MemoryEntry.model_validate_json(row[0])

    def get_revision(self, scope: ScopeRef, memory_id: str, revision: int) -> MemoryEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_memory_revisions WHERE scope_key=? AND memory_id=? AND revision=?",
                (scope_key(scope), memory_id, revision),
            ).fetchone()
        return None if row is None else MemoryEntry.model_validate_json(row[0])

    def list_current(
        self, scope: ScopeRef, *, states: tuple[str, ...], limit: int
    ) -> tuple[MemoryEntry, ...]:
        parameters: list[object] = [scope_key(scope), *states, limit]
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.payload FROM af_memory_revisions r "
                "WHERE r.scope_key=? AND r.revision=(SELECT MAX(x.revision) FROM af_memory_revisions x "
                "WHERE x.scope_key=r.scope_key AND x.memory_id=r.memory_id) "
                f"AND json_extract(r.payload, '$.status') IN ({','.join('?' for _ in states)}) "
                "ORDER BY json_extract(r.payload, '$.updated_at') DESC, r.memory_id LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(MemoryEntry.model_validate_json(row[0]) for row in rows)

    def state_counts(self, scope: ScopeRef) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT json_extract(r.payload, '$.status') AS state, COUNT(*) AS total "
                "FROM af_memory_revisions r WHERE r.scope_key=? AND "
                "r.revision=(SELECT MAX(x.revision) FROM af_memory_revisions x "
                "WHERE x.scope_key=r.scope_key AND x.memory_id=r.memory_id) GROUP BY state",
                (scope_key(scope),),
            ).fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def history(self, scope: ScopeRef, memory_id: str) -> tuple[MemoryEntry, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM af_memory_revisions WHERE scope_key=? AND memory_id=? ORDER BY revision",
                (scope_key(scope), memory_id),
            ).fetchall()
        return tuple(MemoryEntry.model_validate_json(row[0]) for row in rows)

    def candidates(self, scope: ScopeRef, *, limit: int) -> tuple[MemoryEntry, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.payload FROM af_memory_revisions r "
                "WHERE r.scope_key=? AND r.revision=(SELECT MAX(x.revision) FROM af_memory_revisions x "
                "WHERE x.scope_key=r.scope_key AND x.memory_id=r.memory_id) "
                "AND json_extract(r.payload, '$.status')='active' "
                "ORDER BY json_extract(r.payload, '$.updated_at') DESC, r.memory_id LIMIT ?",
                (scope_key(scope), limit),
            ).fetchall()
        return tuple(MemoryEntry.model_validate_json(row[0]) for row in rows)

    def replace(
        self,
        current: MemoryEntry,
        replacement: MemoryEntry,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        action: str,
    ) -> MemoryEntry:
        with self._lock, self._connection:
            existing = self._existing(current.scope, operation_id, command_digest, "entry", MemoryEntry)
            if existing is not None:
                return existing
            current = self._expect_current(
                current.scope, current.memory_id, expected_revision, required_status="active"
            )
            superseded = current.model_copy(update={
                "revision": current.revision + 1,
                "status": "superseded",
                "successor_id": replacement.memory_id,
                "updated_at": datetime.now(timezone.utc),
            })
            self._insert(superseded)
            self._insert(replacement)
            self._record(
                current.scope,
                operation_id,
                command_digest,
                action,
                "entry",
                replacement,
                (current.memory_id, replacement.memory_id),
            )
            return replacement

    def update(
        self,
        entry: MemoryEntry,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        action: str,
    ) -> MemoryEntry:
        with self._lock, self._connection:
            existing = self._existing(entry.scope, operation_id, command_digest, "entry", MemoryEntry)
            if existing is not None:
                return existing
            self._expect_current(entry.scope, entry.memory_id, expected_revision)
            if entry.revision != expected_revision + 1:
                raise FoundationError("memory_revision_invalid", "Memory update has an invalid revision")
            self._insert(entry)
            self._record(
                entry.scope,
                operation_id,
                command_digest,
                action,
                "entry",
                entry,
                (entry.memory_id,),
            )
            return entry

    def record_feedback(
        self,
        entry: MemoryEntry,
        event: MemoryFeedbackEvent,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> MemoryEntry:
        with self._lock, self._connection:
            existing = self._existing(entry.scope, operation_id, command_digest, "entry", MemoryEntry)
            if existing is not None:
                return existing
            self._expect_current(entry.scope, entry.memory_id, expected_revision, required_status="active")
            if entry.revision != expected_revision + 1:
                raise FoundationError("memory_revision_invalid", "Feedback update has an invalid revision")
            if event.scope != entry.scope or event.memory_id != entry.memory_id or event.revision != 1:
                raise FoundationError("memory_feedback_event_invalid", "Feedback event does not match Memory")
            self._insert(entry)
            self._insert_feedback(event)
            self._record(
                entry.scope,
                operation_id,
                command_digest,
                "feedback",
                "entry",
                entry,
                (entry.memory_id, event.event_id),
            )
            return entry

    def get_feedback_event(
        self, scope: ScopeRef, event_id: str
    ) -> MemoryFeedbackEvent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_memory_feedback_events WHERE scope_key=? AND event_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (scope_key(scope), event_id),
            ).fetchone()
        return None if row is None else MemoryFeedbackEvent.model_validate_json(row[0])

    def list_feedback_events(
        self, scope: ScopeRef, *, memory_id: str | None, limit: int
    ) -> tuple[MemoryFeedbackEvent, ...]:
        clauses = [
            "e.scope_key=?",
            "e.revision=(SELECT MAX(x.revision) FROM af_memory_feedback_events x "
            "WHERE x.scope_key=e.scope_key AND x.event_id=e.event_id)",
        ]
        parameters: list[object] = [scope_key(scope)]
        if memory_id is not None:
            clauses.append("e.memory_id=?")
            parameters.append(memory_id)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT e.payload FROM af_memory_feedback_events e WHERE {' AND '.join(clauses)} "
                "ORDER BY json_extract(e.payload, '$.updated_at') DESC, e.event_id LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(MemoryFeedbackEvent.model_validate_json(row[0]) for row in rows)

    def transition_feedback(
        self,
        event: MemoryFeedbackEvent,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> MemoryFeedbackEvent:
        with self._lock, self._connection:
            existing = self._existing(
                event.scope, operation_id, command_digest, "feedback_event", MemoryFeedbackEvent
            )
            if existing is not None:
                return existing
            current_event = self.get_feedback_event(event.scope, event.event_id)
            if current_event is None:
                raise FoundationError("memory_feedback_event_not_found", "Feedback event does not exist")
            if current_event.revision != expected_revision:
                raise FoundationError("memory_feedback_version_conflict", "Feedback event revision changed")
            if event.revision != expected_revision + 1:
                raise FoundationError("memory_feedback_event_invalid", "Feedback event revision is invalid")
            memory = self.get_current(event.scope, event.memory_id)
            if memory is None:
                raise FoundationError("memory_not_found", "Feedback Memory does not exist")
            delta_total = 0.0
            delta_count = 0
            if current_event.status == "active" and event.status != "active":
                delta_total = -current_event.value
                delta_count = -1
            elif current_event.status != "active" and event.status == "active":
                delta_total = current_event.value
                delta_count = 1
            self._insert_feedback(event)
            resource_ids = [event.event_id]
            if delta_count:
                updated = memory.model_copy(update={
                    "revision": memory.revision + 1,
                    "feedback_total": memory.feedback_total + delta_total,
                    "feedback_count": memory.feedback_count + delta_count,
                    "updated_at": datetime.now(timezone.utc),
                })
                if updated.feedback_count < 0:
                    raise FoundationError("memory_feedback_projection_invalid", "Feedback projection is inconsistent")
                self._insert(updated)
                resource_ids.append(memory.memory_id)
            self._record(
                event.scope,
                operation_id,
                command_digest,
                f"feedback_{event.status}",
                "feedback_event",
                event,
                tuple(resource_ids),
            )
            return event

    def save_dreaming_window(
        self,
        window: MemoryDreamingWindow,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryDreamingWindow:
        with self._lock, self._connection:
            existing = self._existing(
                window.scope,
                operation_id,
                command_digest,
                "dreaming_window",
                MemoryDreamingWindow,
            )
            if existing is not None:
                return existing
            row = self._connection.execute(
                "SELECT 1 FROM af_memory_dreaming_windows WHERE scope_key=? AND window_id=?",
                (scope_key(window.scope), window.window_id),
            ).fetchone()
            if row is not None:
                raise FoundationError("memory_dreaming_window_conflict", "Dreaming window identity exists")
            self._connection.execute(
                "INSERT INTO af_memory_dreaming_windows(scope_key, window_id, payload) VALUES(?,?,?)",
                (scope_key(window.scope), window.window_id, window.model_dump_json()),
            )
            self._record(
                window.scope,
                operation_id,
                command_digest,
                "maintain_window_dreaming",
                "dreaming_window",
                window,
                (window.window_id,),
            )
            return window

    def get_dreaming_window(
        self, scope: ScopeRef, window_id: str
    ) -> MemoryDreamingWindow | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_memory_dreaming_windows WHERE scope_key=? AND window_id=?",
                (scope_key(scope), window_id),
            ).fetchone()
        return None if row is None else MemoryDreamingWindow.model_validate_json(row[0])

    def list_dreaming_windows(
        self, scope: ScopeRef, *, limit: int
    ) -> tuple[MemoryDreamingWindow, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM af_memory_dreaming_windows WHERE scope_key=? "
                "ORDER BY json_extract(payload, '$.created_at') DESC, window_id LIMIT ?",
                (scope_key(scope), limit),
            ).fetchall()
        return tuple(MemoryDreamingWindow.model_validate_json(row[0]) for row in rows)

    def save_maintenance_plan(
        self,
        record: MemoryMaintenancePlanRecord,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryMaintenancePlanRecord:
        with self._lock, self._connection:
            existing = self._existing(
                record.scope,
                operation_id,
                command_digest,
                "maintenance_plan",
                MemoryMaintenancePlanRecord,
            )
            if existing is not None:
                return existing
            row = self._connection.execute(
                "SELECT 1 FROM af_memory_maintenance_plans WHERE scope_key=? AND plan_id=?",
                (scope_key(record.scope), record.plan.plan_id),
            ).fetchone()
            if row is not None:
                raise FoundationError("memory_maintenance_plan_conflict", "Maintenance plan identity exists")
            self._put_maintenance_plan(record)
            self._record(
                record.scope,
                operation_id,
                command_digest,
                f"maintain_plan_{record.plan.kind}",
                "maintenance_plan",
                record,
                (record.plan.plan_id,),
            )
            return record

    def get_maintenance_plan(
        self, scope: ScopeRef, plan_id: str
    ) -> MemoryMaintenancePlanRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_memory_maintenance_plans WHERE scope_key=? AND plan_id=?",
                (scope_key(scope), plan_id),
            ).fetchone()
        return None if row is None else MemoryMaintenancePlanRecord.model_validate_json(row[0])

    def list_maintenance_plans(
        self, scope: ScopeRef, *, limit: int
    ) -> tuple[MemoryMaintenancePlanRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM af_memory_maintenance_plans WHERE scope_key=? "
                "ORDER BY json_extract(payload, '$.created_at') DESC, plan_id LIMIT ?",
                (scope_key(scope), limit),
            ).fetchall()
        return tuple(MemoryMaintenancePlanRecord.model_validate_json(row[0]) for row in rows)

    def apply_maintenance(
        self,
        scope: ScopeRef,
        plan_id: str,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryChangeSet:
        with self._lock, self._connection:
            existing = self._existing(
                scope, operation_id, command_digest, "change_set", MemoryChangeSet
            )
            if existing is not None:
                return existing
            plan_record = self.get_maintenance_plan(scope, plan_id)
            if plan_record is None:
                raise FoundationError("memory_maintenance_plan_not_found", "Maintenance plan does not exist")
            if plan_record.state not in {"planned", "graph_pending"}:
                raise FoundationError("memory_maintenance_plan_state_invalid", "Maintenance plan is not pending")
            plan = plan_record.plan
            if any(action.disposition == "blocked" for action in plan.actions):
                raise FoundationError(
                    "memory_maintenance_blocked", "A policy-blocked action cannot execute"
                )
            if any(action.disposition == "plan_only" for action in plan.actions):
                raise FoundationError(
                    "memory_maintenance_plan_only", "A proposal-only plan cannot execute"
                )
            window_entries = {}
            if plan.input_window_id is not None:
                window = self.get_dreaming_window(scope, plan.input_window_id)
                if window is None:
                    raise FoundationError("memory_dreaming_window_not_found", "Dreaming window does not exist")
                window_revisions = {
                    (ref.resource_id, int(ref.revision))
                    for ref in window.source_refs
                    if ref.revision is not None
                }
                plan_revisions = set()
                for action in plan.actions:
                    if action.memory_id is not None:
                        plan_revisions.add((action.memory_id, action.expected_revision))
                    plan_revisions.update(action.expected_revisions.items())
                if plan.kind != "dreaming_v1" or plan.basis_watermark != window.basis_watermark:
                    raise FoundationError("memory_dreaming_plan_invalid", "Dreaming plan does not match window")
                if not plan_revisions.issubset(window_revisions):
                    raise FoundationError("memory_dreaming_plan_invalid", "Dreaming actions are outside window")
            if self.authority_watermark(scope) != plan.basis_watermark:
                raise FoundationError("memory_maintenance_stale", "Memory changed after planning")
            if plan.input_window_id is not None:
                window_entries = {
                    ref.resource_id: self._expect_current(
                        scope, ref.resource_id, int(ref.revision), required_status="active"
                    )
                    for ref in window.source_refs if ref.revision is not None
                }
            current = dict(window_entries)
            for action in plan.actions:
                if action.memory_id is not None:
                    current[action.memory_id] = self._expect_current(
                        scope, action.memory_id, action.expected_revision, required_status="active"
                    )
                for memory_id, revision in action.expected_revisions.items():
                    current[memory_id] = self._expect_current(
                        scope, memory_id, revision, required_status="active"
                    )
            changed = []
            resource_ids = []
            action_receipts = []
            instant = datetime.now(timezone.utc)
            for action in plan.actions:
                action_operation_id = f"{operation_id}:{action.action_id}"
                if action.action == "link":
                    action_receipts.append(MemoryMaintenanceActionReceipt(
                        action_id=action.action_id,
                        operation_id=action_operation_id,
                        status="graph_pending",
                        resource_refs=(current[action.memory_id].ref(), action.link_target),
                        detail=action.link_relation,
                    ))
                    continue
                if action.action == "create":
                    if self.get_current(scope, action.new_memory_id) is not None:
                        raise FoundationError(
                            "memory_maintenance_conflict", "Created Memory identity already exists"
                        )
                    sources = tuple(sorted(
                        {ref for entry in window_entries.values() for ref in entry.source_refs},
                        key=lambda ref: (
                            ref.domain, ref.resource_type, ref.resource_id, ref.revision or ""
                        ),
                    ))
                    created = MemoryEntry(
                        memory_id=action.new_memory_id,
                        scope=scope,
                        content=action.replacement_content,
                        status="active",
                        source_refs=sources,
                    )
                    self._insert(created)
                    changed.append(created)
                    resource_ids.append(created.memory_id)
                    action_receipts.append(MemoryMaintenanceActionReceipt(
                        action_id=action.action_id,
                        operation_id=action_operation_id,
                        status="applied",
                        resource_refs=(created.ref(),),
                    ))
                    continue
                if action.action == "merge":
                    sources_to_merge = tuple(current[item] for item in action.memory_ids)
                    source_refs = tuple(sorted(
                        {ref for entry in sources_to_merge for ref in entry.source_refs},
                        key=lambda ref: (
                            ref.domain, ref.resource_type, ref.resource_id, ref.revision or ""
                        ),
                    ))
                    successor = MemoryEntry(
                        memory_id=action.new_memory_id,
                        scope=scope,
                        content=action.replacement_content,
                        status="active",
                        source_refs=source_refs,
                    )
                    receipt_refs = []
                    for entry in sources_to_merge:
                        superseded = entry.model_copy(update={
                            "revision": entry.revision + 1,
                            "status": "superseded",
                            "successor_id": successor.memory_id,
                            "updated_at": instant,
                        })
                        self._insert(superseded)
                        resource_ids.append(entry.memory_id)
                        receipt_refs.append(superseded.ref())
                    self._insert(successor)
                    changed.append(successor)
                    resource_ids.append(successor.memory_id)
                    receipt_refs.append(successor.ref())
                    action_receipts.append(MemoryMaintenanceActionReceipt(
                        action_id=action.action_id,
                        operation_id=action_operation_id,
                        status="applied",
                        resource_refs=tuple(receipt_refs),
                    ))
                    continue
                entry = current[action.memory_id]
                if action.action in {"revoke", "archive"}:
                    updated = entry.model_copy(update={
                        "revision": entry.revision + 1,
                        "status": "revoked",
                        "updated_at": instant,
                    })
                    self._insert(updated)
                    changed.append(updated)
                    resource_ids.append(updated.memory_id)
                    action_receipts.append(MemoryMaintenanceActionReceipt(
                        action_id=action.action_id,
                        operation_id=action_operation_id,
                        status="applied",
                        resource_refs=(updated.ref(),),
                    ))
                    continue
                if action.action == "update":
                    updated = entry.model_copy(update={
                        "revision": entry.revision + 1,
                        "content": action.replacement_content,
                        "updated_at": instant,
                    })
                    self._insert(updated)
                    changed.append(updated)
                    resource_ids.append(updated.memory_id)
                    action_receipts.append(MemoryMaintenanceActionReceipt(
                        action_id=action.action_id,
                        operation_id=action_operation_id,
                        status="applied",
                        resource_refs=(updated.ref(),),
                    ))
                    continue
                successor = MemoryEntry(
                    scope=scope,
                    content=action.replacement_content,
                    status="active",
                    source_refs=entry.source_refs,
                )
                superseded = entry.model_copy(update={
                    "revision": entry.revision + 1,
                    "status": "superseded",
                    "successor_id": successor.memory_id,
                    "updated_at": instant,
                })
                self._insert(superseded)
                self._insert(successor)
                changed.append(successor)
                resource_ids.extend((entry.memory_id, successor.memory_id))
                action_receipts.append(MemoryMaintenanceActionReceipt(
                    action_id=action.action_id,
                    operation_id=action_operation_id,
                    status="applied",
                    resource_refs=(superseded.ref(), successor.ref()),
                ))
            result = MemoryChangeSet(
                entries=tuple(changed), action_receipts=tuple(action_receipts)
            )
            pending_graph = any(item.status == "graph_pending" for item in action_receipts)
            self._put_maintenance_plan(plan_record.model_copy(update={
                "revision": plan_record.revision + 1,
                "state": "graph_pending" if pending_graph else "applied",
                "action_receipts": tuple(action_receipts),
                "applied_operation_id": operation_id,
                "updated_at": instant,
            }))
            self._record(
                scope,
                operation_id,
                command_digest,
                "maintain_apply",
                "change_set",
                result,
                tuple(resource_ids),
            )
            return result

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> MemoryOperationReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT operation_id, command_digest, action, resource_ids, committed_at, authority_sequence "
                "FROM af_memory_operations WHERE scope_key=? AND operation_id=?",
                (scope_key(scope), operation_id),
            ).fetchone()
        if row is None:
            return None
        return MemoryOperationReceipt(
            operation_id=row["operation_id"],
            command_digest=row["command_digest"],
            action=row["action"],
            resource_ids=tuple(json.loads(row["resource_ids"])),
            committed_at=row["committed_at"],
            authority_watermark=row["authority_sequence"],
        )

    def record_maintenance_link(
        self,
        scope: ScopeRef,
        plan_id: str,
        action_id: str,
        *,
        status: str,
        proposal_ref,
    ) -> MemoryChangeSet:
        if status not in {"graph_pending", "applied"}:
            raise FoundationError(
                "memory_maintenance_link_state_invalid", "Graph link status is invalid"
            )
        with self._lock, self._connection:
            record = self.get_maintenance_plan(scope, plan_id)
            if record is None or record.applied_operation_id is None:
                raise FoundationError(
                    "memory_maintenance_plan_not_found", "Applied maintenance plan is unavailable"
                )
            receipts = []
            matched = False
            for receipt in record.action_receipts:
                if receipt.action_id != action_id:
                    receipts.append(receipt)
                    continue
                matched = True
                references = tuple(
                    ref for ref in receipt.resource_refs
                    if ref.resource_type != "graph_proposal"
                ) + (proposal_ref,)
                receipts.append(receipt.model_copy(update={
                    "status": status,
                    "resource_refs": references,
                }))
            if not matched:
                raise FoundationError(
                    "memory_maintenance_action_not_found", "Maintenance action does not exist"
                )
            next_state = (
                "graph_pending"
                if any(item.status == "graph_pending" for item in receipts)
                else "applied"
            )
            updated_record = record.model_copy(update={
                "revision": record.revision + 1,
                "state": next_state,
                "action_receipts": tuple(receipts),
                "updated_at": datetime.now(timezone.utc),
            })
            row = self._connection.execute(
                "SELECT result_payload FROM af_memory_operations "
                "WHERE scope_key=? AND operation_id=? AND result_kind='change_set'",
                (scope_key(scope), record.applied_operation_id),
            ).fetchone()
            if row is None:
                raise FoundationError(
                    "memory_maintenance_receipt_invalid", "Maintenance receipt is unavailable"
                )
            result = MemoryChangeSet.model_validate_json(row["result_payload"])
            updated_result = result.model_copy(update={
                "action_receipts": tuple(receipts)
            })
            self._put_maintenance_plan(updated_record)
            self._connection.execute(
                "UPDATE af_memory_operations SET result_payload=? "
                "WHERE scope_key=? AND operation_id=?",
                (
                    updated_result.model_dump_json(),
                    scope_key(scope),
                    record.applied_operation_id,
                ),
            )
            return updated_result

    def rollback_maintenance(
        self,
        scope: ScopeRef,
        applied_operation_id: str,
        *,
        operation_id: str,
        command_digest: str,
    ) -> MemoryChangeSet:
        with self._lock, self._connection:
            existing = self._existing(
                scope, operation_id, command_digest, "change_set", MemoryChangeSet
            )
            if existing is not None:
                return existing
            applied = self._connection.execute(
                "SELECT action, result_kind, result_payload, resource_ids FROM af_memory_operations "
                "WHERE scope_key=? AND operation_id=?",
                (scope_key(scope), applied_operation_id),
            ).fetchone()
            if applied is None or applied["action"] != "maintain_apply" or applied["result_kind"] != "change_set":
                raise FoundationError("memory_maintenance_receipt_invalid", "Applied maintenance receipt is unavailable")
            plan_row = self._connection.execute(
                "SELECT payload FROM af_memory_maintenance_plans WHERE scope_key=? "
                "AND json_extract(payload, '$.applied_operation_id')=?",
                (scope_key(scope), applied_operation_id),
            ).fetchone()
            if plan_row is None:
                raise FoundationError("memory_maintenance_plan_not_found", "Applied maintenance plan is unavailable")
            plan_record = MemoryMaintenancePlanRecord.model_validate_json(plan_row[0])
            if plan_record.state not in {"applied", "graph_pending"}:
                raise FoundationError("memory_maintenance_plan_state_invalid", "Maintenance plan is not applied")
            resource_ids = tuple(json.loads(applied["resource_ids"]))
            current = {memory_id: self.get_current(scope, memory_id) for memory_id in resource_ids}
            changes: list[MemoryEntry] = []
            instant = datetime.now(timezone.utc)
            for action in reversed(plan_record.plan.actions):
                if action.action == "link":
                    continue
                if action.action == "create":
                    latest = current.get(action.new_memory_id)
                    if latest is None or latest.revision != 1 or latest.status != "active":
                        raise FoundationError(
                            "memory_maintenance_rollback_stale",
                            "Created Memory changed after maintenance",
                        )
                    changes.append(latest.model_copy(update={
                        "revision": latest.revision + 1,
                        "status": "revoked",
                        "updated_at": instant,
                    }))
                    continue
                if action.action == "update":
                    latest = current.get(action.memory_id)
                    if (
                        latest is None
                        or latest.revision != action.expected_revision + 1
                        or latest.status != "active"
                    ):
                        raise FoundationError(
                            "memory_maintenance_rollback_stale",
                            "Updated Memory changed after maintenance",
                        )
                    previous = self.get_revision(
                        scope, action.memory_id, action.expected_revision
                    )
                    if previous is None:
                        raise FoundationError(
                            "memory_maintenance_rollback_stale",
                            "Updated Memory predecessor is unavailable",
                        )
                    changes.append(latest.model_copy(update={
                        "revision": latest.revision + 1,
                        "content": previous.content,
                        "source_refs": previous.source_refs,
                        "updated_at": instant,
                    }))
                    continue
                if action.action in {"archive", "revoke"}:
                    latest = current.get(action.memory_id)
                    if (
                        latest is None
                        or latest.revision != action.expected_revision + 1
                        or latest.status != "revoked"
                    ):
                        raise FoundationError(
                            "memory_maintenance_rollback_stale",
                            "Archived Memory changed after maintenance",
                        )
                    changes.append(latest.model_copy(update={
                        "revision": latest.revision + 1,
                        "status": "active",
                        "updated_at": instant,
                    }))
                    continue
                predecessor_ids = (
                    action.memory_ids if action.action == "merge" else (action.memory_id,)
                )
                predecessors = tuple(current.get(memory_id) for memory_id in predecessor_ids)
                successor_id = action.new_memory_id if action.action == "merge" else (
                    predecessors[0].successor_id if predecessors[0] is not None else None
                )
                successor = current.get(successor_id)
                if (
                    successor is None
                    or successor.revision != 1
                    or successor.status != "active"
                    or any(
                        predecessor is None
                        or predecessor.status != "superseded"
                        or predecessor.successor_id != successor.memory_id
                        for predecessor in predecessors
                    )
                ):
                    raise FoundationError(
                        "memory_maintenance_rollback_stale",
                        "Merged Memory changed after maintenance",
                    )
                changes.extend(predecessor.model_copy(update={
                    "revision": predecessor.revision + 1,
                    "status": "active",
                    "successor_id": None,
                    "updated_at": instant,
                }) for predecessor in predecessors)
                changes.append(successor.model_copy(update={
                    "revision": successor.revision + 1,
                    "status": "revoked",
                    "updated_at": instant,
                }))
            for entry in changes:
                self._insert(entry)
            rollback = MemoryChangeSet(entries=tuple(changes))
            self._put_maintenance_plan(plan_record.model_copy(update={
                "revision": plan_record.revision + 1,
                "state": "rolled_back",
                "rollback_operation_id": operation_id,
                "updated_at": instant,
            }))
            self._record(
                scope,
                operation_id,
                command_digest,
                "maintain_rollback",
                "change_set",
                rollback,
                tuple(entry.memory_id for entry in changes),
            )
            return rollback

    def authority_watermark(self, scope: ScopeRef) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM af_memory_audit "
                "WHERE scope_key=? AND action NOT LIKE 'maintain_plan_%' "
                "AND action NOT LIKE 'maintain_window_%'",
                (scope_key(scope),),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._connection.close()

    def _put_maintenance_plan(self, record: MemoryMaintenancePlanRecord) -> None:
        self._connection.execute(
            "INSERT INTO af_memory_maintenance_plans(scope_key, plan_id, payload) VALUES(?,?,?) "
            "ON CONFLICT(scope_key, plan_id) DO UPDATE SET payload=excluded.payload",
            (
                scope_key(record.scope),
                record.plan.plan_id,
                record.model_dump_json(),
            ),
        )

    def _expect_current(
        self,
        scope: ScopeRef,
        memory_id: str,
        expected_revision: int,
        *,
        required_status: str | None = None,
    ) -> MemoryEntry:
        current = self.get_current(scope, memory_id)
        if current is None:
            raise FoundationError("memory_not_found", "Memory does not exist")
        if current.revision != expected_revision:
            raise FoundationError("memory_version_conflict", "Memory revision changed")
        if required_status is not None and current.status != required_status:
            raise FoundationError("memory_state_conflict", "Memory state changed")
        return current

    def _insert(self, entry: MemoryEntry) -> None:
        self._connection.execute(
            "INSERT INTO af_memory_revisions(scope_key, memory_id, revision, payload) VALUES (?, ?, ?, ?)",
            (scope_key(entry.scope), entry.memory_id, entry.revision, entry.model_dump_json()),
        )

    def _insert_feedback(self, event: MemoryFeedbackEvent) -> None:
        self._connection.execute(
            "INSERT INTO af_memory_feedback_events(scope_key, event_id, revision, memory_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                scope_key(event.scope),
                event.event_id,
                event.revision,
                event.memory_id,
                event.model_dump_json(),
            ),
        )

    def _existing(self, scope: ScopeRef, operation_id: str, digest: str, kind: str, model):
        row = self._connection.execute(
            "SELECT command_digest, result_kind, result_payload "
            "FROM af_memory_operations WHERE scope_key=? AND operation_id=?",
            (scope_key(scope), operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["command_digest"] != digest or row["result_kind"] != kind:
            raise FoundationError("operation_id_conflict", "Operation ID already has different command content")
        return model.model_validate_json(row["result_payload"])

    def _record(
        self,
        scope: ScopeRef,
        operation_id: str,
        command_digest: str,
        action: str,
        result_kind: str,
        result,
        resource_ids: tuple[str, ...],
    ) -> None:
        committed_at = datetime.now(timezone.utc).isoformat()
        resources = json.dumps(resource_ids, separators=(",", ":"))
        audit = self._connection.execute(
            "INSERT INTO af_memory_audit(scope_key, operation_id, action, resource_ids, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope_key(scope), operation_id, action, resources, committed_at),
        )
        self._connection.execute(
            "INSERT INTO af_memory_operations "
            "(operation_id, scope_key, command_digest, action, result_kind, result_payload, resource_ids, "
            "committed_at, authority_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                scope_key(scope),
                command_digest,
                action,
                result_kind,
                result.model_dump_json(),
                resources,
                committed_at,
                audit.lastrowid,
            ),
        )

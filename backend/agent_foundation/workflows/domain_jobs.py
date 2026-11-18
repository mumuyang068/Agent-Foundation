from __future__ import annotations

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.graph.contracts import GraphOperation
from agent_foundation.modules.jobs.contracts import JobStepResult


class GraphBulkProposalWorkflow:
    def __init__(self, graph) -> None:
        self._graph = graph

    def __call__(self, payload, cursor, fence):
        operations = tuple(GraphOperation.model_validate(item) for item in payload["operations"])
        evidence_refs = tuple(ResourceRef.model_validate(item) for item in payload["evidence_refs"])
        chunk_size = int(payload.get("chunk_size", 100))
        if not 1 <= chunk_size <= 500:
            raise FoundationError("graph_bulk_chunk_invalid", "Graph bulk chunk must be 1..500")
        start = int(cursor or "0")
        end = min(len(operations), start + chunk_size)
        if start >= end:
            raise FoundationError("graph_bulk_cursor_invalid", "Graph bulk cursor is outside operations")
        fence.check()
        proposal = self._graph.propose(
            fence.token,
            operations=operations[start:end],
            evidence_refs=evidence_refs,
            schema_id=payload.get("schema_id"),
            schema_version=payload.get("schema_version"),
            operation_id=f"bulk:{payload['bulk_id']}:{start}",
        )
        return JobStepResult(
            done=end == len(operations),
            cursor=str(end),
            result={
                "bulk_id": payload["bulk_id"],
                "proposed_operations": end,
                "last_proposal_id": proposal.proposal_id,
                "last_proposal_status": proposal.status,
            },
        )


class GraphApplyWorkflow:
    """Reconciles a prepared Graph apply through the domain's stable operation ID."""

    def __init__(self, graph) -> None:
        self._graph = graph

    def __call__(self, payload, cursor, fence):
        if cursor == "applied":
            return JobStepResult(
                done=True,
                cursor=cursor,
                result={"proposal_id": payload["proposal_id"], "status": "applied"},
            )
        operation_id = f"graph-apply-job:{payload['job_key']}"
        fence.check()
        proposal = self._graph.apply(
            fence.token,
            payload["proposal_id"],
            expected_revision=int(payload["expected_revision"]),
            operation_id=operation_id,
        )
        return JobStepResult(
            done=True,
            cursor="applied",
            result={
                "proposal_id": proposal.proposal_id,
                "proposal_revision": proposal.revision,
                "status": proposal.status,
                "operation_id": operation_id,
            },
        )


class MemoryMaintenanceWorkflow:
    def __init__(self, memory) -> None:
        self._memory = memory

    def __call__(self, payload, cursor, fence):
        if cursor == "applied":
            return JobStepResult(
                done=True,
                cursor=cursor,
                result={"plan_id": payload["plan_id"], "status": "applied"},
            )
        if cursor not in {None, "graph_pending"}:
            raise FoundationError(
                "memory_maintenance_cursor_invalid",
                "Memory maintenance cursor is invalid",
            )
        fence.check()
        changed = self._memory.apply_maintenance(
            fence.token,
            payload["plan_id"],
            operation_id=f"maintenance-job:{payload['job_key']}",
        )
        pending = tuple(
            receipt.action_id
            for receipt in changed.action_receipts
            if receipt.status == "graph_pending"
        )
        return JobStepResult(
            done=not pending,
            cursor="graph_pending" if pending else "applied",
            result={
                "plan_id": payload["plan_id"],
                "memory_refs": [entry.ref().model_dump(mode="json") for entry in changed.entries],
                "pending_graph_action_ids": list(pending),
                "status": "graph_pending" if pending else "applied",
            },
        )

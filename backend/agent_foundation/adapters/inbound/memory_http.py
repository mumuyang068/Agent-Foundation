from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Header
from pydantic import Field

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.memory.ports import MemoryService

from .access_http import RequestModel


class ProposeRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1)
    source_refs: tuple[ResourceRef, ...] = ()


class ReviewRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    approve: bool


class CorrectRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1)
    source_refs: tuple[ResourceRef, ...] | None = None


class FeedbackRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    value: float = Field(ge=-1, le=1)


class FeedbackTransitionRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    status: Literal["active", "contested", "retracted"]
    reason: str = Field(min_length=1, max_length=2_000)


class RevokeRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)


class RecallRequest(RequestModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1)


class CapacityPlanRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    max_active: int = Field(ge=1, le=10_000)


class RewritePlanRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    kind: Literal["reflective_rewrite", "normalization", "revision_set"]
    replacements: dict[str, str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class DreamingWindowRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    memory_ids: tuple[str, ...] = Field(min_length=2, max_length=8)


class DreamingPlanRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    window_id: str = Field(min_length=1, max_length=240)
    replacements: dict[str, str] | None = None
    actions: tuple[dict, ...] | None = None
    rationale: str = Field(min_length=1, max_length=2_000)
    planner_id: str | None = Field(default=None, max_length=240)
    planner_output: dict | None = None
    draft: dict | None = None
    proposal_only: bool = False


class ApplyMaintenanceRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    plan_id: str = Field(min_length=1, max_length=240)


class RollbackMaintenanceRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    applied_operation_id: str = Field(min_length=1, max_length=240)


def bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise FoundationError("session_inactive", "A Bearer credential is required")
    return authorization.removeprefix("Bearer ")


def add_memory_routes(app: FastAPI, memory: MemoryService) -> None:
    @app.post("/api/foundation/v1/memory/entries")
    def propose(payload: ProposeRequest, authorization: str | None = Header(default=None)):
        return memory.propose(
            bearer(authorization),
            content=payload.content,
            source_refs=payload.source_refs,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/memory/entries/{memory_id}")
    def get(memory_id: str, authorization: str | None = Header(default=None)):
        return memory.get(bearer(authorization), memory_id)

    @app.get("/api/foundation/v1/memory/entries")
    def list_entries(
        states: str = "active",
        limit: int = 8,
        authorization: str | None = Header(default=None),
    ):
        return memory.list(
            bearer(authorization),
            states=tuple(item.strip() for item in states.split(",") if item.strip()),
            limit=limit,
        )

    @app.get("/api/foundation/v1/memory/entries/{memory_id}/revisions/{revision}")
    def get_revision(memory_id: str, revision: int, authorization: str | None = Header(default=None)):
        return memory.get_revision(bearer(authorization), memory_id, revision)

    @app.get("/api/foundation/v1/memory/entries/{memory_id}/trace")
    def trace(memory_id: str, authorization: str | None = Header(default=None)):
        return memory.trace(bearer(authorization), memory_id)

    @app.get("/api/foundation/v1/memory/entries/{memory_id}/activation")
    def activation(memory_id: str, authorization: str | None = Header(default=None)):
        return memory.activation(bearer(authorization), memory_id)

    @app.get("/api/foundation/v1/memory/formation/status")
    def formation_status(authorization: str | None = Header(default=None)):
        return memory.formation_status(bearer(authorization))

    @app.get("/api/foundation/v1/memory/formation/queue")
    def formation_queue(limit: int = 8, authorization: str | None = Header(default=None)):
        return memory.list(bearer(authorization), states=("candidate",), limit=limit)

    @app.post("/api/foundation/v1/memory/entries/{memory_id}/review")
    def review(memory_id: str, payload: ReviewRequest, authorization: str | None = Header(default=None)):
        return memory.review(
            bearer(authorization),
            memory_id,
            approve=payload.approve,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/entries/{memory_id}/correct")
    def correct(memory_id: str, payload: CorrectRequest, authorization: str | None = Header(default=None)):
        return memory.correct(
            bearer(authorization),
            memory_id,
            content=payload.content,
            source_refs=payload.source_refs,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/entries/{memory_id}/feedback")
    def feedback(memory_id: str, payload: FeedbackRequest, authorization: str | None = Header(default=None)):
        return memory.feedback(
            bearer(authorization),
            memory_id,
            value=payload.value,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/memory/feedback-events")
    def list_feedback(
        memory_id: str | None = None,
        limit: int = 8,
        authorization: str | None = Header(default=None),
    ):
        return memory.list_feedback(bearer(authorization), memory_id=memory_id, limit=limit)

    @app.post("/api/foundation/v1/memory/feedback-events/{event_id}/transition")
    def transition_feedback(
        event_id: str,
        payload: FeedbackTransitionRequest,
        authorization: str | None = Header(default=None),
    ):
        return memory.transition_feedback(
            bearer(authorization),
            event_id,
            status=payload.status,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/memory/entries/{memory_id}/reinforcement-diagnostic")
    def reinforcement_diagnostic(
        memory_id: str, authorization: str | None = Header(default=None)
    ):
        return memory.reinforcement_diagnostic(bearer(authorization), memory_id)

    @app.post("/api/foundation/v1/memory/entries/{memory_id}/revoke")
    def revoke(memory_id: str, payload: RevokeRequest, authorization: str | None = Header(default=None)):
        return memory.revoke(
            bearer(authorization),
            memory_id,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/entries/{memory_id}/restore")
    def restore(memory_id: str, payload: RevokeRequest, authorization: str | None = Header(default=None)):
        return memory.restore(
            bearer(authorization),
            memory_id,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/recall")
    def recall(payload: RecallRequest, authorization: str | None = Header(default=None)):
        return memory.recall(bearer(authorization), query=payload.query, limit=payload.limit)

    @app.post("/api/foundation/v1/memory/maintenance/capacity-plan")
    def capacity_plan(payload: CapacityPlanRequest, authorization: str | None = Header(default=None)):
        return memory.plan_capacity_maintenance(
            bearer(authorization),
            max_active=payload.max_active,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/maintenance/rewrite-plan")
    def rewrite_plan(payload: RewritePlanRequest, authorization: str | None = Header(default=None)):
        return memory.plan_rewrite_maintenance(
            bearer(authorization),
            kind=payload.kind,
            replacements=payload.replacements,
            rationale=payload.rationale,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/memory/maintenance/dreaming-windows")
    def freeze_dreaming_window(
        payload: DreamingWindowRequest, authorization: str | None = Header(default=None)
    ):
        return memory.freeze_dreaming_window(
            bearer(authorization),
            memory_ids=payload.memory_ids,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/memory/maintenance/dreaming-windows/{window_id}")
    def get_dreaming_window(window_id: str, authorization: str | None = Header(default=None)):
        return memory.get_dreaming_window(bearer(authorization), window_id)

    @app.get("/api/foundation/v1/memory/maintenance/dreaming-windows")
    def list_dreaming_windows(
        limit: int = 8, authorization: str | None = Header(default=None)
    ):
        return memory.list_dreaming_windows(bearer(authorization), limit=limit)

    @app.post("/api/foundation/v1/memory/maintenance/dreaming-plan")
    def dreaming_plan(
        payload: DreamingPlanRequest, authorization: str | None = Header(default=None)
    ):
        return memory.plan_dreaming_maintenance(
            bearer(authorization),
            window_id=payload.window_id,
            replacements=payload.replacements,
            actions=payload.actions,
            rationale=payload.rationale,
            operation_id=payload.operation_id,
            planner_id=payload.planner_id,
            planner_output=payload.planner_output,
            draft=payload.draft,
            proposal_only=payload.proposal_only,
        )

    @app.get("/api/foundation/v1/memory/maintenance/plans/{plan_id}")
    def get_maintenance_plan(plan_id: str, authorization: str | None = Header(default=None)):
        return memory.get_maintenance_plan(bearer(authorization), plan_id)

    @app.get("/api/foundation/v1/memory/maintenance/plans")
    def list_maintenance_plans(
        limit: int = 8, authorization: str | None = Header(default=None)
    ):
        return memory.list_maintenance_plans(bearer(authorization), limit=limit)

    @app.post("/api/foundation/v1/memory/maintenance/apply")
    def apply_maintenance(
        payload: ApplyMaintenanceRequest, authorization: str | None = Header(default=None)
    ):
        return memory.apply_maintenance(
            bearer(authorization), payload.plan_id, operation_id=payload.operation_id
        )

    @app.post("/api/foundation/v1/memory/maintenance/rollback")
    def rollback_maintenance(
        payload: RollbackMaintenanceRequest, authorization: str | None = Header(default=None)
    ):
        return memory.rollback_maintenance(
            bearer(authorization),
            payload.applied_operation_id,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/memory/operations/{operation_id}")
    def get_operation_receipt(operation_id: str, authorization: str | None = Header(default=None)):
        return memory.get_operation_receipt(bearer(authorization), operation_id)

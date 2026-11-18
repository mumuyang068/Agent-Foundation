from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import JobPage, JobPolicy, ManagedJob
from .ports import JobAuthority, JobHandler


class _ActiveJobFence:
    def __init__(self, application: "JobsApplication", token: str, job: ManagedJob) -> None:
        self._application = application
        self._token = token
        self._scope = job.scope
        self._job_id = job.job_id
        self._lease_token = job.lease_token or ""

    @property
    def token(self) -> str:
        return self._token

    def check(self) -> None:
        self._application._authorize(self._token, "execute")
        self._application._authority.assert_lease(
            self._scope,
            self._job_id,
            lease_token=self._lease_token,
            now=self._application._now(),
        )


class JobsApplication:
    def __init__(
        self,
        authority: JobAuthority,
        access: AccessAuthorization,
        policy: JobPolicy,
        handlers: dict[str, JobHandler] | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority = authority
        self._access = access
        self._policy = policy
        self._handlers = dict(handlers or {})
        self._now = now or (lambda: datetime.now(timezone.utc))

    def plan(
        self, token: str, *, kind: str, payload: dict, operation_id: str
    ) -> ManagedJob:
        operation, _grant, _trusted = self._authorize(token, "plan")
        digest = stable_fingerprint({
            "action": "plan", "scope": operation.scope.model_dump(mode="json"),
            "kind": kind, "payload": payload,
        })
        job = ManagedJob(
            scope=operation.scope,
            planned_by=operation.principal.principal_id,
            kind=kind,
            payload=payload,
        )
        return self._authority.plan(job, operation_id=operation_id, command_digest=digest)

    def authorize_job(
        self, token: str, job_id: str, *, expected_revision: int, operation_id: str
    ) -> ManagedJob:
        operation, _grant, _trusted = self._authorize(token, "authorize")
        digest = stable_fingerprint({
            "action": "authorize", "job_id": job_id, "expected_revision": expected_revision,
        })
        return self._authority.authorize(
            operation.scope, job_id, expected_revision=expected_revision,
            operation_id=operation_id, command_digest=digest,
        )

    def run_next(
        self,
        token: str,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> ManagedJob:
        operation, _grant, _trusted = self._authorize(token, "execute")
        if not worker_id or not 1 <= lease_seconds <= self._policy.max_lease_seconds:
            raise FoundationError("job_lease_invalid", "Job lease configuration is invalid")
        try:
            handler = self._handlers[self._required(operation.scope, job_id).kind]
        except KeyError as error:
            raise FoundationError("job_handler_unavailable", "Job handler is not configured", retryable=True) from error
        claimed = self._authority.claim(
            operation.scope,
            job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=self._now(),
            max_attempts=self._policy.max_attempts,
        )
        fence = _ActiveJobFence(self, token, claimed)
        try:
            fence.check()
            step = handler(claimed.payload, claimed.cursor, fence)
            fence.check()
            return self._authority.finish_step(
                operation.scope,
                job_id,
                lease_token=claimed.lease_token or "",
                step=step,
                now=self._now(),
            )
        except FoundationError as error:
            current = self._authority.get(operation.scope, job_id)
            if current is not None and current.status == "cancelled":
                return current
            try:
                return self._authority.fail_step(
                    operation.scope,
                    job_id,
                    lease_token=claimed.lease_token or "",
                    error_code=error.code,
                    retryable=error.retryable,
                    now=self._now(),
                )
            except FoundationError as lease_error:
                if lease_error.code == "job_lease_lost":
                    return self._required(operation.scope, job_id)
                raise
        except Exception:
            return self._authority.fail_step(
                operation.scope,
                job_id,
                lease_token=claimed.lease_token or "",
                error_code="job_handler_failed",
                retryable=False,
                now=self._now(),
            )

    def cancel(self, token: str, job_id: str, *, operation_id: str) -> ManagedJob:
        operation, _grant, _trusted = self._authorize(token, "cancel")
        digest = stable_fingerprint({"action": "cancel", "job_id": job_id})
        return self._authority.cancel(
            operation.scope,
            job_id,
            operation_id=operation_id,
            command_digest=digest,
            now=self._now(),
        )

    def get(self, token: str, job_id: str) -> ManagedJob:
        operation, _grant, _trusted = self._authorize(token, "read")
        return self._required(operation.scope, job_id)

    def list(self, token: str, *, limit: int = 20) -> JobPage:
        operation, grant, _trusted = self._authorize(token, "read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("job_budget_exceeded", "Job list exceeds budget")
        return JobPage(items=self._authority.list(operation.scope, limit=limit))

    def get_operation_receipt(self, token: str, operation_id: str):
        operation, _grant, _trusted = self._authorize(token, "read")
        receipt = self._authority.get_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("job_receipt_not_found", "Job operation receipt does not exist")
        return receipt

    def list_outbox(self, token: str, *, limit: int = 100):
        operation, grant, _trusted = self._authorize(token, "read")
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("job_budget_exceeded", "Outbox list exceeds budget")
        return self._authority.list_outbox(operation.scope, limit=limit)

    def acknowledge_outbox(self, token: str, event_id: str):
        operation, _grant, _trusted = self._authorize(token, "outbox_ack")
        return self._authority.acknowledge_outbox(operation.scope, event_id)

    def close(self) -> None:
        self._authority.close()

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token,
            domain="jobs",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
        )

    def _required(self, scope, job_id):
        job = self._authority.get(scope, job_id)
        if job is None:
            raise FoundationError("job_not_found", "Job does not exist")
        return job

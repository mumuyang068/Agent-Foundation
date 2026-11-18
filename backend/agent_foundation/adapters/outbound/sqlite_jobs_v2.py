from __future__ import annotations

from datetime import datetime
import sqlite3
import threading
from uuid import uuid4

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.jobs.contracts import (
    JobOperationReceipt,
    JobOutboxEvent,
    JobStepResult,
    ManagedJob,
)


def _scope_key(scope: ScopeRef) -> str:
    return scope.model_dump_json()


class SqliteJobAuthority:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS af_jobs (
                    scope_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, job_id)
                );
                CREATE TABLE IF NOT EXISTS af_job_operations (
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, operation_id)
                );
                CREATE TABLE IF NOT EXISTS af_job_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )

    def plan(self, job, *, operation_id: str, command_digest: str):
        key = _scope_key(job.scope)
        with self._transaction():
            replay = self._receipt_row(key, operation_id, command_digest)
            if replay is not None:
                return self._required_locked(job.scope, replay["job_id"])
            now = job.created_at
            self._connection.execute(
                "INSERT INTO af_jobs VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
                (key, job.job_id, job.revision, job.status, job.model_dump_json(), now.isoformat()),
            )
            self._insert_receipt(key, operation_id, command_digest, "plan", job.job_id, now)
            self._insert_event(key, job, "planned", now)
        return job

    def get(self, scope: ScopeRef, job_id: str):
        row = self._connection.execute(
            "SELECT payload_json FROM af_jobs WHERE scope_key=? AND job_id=?",
            (_scope_key(scope), job_id),
        ).fetchone()
        return None if row is None else ManagedJob.model_validate_json(row["payload_json"])

    def list(self, scope: ScopeRef, *, limit: int):
        rows = self._connection.execute(
            "SELECT payload_json FROM af_jobs WHERE scope_key=? ORDER BY updated_at DESC, job_id LIMIT ?",
            (_scope_key(scope), limit),
        ).fetchall()
        return tuple(ManagedJob.model_validate_json(row["payload_json"]) for row in rows)

    def authorize(self, scope, job_id, *, expected_revision, operation_id, command_digest):
        key = _scope_key(scope)
        with self._transaction():
            replay = self._receipt_row(key, operation_id, command_digest)
            if replay is not None:
                return self._required_locked(scope, replay["job_id"])
            job = self._required_locked(scope, job_id)
            if job.status != "planned" or job.revision != expected_revision:
                raise FoundationError("job_revision_conflict", "Job is not at the planned revision")
            updated = self._next(job, status="authorized")
            self._store_locked(updated, expected_revision=expected_revision)
            self._insert_receipt(key, operation_id, command_digest, "authorize", job_id, updated.updated_at)
            self._insert_event(key, updated, "authorized", updated.updated_at)
        return updated

    def claim(self, scope, job_id, *, worker_id, lease_seconds, now, max_attempts):
        with self._transaction():
            job = self._required_locked(scope, job_id)
            active_lease = job.lease_expires_at is not None and job.lease_expires_at > now
            if job.status == "running" and active_lease:
                raise FoundationError("job_leased", "Job already has an active lease", retryable=True)
            if job.status not in {"authorized", "running", "failed"}:
                raise FoundationError("job_not_runnable", "Job is not runnable")
            if job.status == "failed" and not job.retryable:
                raise FoundationError("job_not_runnable", "Job failure is final")
            if job.attempts >= max_attempts:
                raise FoundationError("job_attempts_exhausted", "Job attempts are exhausted")
            token = str(uuid4())
            updated = self._next(
                job,
                status="running",
                attempts=job.attempts + 1,
                lease_owner=worker_id,
                lease_token=token,
                lease_expires_at=datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=now.tzinfo),
                error_code=None,
            )
            self._store_locked(updated, expected_revision=job.revision)
            self._insert_event(_scope_key(scope), updated, "claimed", now)
        return updated

    def assert_lease(self, scope, job_id, *, lease_token, now):
        job = self.get(scope, job_id)
        if (
            job is None or job.status != "running" or job.lease_token != lease_token
            or job.lease_expires_at is None or job.lease_expires_at <= now
        ):
            raise FoundationError("job_lease_lost", "Job lease is no longer active")
        return job

    def finish_step(self, scope, job_id, *, lease_token, step: JobStepResult, now):
        with self._transaction():
            job = self._required_locked(scope, job_id)
            self._assert_locked_lease(job, lease_token, now)
            updated = self._next(
                job,
                status="succeeded" if step.done else "authorized",
                cursor=step.cursor,
                result=step.result,
                retryable=False,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
            self._store_locked(updated, expected_revision=job.revision)
            self._insert_event(_scope_key(scope), updated, "completed" if step.done else "advanced", now)
        return updated

    def fail_step(self, scope, job_id, *, lease_token, error_code, retryable, now):
        with self._transaction():
            job = self._required_locked(scope, job_id)
            self._assert_locked_lease(job, lease_token, now)
            updated = self._next(
                job,
                status="failed",
                retryable=retryable,
                error_code=error_code,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
            self._store_locked(updated, expected_revision=job.revision)
            self._insert_event(_scope_key(scope), updated, "failed", now)
        return updated

    def cancel(self, scope, job_id, *, operation_id, command_digest, now):
        key = _scope_key(scope)
        with self._transaction():
            replay = self._receipt_row(key, operation_id, command_digest)
            if replay is not None:
                return self._required_locked(scope, replay["job_id"])
            job = self._required_locked(scope, job_id)
            if job.status in {"succeeded", "cancelled"}:
                raise FoundationError("job_transition_invalid", "Completed Job cannot be cancelled")
            updated = self._next(
                job, status="cancelled", lease_owner=None, lease_token=None, lease_expires_at=None
            )
            self._store_locked(updated, expected_revision=job.revision)
            self._insert_receipt(key, operation_id, command_digest, "cancel", job_id, now)
            self._insert_event(key, updated, "cancelled", now)
        return updated

    def get_receipt(self, scope, operation_id):
        row = self._connection.execute(
            "SELECT * FROM af_job_operations WHERE scope_key=? AND operation_id=?",
            (_scope_key(scope), operation_id),
        ).fetchone()
        return None if row is None else self._receipt(row)

    def list_outbox(self, scope, *, limit):
        rows = self._connection.execute(
            "SELECT * FROM af_job_outbox WHERE scope_key=? AND acknowledged=0 ORDER BY sequence LIMIT ?",
            (_scope_key(scope), limit),
        ).fetchall()
        return tuple(self._event(row) for row in rows)

    def acknowledge_outbox(self, scope, event_id):
        key = _scope_key(scope)
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM af_job_outbox WHERE scope_key=? AND event_id=?", (key, event_id)
            ).fetchone()
            if row is None:
                raise FoundationError("job_outbox_not_found", "Job outbox event does not exist")
            self._connection.execute(
                "UPDATE af_job_outbox SET acknowledged=1 WHERE scope_key=? AND event_id=?",
                (key, event_id),
            )
        return self._event(row).model_copy(update={"acknowledged": True})

    def close(self) -> None:
        self._connection.close()

    def _required_locked(self, scope, job_id):
        job = self.get(scope, job_id)
        if job is None:
            raise FoundationError("job_not_found", "Job does not exist")
        return job

    def _store_locked(self, job: ManagedJob, *, expected_revision: int):
        changed = self._connection.execute(
            """
            UPDATE af_jobs SET revision=?, status=?, payload_json=?, lease_token=?,
                lease_expires_at=?, updated_at=?
            WHERE scope_key=? AND job_id=? AND revision=?
            """,
            (
                job.revision, job.status, job.model_dump_json(), job.lease_token,
                job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                job.updated_at.isoformat(), _scope_key(job.scope), job.job_id, expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise FoundationError("job_revision_conflict", "Job revision changed")

    @staticmethod
    def _next(job: ManagedJob, **changes):
        return job.model_copy(update={
            **changes,
            "revision": job.revision + 1,
            "updated_at": changes.get("updated_at", datetime.now(job.updated_at.tzinfo)),
        })

    @staticmethod
    def _assert_locked_lease(job, lease_token, now):
        if (
            job.status != "running" or job.lease_token != lease_token
            or job.lease_expires_at is None or job.lease_expires_at <= now
        ):
            raise FoundationError("job_lease_lost", "Job lease is no longer active")

    def _receipt_row(self, key, operation_id, digest):
        row = self._connection.execute(
            "SELECT * FROM af_job_operations WHERE scope_key=? AND operation_id=?",
            (key, operation_id),
        ).fetchone()
        if row is not None and row["command_digest"] != digest:
            raise FoundationError("operation_id_conflict", "Operation ID has different Job content")
        return row

    def _insert_receipt(self, key, operation_id, digest, action, job_id, now):
        self._connection.execute(
            "INSERT INTO af_job_operations VALUES (?, ?, ?, ?, ?, ?)",
            (key, operation_id, digest, action, job_id, now.isoformat()),
        )

    def _insert_event(self, key, job, kind, now):
        event_id = f"{job.job_id}:{job.revision}:{kind}"
        self._connection.execute(
            "INSERT INTO af_job_outbox(scope_key,event_id,job_id,kind,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (key, event_id, job.job_id, f"job.{kind}", job.model_dump_json(), now.isoformat()),
        )

    @staticmethod
    def _receipt(row):
        return JobOperationReceipt(
            operation_id=row["operation_id"], command_digest=row["command_digest"],
            action=row["action"], job_id=row["job_id"],
            committed_at=datetime.fromisoformat(row["committed_at"]),
        )

    @staticmethod
    def _event(row):
        return JobOutboxEvent(
            sequence=row["sequence"], event_id=row["event_id"], job_id=row["job_id"],
            kind=row["kind"], payload=ManagedJob.model_validate_json(row["payload_json"]).model_dump(mode="json"),
            acknowledged=bool(row["acknowledged"]), created_at=datetime.fromisoformat(row["created_at"]),
        )

    class _Transaction:
        def __init__(self, outer):
            self.outer = outer
        def __enter__(self):
            self.outer._lock.acquire()
            self.outer._connection.execute("BEGIN IMMEDIATE")
        def __exit__(self, kind, value, traceback):
            try:
                self.outer._connection.execute("COMMIT" if kind is None else "ROLLBACK")
            finally:
                self.outer._lock.release()

    def _transaction(self):
        return self._Transaction(self)

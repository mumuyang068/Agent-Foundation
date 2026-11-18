from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.management.contracts import (
    EvaluationReport,
    ExtractorCertificate,
    ExtractorCertificateRevocation,
    ExtractorExecutionReceipt,
    IndexGeneration,
    ManagedRelease,
    ManagementOperationReceipt,
    TransferPlan,
)


def _scope(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _payload(value) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class SqliteManagementAuthority:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            self._connection.executescript("""
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS af_management_releases (
                    scope_key TEXT NOT NULL, release_id TEXT NOT NULL, kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(scope_key, release_id),
                    UNIQUE(scope_key, kind, artifact_id, release_id)
                );
                CREATE INDEX IF NOT EXISTS af_management_release_active
                    ON af_management_releases(scope_key, kind, artifact_id, status);
                CREATE TABLE IF NOT EXISTS af_management_indexes (
                    scope_key TEXT NOT NULL, generation_id TEXT NOT NULL, domain TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, generation_id)
                );
                CREATE INDEX IF NOT EXISTS af_management_index_active
                    ON af_management_indexes(scope_key, domain, status);
                CREATE TABLE IF NOT EXISTS af_management_evaluations (
                    scope_key TEXT NOT NULL, evaluation_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, evaluation_id)
                );
                CREATE TABLE IF NOT EXISTS af_management_transfers (
                    scope_key TEXT NOT NULL, transfer_id TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, transfer_id)
                );
                CREATE TABLE IF NOT EXISTS af_management_operations (
                    scope_key TEXT NOT NULL, operation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL, action TEXT NOT NULL,
                    resource_id TEXT NOT NULL, resource_revision INTEGER NOT NULL,
                    committed_at TEXT NOT NULL, PRIMARY KEY(scope_key, operation_id)
                );
                CREATE TABLE IF NOT EXISTS af_extractor_certificates (
                    scope_key TEXT NOT NULL, certificate_id TEXT NOT NULL,
                    release_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, certificate_id)
                );
                CREATE INDEX IF NOT EXISTS af_extractor_certificate_release
                    ON af_extractor_certificates(scope_key, release_id);
                CREATE TABLE IF NOT EXISTS af_extractor_revocations (
                    scope_key TEXT NOT NULL, revocation_id TEXT NOT NULL,
                    certificate_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, revocation_id), UNIQUE(scope_key, certificate_id)
                );
                CREATE TABLE IF NOT EXISTS af_extractor_executions (
                    scope_key TEXT NOT NULL, event_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, event_id)
                );
            """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def stage_release(self, release, *, operation_id, command_digest):
        replay = self._replay(release.scope, operation_id, command_digest)
        if replay:
            return self.get_release(release.scope, replay.resource_id)
        with self._transaction():
            replay = self._replay_locked(release.scope, operation_id, command_digest)
            if replay:
                return self._release_locked(release.scope, replay.resource_id)
            existing = self._connection.execute(
                "SELECT payload FROM af_management_releases WHERE scope_key=? AND kind=? AND artifact_id=?",
                (_scope(release.scope), release.kind, release.artifact_id),
            ).fetchall()
            for row in existing:
                current = ManagedRelease.model_validate_json(row["payload"])
                if current.version == release.version and current.content_digest != release.content_digest:
                    raise FoundationError("management_release_conflict", "Release identity has different content")
            self._connection.execute(
                "INSERT INTO af_management_releases VALUES(?,?,?,?,?,?,?)",
                (_scope(release.scope), release.release_id, release.kind, release.artifact_id,
                 release.status, release.revision, _payload(release)),
            )
            self._receipt_locked(release.scope, operation_id, command_digest, "release_stage", release.release_id, 1)
        return release

    def save_extractor_certificate(self, certificate, *, operation_id, command_digest):
        replay = self._replay(certificate.scope, operation_id, command_digest)
        if replay:
            stored = self.get_extractor_certificate(certificate.scope, replay.resource_id)
            if stored is None:
                raise FoundationError("extractor_certificate_corrupt", "Extractor certificate is missing")
            return stored
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM af_extractor_revocations r JOIN af_extractor_certificates c "
                "ON c.scope_key=r.scope_key AND c.certificate_id=r.certificate_id "
                "WHERE c.scope_key=? AND c.release_id=?",
                (_scope(certificate.scope), certificate.release_id),
            ).fetchone():
                raise FoundationError(
                    "extractor_release_revoked", "A revoked extractor release needs a new publication"
                )
            self._connection.execute(
                "INSERT INTO af_extractor_certificates VALUES(?,?,?,?,?)",
                (_scope(certificate.scope), certificate.certificate_id, certificate.release_id,
                 certificate.fingerprint, _payload(certificate)),
            )
            self._receipt_locked(
                certificate.scope, operation_id, command_digest, "extractor_certify",
                certificate.certificate_id, 1,
            )
        return certificate

    def get_extractor_certificate(self, scope, certificate_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_extractor_certificates WHERE scope_key=? AND certificate_id=?",
                (_scope(scope), certificate_id),
            ).fetchone()
        return ExtractorCertificate.model_validate_json(row["payload"]) if row else None

    def get_release_certificate(self, scope, release_id):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM af_extractor_certificates WHERE scope_key=? AND release_id=? "
                "ORDER BY rowid DESC",
                (_scope(scope), release_id),
            ).fetchall()
        for row in rows:
            certificate = ExtractorCertificate.model_validate_json(row["payload"])
            if certificate.status == "conformant" and self.get_extractor_revocation(
                scope, certificate.certificate_id
            ) is None:
                return certificate
        return None

    def revoke_extractor_certificate(self, revocation, *, operation_id, command_digest):
        replay = self._replay(revocation.scope, operation_id, command_digest)
        if replay:
            stored = self.get_extractor_revocation(revocation.scope, revocation.certificate_id)
            if stored is None:
                raise FoundationError("extractor_revocation_corrupt", "Extractor revocation is missing")
            return stored
        with self._transaction():
            try:
                self._connection.execute(
                    "INSERT INTO af_extractor_revocations VALUES(?,?,?,?)",
                    (_scope(revocation.scope), revocation.revocation_id,
                     revocation.certificate_id, _payload(revocation)),
                )
            except sqlite3.IntegrityError as error:
                raise FoundationError(
                    "extractor_certificate_already_revoked", "Extractor certificate is already revoked"
                ) from error
            self._receipt_locked(
                revocation.scope, operation_id, command_digest, "extractor_revoke",
                revocation.revocation_id, 1,
            )
        return revocation

    def get_extractor_revocation(self, scope, certificate_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_extractor_revocations WHERE scope_key=? AND certificate_id=?",
                (_scope(scope), certificate_id),
            ).fetchone()
        return ExtractorCertificateRevocation.model_validate_json(row["payload"]) if row else None

    def save_extractor_execution(self, receipt, *, operation_id, command_digest):
        with self._transaction():
            row = self._connection.execute(
                "SELECT command_digest, payload FROM af_extractor_executions "
                "WHERE scope_key=? AND event_id=?",
                (_scope(receipt.scope), receipt.event_id),
            ).fetchone()
            if row is not None:
                if row["command_digest"] != command_digest:
                    raise FoundationError(
                        "operation_id_conflict", "Extractor event ID has different content"
                    )
                return ExtractorExecutionReceipt.model_validate_json(row["payload"])
            self._connection.execute(
                "INSERT INTO af_extractor_executions VALUES(?,?,?,?)",
                (_scope(receipt.scope), receipt.event_id, command_digest, _payload(receipt)),
            )
            self._receipt_locked(
                receipt.scope, operation_id, command_digest, "extractor_execute", receipt.event_id, 1
            )
        return receipt

    def get_extractor_execution(self, scope, event_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_extractor_executions WHERE scope_key=? AND event_id=?",
                (_scope(scope), event_id),
            ).fetchone()
        return ExtractorExecutionReceipt.model_validate_json(row["payload"]) if row else None

    def get_extractor_execution_digest(self, scope, event_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT command_digest FROM af_extractor_executions "
                "WHERE scope_key=? AND event_id=?",
                (_scope(scope), event_id),
            ).fetchone()
        return row["command_digest"] if row else None

    def get_release(self, scope, release_id):
        with self._lock:
            return self._release_locked(scope, release_id)

    def list_releases(self, scope, *, kind=None, status=None, limit=20):
        return self._list_payloads(
            "af_management_releases", scope,
            filters=(("kind", kind), ("status", status)),
            order_by="release_id", limit=limit, model=ManagedRelease,
        )

    def get_active_release(self, scope, kind, artifact_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_management_releases WHERE scope_key=? AND kind=? AND artifact_id=? AND status='active'",
                (_scope(scope), kind, artifact_id),
            ).fetchone()
        return ManagedRelease.model_validate_json(row["payload"]) if row else None

    def approve_release(self, scope, release_id, *, expected_revision, approved_by, operation_id, command_digest):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_release(scope, replay.resource_id)
        with self._transaction():
            current = self._required_release_locked(scope, release_id)
            if current.revision != expected_revision or current.status not in {"staged", "failed"}:
                raise FoundationError("management_release_stale", "Expected staged or failed release is required")
            updated = current.model_copy(update={
                "status": "approved", "revision": current.revision + 1,
                "approved_by": approved_by, "updated_at": datetime.now(timezone.utc),
            })
            self._update_release_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, "release_approve", release_id, updated.revision)
        return updated

    def activate_release(self, scope, release_id, *, expected_revision, activated_by, effect_receipt, operation_id, command_digest):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_release(scope, replay.resource_id)
        with self._transaction():
            current = self._required_release_locked(scope, release_id)
            if current.revision != expected_revision or current.status != "approved":
                raise FoundationError("management_release_stale", "Expected approved release is required")
            rows = self._connection.execute(
                "SELECT payload FROM af_management_releases WHERE scope_key=? AND kind=? AND artifact_id=? AND status='active'",
                (_scope(scope), current.kind, current.artifact_id),
            ).fetchall()
            for row in rows:
                old = ManagedRelease.model_validate_json(row["payload"])
                retired = old.model_copy(update={
                    "status": "retired", "revision": old.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                })
                self._update_release_locked(retired, old.revision)
            updated = current.model_copy(update={
                "status": "active", "revision": current.revision + 1,
                "activated_by": activated_by, "effect_receipt": effect_receipt,
                "error_code": None, "updated_at": datetime.now(timezone.utc),
            })
            self._update_release_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, "release_activate", release_id, updated.revision)
        return updated

    def fail_release(self, scope, release_id, *, expected_revision, error_code):
        with self._transaction():
            current = self._required_release_locked(scope, release_id)
            if current.revision != expected_revision:
                raise FoundationError("management_release_stale", "Release revision changed")
            updated = current.model_copy(update={
                "status": "failed", "revision": current.revision + 1,
                "error_code": error_code, "updated_at": datetime.now(timezone.utc),
            })
            self._update_release_locked(updated, expected_revision)
        return updated

    def begin_index(self, generation, *, operation_id, command_digest):
        replay = self._replay(generation.scope, operation_id, command_digest)
        if replay:
            return self.get_index(generation.scope, replay.resource_id)
        with self._transaction():
            self._connection.execute(
                "INSERT INTO af_management_indexes VALUES(?,?,?,?,?,?)",
                (_scope(generation.scope), generation.generation_id, generation.domain,
                 generation.status, generation.revision, _payload(generation)),
            )
            self._receipt_locked(generation.scope, operation_id, command_digest, "index_begin", generation.generation_id, 1)
        return generation

    def get_index(self, scope, generation_id):
        with self._lock:
            return self._index_locked(scope, generation_id)

    def list_indexes(self, scope, *, domain=None, status=None, limit=20):
        return self._list_payloads(
            "af_management_indexes", scope,
            filters=(("domain", domain), ("status", status)),
            order_by="generation_id", limit=limit, model=IndexGeneration,
        )

    def get_active_index(self, scope, domain):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_management_indexes WHERE scope_key=? AND domain=? AND status='active'",
                (_scope(scope), domain),
            ).fetchone()
        return IndexGeneration.model_validate_json(row["payload"]) if row else None

    def verify_index(self, scope, generation_id, *, expected_revision, effect_receipt, operation_id, command_digest):
        return self._transition_index(
            scope, generation_id, expected_revision=expected_revision, expected_status="building",
            status="verified", effect_receipt=effect_receipt, actor=None,
            operation_id=operation_id, command_digest=command_digest, action="index_verify",
        )

    def activate_index(self, scope, generation_id, *, expected_revision, activated_by, effect_receipt, operation_id, command_digest):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_index(scope, replay.resource_id)
        with self._transaction():
            current = self._required_index_locked(scope, generation_id)
            if current.revision != expected_revision or current.status != "verified":
                raise FoundationError("index_generation_stale", "Expected verified generation is required")
            rows = self._connection.execute(
                "SELECT payload FROM af_management_indexes WHERE scope_key=? AND domain=? AND status='active'",
                (_scope(scope), current.domain),
            ).fetchall()
            for row in rows:
                old = IndexGeneration.model_validate_json(row["payload"])
                retired = old.model_copy(update={
                    "status": "retired", "revision": old.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                })
                self._update_index_locked(retired, old.revision)
            updated = current.model_copy(update={
                "status": "active", "revision": current.revision + 1,
                "activated_by": activated_by, "effect_receipt": effect_receipt,
                "error_code": None, "updated_at": datetime.now(timezone.utc),
            })
            self._update_index_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, "index_activate", generation_id, updated.revision)
        return updated

    def fail_index(self, scope, generation_id, *, expected_revision, error_code):
        with self._transaction():
            current = self._required_index_locked(scope, generation_id)
            if current.revision != expected_revision:
                raise FoundationError("index_generation_stale", "Index revision changed")
            updated = current.model_copy(update={
                "status": "failed", "revision": current.revision + 1,
                "error_code": error_code, "updated_at": datetime.now(timezone.utc),
            })
            self._update_index_locked(updated, expected_revision)
        return updated

    def record_evaluation(self, report, *, operation_id, command_digest):
        replay = self._replay(report.scope, operation_id, command_digest)
        if replay:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload FROM af_management_evaluations WHERE scope_key=? AND evaluation_id=?",
                    (_scope(report.scope), replay.resource_id),
                ).fetchone()
            return EvaluationReport.model_validate_json(row["payload"])
        with self._transaction():
            self._connection.execute(
                "INSERT INTO af_management_evaluations VALUES(?,?,?)",
                (_scope(report.scope), report.evaluation_id, _payload(report)),
            )
            self._receipt_locked(report.scope, operation_id, command_digest, "evaluate", report.evaluation_id, 1)
        return report

    def get_evaluation(self, scope, evaluation_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_management_evaluations WHERE scope_key=? AND evaluation_id=?",
                (_scope(scope), evaluation_id),
            ).fetchone()
        return EvaluationReport.model_validate_json(row["payload"]) if row else None

    def list_evaluations(self, scope, *, subject_ref=None, limit=20):
        query = "SELECT payload FROM af_management_evaluations WHERE scope_key=?"
        parameters: list[object] = [_scope(scope)]
        if subject_ref is not None:
            query += " AND json_extract(payload, '$.subject_ref')=?"
            parameters.append(subject_ref)
        query += " ORDER BY evaluation_id LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return tuple(EvaluationReport.model_validate_json(row["payload"]) for row in rows)

    def propose_transfer(self, plan, *, operation_id, command_digest):
        replay = self._replay(plan.scope, operation_id, command_digest)
        if replay:
            return self.get_transfer(plan.scope, replay.resource_id)
        with self._transaction():
            self._connection.execute(
                "INSERT INTO af_management_transfers VALUES(?,?,?,?,?)",
                (_scope(plan.scope), plan.transfer_id, plan.status, plan.revision, _payload(plan)),
            )
            self._receipt_locked(plan.scope, operation_id, command_digest, "transfer_plan", plan.transfer_id, 1)
        return plan

    def get_transfer(self, scope, transfer_id):
        with self._lock:
            return self._transfer_locked(scope, transfer_id)

    def list_transfers(self, scope, *, kind=None, status=None, limit=20):
        return self._list_payloads(
            "af_management_transfers", scope,
            filters=(("json_extract(payload, '$.kind')", kind), ("status", status)),
            order_by="transfer_id", limit=limit, model=TransferPlan,
        )

    def approve_transfer(self, scope, transfer_id, *, expected_revision, approved_by, operation_id, command_digest):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_transfer(scope, replay.resource_id)
        with self._transaction():
            current = self._required_transfer_locked(scope, transfer_id)
            if current.revision != expected_revision or current.status not in {"proposed", "failed"}:
                raise FoundationError("transfer_stale", "Expected proposed or failed transfer is required")
            updated = current.model_copy(update={
                "status": "approved", "revision": current.revision + 1,
                "approved_by": approved_by, "error_code": None,
                "updated_at": datetime.now(timezone.utc),
            })
            self._update_transfer_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, "transfer_approve", transfer_id, updated.revision)
        return updated

    def complete_transfer(self, scope, transfer_id, *, expected_revision, executed_by, effect_receipt, operation_id, command_digest):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_transfer(scope, replay.resource_id)
        with self._transaction():
            current = self._required_transfer_locked(scope, transfer_id)
            if current.revision != expected_revision or current.status != "approved":
                raise FoundationError("transfer_stale", "Expected approved transfer is required")
            updated = current.model_copy(update={
                "status": "completed", "revision": current.revision + 1,
                "executed_by": executed_by, "effect_receipt": effect_receipt,
                "updated_at": datetime.now(timezone.utc),
            })
            self._update_transfer_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, "transfer_execute", transfer_id, updated.revision)
        return updated

    def fail_transfer(self, scope, transfer_id, *, expected_revision, error_code):
        with self._transaction():
            current = self._required_transfer_locked(scope, transfer_id)
            if current.revision != expected_revision:
                raise FoundationError("transfer_stale", "Transfer revision changed")
            updated = current.model_copy(update={
                "status": "failed", "revision": current.revision + 1,
                "error_code": error_code, "updated_at": datetime.now(timezone.utc),
            })
            self._update_transfer_locked(updated, expected_revision)
        return updated

    def diagnostics(self, scope):
        key = _scope(scope)
        with self._lock:
            counts = {
                name: self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE scope_key=?", (key,)
                ).fetchone()[0]
                for name, table in {
                    "releases": "af_management_releases",
                    "indexes": "af_management_indexes",
                    "evaluations": "af_management_evaluations",
                    "transfers": "af_management_transfers",
                }.items()
            }
            active_releases = self._connection.execute(
                "SELECT kind,artifact_id,release_id FROM af_management_releases WHERE scope_key=? AND status='active' ORDER BY kind,artifact_id",
                (key,),
            ).fetchall()
            active_indexes = self._connection.execute(
                "SELECT domain,generation_id FROM af_management_indexes WHERE scope_key=? AND status='active' ORDER BY domain",
                (key,),
            ).fetchall()
        return {
            **counts,
            "active_releases": [dict(row) for row in active_releases],
            "active_indexes": [dict(row) for row in active_indexes],
        }

    def get_receipt(self, scope, operation_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM af_management_operations WHERE scope_key=? AND operation_id=?",
                (_scope(scope), operation_id),
            ).fetchone()
        return self._receipt_model(row) if row else None

    def close(self):
        with self._lock:
            self._connection.close()

    def _transition_index(self, scope, generation_id, *, expected_revision, expected_status, status, effect_receipt, actor, operation_id, command_digest, action):
        replay = self._replay(scope, operation_id, command_digest)
        if replay:
            return self.get_index(scope, replay.resource_id)
        with self._transaction():
            current = self._required_index_locked(scope, generation_id)
            if current.revision != expected_revision or current.status != expected_status:
                raise FoundationError("index_generation_stale", f"Expected {expected_status} generation is required")
            updated = current.model_copy(update={
                "status": status, "revision": current.revision + 1,
                "effect_receipt": effect_receipt,
                "updated_at": datetime.now(timezone.utc),
            })
            self._update_index_locked(updated, expected_revision)
            self._receipt_locked(scope, operation_id, command_digest, action, generation_id, updated.revision)
        return updated

    def _list_payloads(self, table, scope, *, filters, order_by, limit, model):
        query = f"SELECT payload FROM {table} WHERE scope_key=?"
        parameters: list[object] = [_scope(scope)]
        for column, value in filters:
            if value is not None:
                query += f" AND {column}=?"
                parameters.append(value)
        query += f" ORDER BY {order_by} LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return tuple(model.model_validate_json(row["payload"]) for row in rows)

    def _replay(self, scope, operation_id, digest):
        receipt = self.get_receipt(scope, operation_id)
        if receipt and receipt.command_digest != digest:
            raise FoundationError("operation_id_conflict", "Operation ID has different content")
        return receipt

    def _replay_locked(self, scope, operation_id, digest):
        row = self._connection.execute(
            "SELECT * FROM af_management_operations WHERE scope_key=? AND operation_id=?",
            (_scope(scope), operation_id),
        ).fetchone()
        if row and row["command_digest"] != digest:
            raise FoundationError("operation_id_conflict", "Operation ID has different content")
        return self._receipt_model(row) if row else None

    def _receipt_locked(self, scope, operation_id, digest, action, resource_id, revision):
        self._connection.execute(
            "INSERT INTO af_management_operations VALUES(?,?,?,?,?,?,?)",
            (_scope(scope), operation_id, digest, action, resource_id, revision,
             datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _receipt_model(row):
        return ManagementOperationReceipt(
            operation_id=row["operation_id"], command_digest=row["command_digest"],
            action=row["action"], resource_id=row["resource_id"],
            resource_revision=row["resource_revision"], committed_at=row["committed_at"],
        )

    def _release_locked(self, scope, release_id):
        row = self._connection.execute(
            "SELECT payload FROM af_management_releases WHERE scope_key=? AND release_id=?",
            (_scope(scope), release_id),
        ).fetchone()
        return ManagedRelease.model_validate_json(row["payload"]) if row else None

    def _required_release_locked(self, scope, release_id):
        value = self._release_locked(scope, release_id)
        if value is None:
            raise FoundationError("management_release_not_found", "Release does not exist")
        return value

    def _update_release_locked(self, value, expected_revision):
        cursor = self._connection.execute(
            "UPDATE af_management_releases SET status=?,revision=?,payload=? WHERE scope_key=? AND release_id=? AND revision=?",
            (value.status, value.revision, _payload(value), _scope(value.scope), value.release_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise FoundationError("management_release_stale", "Release revision changed")

    def _index_locked(self, scope, generation_id):
        row = self._connection.execute(
            "SELECT payload FROM af_management_indexes WHERE scope_key=? AND generation_id=?",
            (_scope(scope), generation_id),
        ).fetchone()
        return IndexGeneration.model_validate_json(row["payload"]) if row else None

    def _required_index_locked(self, scope, generation_id):
        value = self._index_locked(scope, generation_id)
        if value is None:
            raise FoundationError("index_generation_not_found", "Index generation does not exist")
        return value

    def _update_index_locked(self, value, expected_revision):
        cursor = self._connection.execute(
            "UPDATE af_management_indexes SET status=?,revision=?,payload=? WHERE scope_key=? AND generation_id=? AND revision=?",
            (value.status, value.revision, _payload(value), _scope(value.scope), value.generation_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise FoundationError("index_generation_stale", "Index revision changed")

    def _transfer_locked(self, scope, transfer_id):
        row = self._connection.execute(
            "SELECT payload FROM af_management_transfers WHERE scope_key=? AND transfer_id=?",
            (_scope(scope), transfer_id),
        ).fetchone()
        return TransferPlan.model_validate_json(row["payload"]) if row else None

    def _required_transfer_locked(self, scope, transfer_id):
        value = self._transfer_locked(scope, transfer_id)
        if value is None:
            raise FoundationError("transfer_not_found", "Transfer does not exist")
        return value

    def _update_transfer_locked(self, value, expected_revision):
        cursor = self._connection.execute(
            "UPDATE af_management_transfers SET status=?,revision=?,payload=? WHERE scope_key=? AND transfer_id=? AND revision=?",
            (value.status, value.revision, _payload(value), _scope(value.scope), value.transfer_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise FoundationError("transfer_stale", "Transfer revision changed")

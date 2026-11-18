from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.context.contracts import (
    ContextOperationReceipt,
    ContextSnapshot,
    ResidentContext,
)


def _scope_key(scope: ScopeRef) -> str:
    return scope.model_dump_json()


class SqliteContextAuthority:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS af_context_snapshots (
                    scope_key TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_key, snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS af_context_resident (
                    scope_key TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_context_operations (
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result_kind TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (scope_key, operation_id)
                );
                CREATE TABLE IF NOT EXISTS af_context_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )

    def put_snapshot(self, snapshot, *, operation_id: str, command_digest: str):
        key = _scope_key(snapshot.scope)
        replay = self._replay(key, operation_id, command_digest)
        if replay is not None:
            if replay.result_kind != "snapshot":
                raise FoundationError("operation_id_conflict", "Operation result kind conflicts")
            existing = self.get_snapshot(snapshot.scope, replay.resource_id)
            if existing is None:
                raise FoundationError("context_receipt_corrupt", "Snapshot receipt target is missing")
            return existing
        now = datetime.now(timezone.utc)
        with self._connection:
            self._connection.execute(
                "INSERT INTO af_context_snapshots VALUES (?, ?, ?, ?)",
                (key, snapshot.snapshot_id, snapshot.model_dump_json(), snapshot.created_at.isoformat()),
            )
            self._insert_receipt(
                key, operation_id, command_digest, "create_snapshot", "snapshot", snapshot.snapshot_id, now
            )
        return snapshot

    def get_snapshot(self, scope: ScopeRef, snapshot_id: str):
        row = self._connection.execute(
            "SELECT payload_json FROM af_context_snapshots WHERE scope_key=? AND snapshot_id=?",
            (_scope_key(scope), snapshot_id),
        ).fetchone()
        return None if row is None else ContextSnapshot.model_validate_json(row["payload_json"])

    def list_snapshots(self, scope: ScopeRef, *, limit: int):
        rows = self._connection.execute(
            "SELECT payload_json FROM af_context_snapshots WHERE scope_key=? ORDER BY created_at DESC, snapshot_id LIMIT ?",
            (_scope_key(scope), limit),
        ).fetchall()
        return tuple(ContextSnapshot.model_validate_json(row["payload_json"]) for row in rows)

    def get_resident(self, scope: ScopeRef):
        row = self._connection.execute(
            "SELECT payload_json FROM af_context_resident WHERE scope_key=?", (_scope_key(scope),)
        ).fetchone()
        return None if row is None else ResidentContext.model_validate_json(row["payload_json"])

    def replace_resident(
        self,
        resident: ResidentContext,
        *,
        expected_revision: int | None,
        operation_id: str,
        command_digest: str,
    ):
        key = _scope_key(resident.scope)
        replay = self._replay(key, operation_id, command_digest)
        if replay is not None:
            if replay.result_kind != "resident":
                raise FoundationError("operation_id_conflict", "Operation result kind conflicts")
            current = self.get_resident(resident.scope)
            if current is None or str(current.revision) != replay.resource_id:
                raise FoundationError("context_receipt_corrupt", "Resident receipt target is missing")
            return current
        now = datetime.now(timezone.utc)
        with self._connection:
            row = self._connection.execute(
                "SELECT revision FROM af_context_resident WHERE scope_key=?", (key,)
            ).fetchone()
            current_revision = None if row is None else int(row["revision"])
            if current_revision != expected_revision:
                raise FoundationError("context_revision_conflict", "Resident Context revision changed")
            next_revision = 1 if current_revision is None else current_revision + 1
            stored = resident.model_copy(update={"revision": next_revision, "updated_at": now})
            self._connection.execute(
                """
                INSERT INTO af_context_resident(scope_key, revision, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    revision=excluded.revision,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (key, next_revision, stored.model_dump_json(), now.isoformat()),
            )
            self._insert_receipt(
                key, operation_id, command_digest, "replace_resident", "resident", str(next_revision), now
            )
        return stored

    def get_operation_receipt(self, scope: ScopeRef, operation_id: str):
        row = self._connection.execute(
            "SELECT * FROM af_context_operations WHERE scope_key=? AND operation_id=?",
            (_scope_key(scope), operation_id),
        ).fetchone()
        return None if row is None else self._receipt(row)

    def close(self) -> None:
        self._connection.close()

    def _replay(self, scope_key: str, operation_id: str, command_digest: str):
        row = self._connection.execute(
            "SELECT * FROM af_context_operations WHERE scope_key=? AND operation_id=?",
            (scope_key, operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["command_digest"] != command_digest:
            raise FoundationError("operation_id_conflict", "Operation ID already has different content")
        return self._receipt(row)

    def _insert_receipt(
        self,
        scope_key: str,
        operation_id: str,
        command_digest: str,
        action: str,
        result_kind: str,
        resource_id: str,
        committed_at: datetime,
    ) -> None:
        self._connection.execute(
            "INSERT INTO af_context_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope_key, operation_id, command_digest, action, result_kind, resource_id, committed_at.isoformat()),
        )
        self._connection.execute(
            "INSERT INTO af_context_audit(scope_key, action, resource_id, operation_id, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (scope_key, action, resource_id, operation_id, committed_at.isoformat()),
        )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> ContextOperationReceipt:
        return ContextOperationReceipt(
            operation_id=row["operation_id"],
            command_digest=row["command_digest"],
            action=row["action"],
            result_kind=row["result_kind"],
            resource_id=row["resource_id"],
            committed_at=datetime.fromisoformat(row["committed_at"]),
        )

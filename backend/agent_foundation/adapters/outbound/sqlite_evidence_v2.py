from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef, stable_fingerprint
from agent_foundation.modules.evidence.contracts import (
    EvidenceOperationReceipt,
    EvidenceRevision,
    EvidenceRevisionPage,
    EvidenceSupport,
    SourceRange,
)


def scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class SqliteEvidenceAuthority:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = RLock()
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS af_evidence_revisions (
                    scope_key TEXT NOT NULL,
                    revision_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(scope_key, source_id, source_version)
                );
                CREATE TABLE IF NOT EXISTS af_evidence_supports (
                    scope_key TEXT NOT NULL,
                    support_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_evidence_operations (
                    operation_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result_kind TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    authority_sequence INTEGER NOT NULL,
                    PRIMARY KEY(scope_key, operation_id)
                );
                CREATE TABLE IF NOT EXISTS af_evidence_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )

    def admit(
        self,
        scope: ScopeRef,
        *,
        source_id: str,
        content: str,
        source_range: SourceRange | None,
        operation_id: str,
        command_digest: str,
    ) -> EvidenceRevision:
        with self._lock, self._connection:
            existing = self._existing(scope, operation_id, command_digest, "revision", EvidenceRevision)
            if existing is not None:
                return existing
            key = scope_key(scope)
            row = self._connection.execute(
                "SELECT COALESCE(MAX(source_version), 0) FROM af_evidence_revisions WHERE scope_key=? AND source_id=?",
                (key, source_id),
            ).fetchone()
            previous = self._connection.execute(
                "SELECT revision_id, payload FROM af_evidence_revisions "
                "WHERE scope_key=? AND source_id=? ORDER BY source_version DESC LIMIT 1",
                (key, source_id),
            ).fetchone()
            if previous is not None:
                earlier = EvidenceRevision.model_validate_json(previous["payload"])
                if earlier.status == "active":
                    earlier = earlier.model_copy(update={"status": "superseded"})
                    self._connection.execute(
                        "UPDATE af_evidence_revisions SET payload=? WHERE scope_key=? AND revision_id=?",
                        (earlier.model_dump_json(), key, previous["revision_id"]),
                    )
            revision = EvidenceRevision(
                scope=scope,
                source_id=source_id,
                source_version=int(row[0]) + 1,
                content=content,
                source_range=source_range,
            )
            self._connection.execute(
                "INSERT INTO af_evidence_revisions VALUES (?, ?, ?, ?, ?)",
                (key, revision.revision_id, source_id, revision.source_version, revision.model_dump_json()),
            )
            self._record(scope, operation_id, command_digest, "revision", revision, "admit", revision.revision_id)
            return revision

    def get_revision(self, scope: ScopeRef, revision_id: str) -> EvidenceRevision | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_evidence_revisions WHERE scope_key=? AND revision_id=?",
                (scope_key(scope), revision_id),
            ).fetchone()
        return None if row is None else EvidenceRevision.model_validate_json(row[0])

    def list_revisions(
        self,
        scope: ScopeRef,
        *,
        states: tuple[str, ...],
        source_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> EvidenceRevisionPage:
        key = scope_key(scope)
        watermark = self.authority_watermark(scope)
        offset = self._decode_cursor(cursor, key=key, states=states, source_id=source_id, watermark=watermark)
        clauses = ["scope_key=?", f"json_extract(payload, '$.status') IN ({','.join('?' for _ in states)})"]
        parameters: list[object] = [key, *states]
        if source_id is not None:
            clauses.append("source_id=?")
            parameters.append(source_id)
        parameters.extend((limit + 1, offset))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM af_evidence_revisions WHERE {' AND '.join(clauses)} "
                "ORDER BY source_id, source_version, revision_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        items = tuple(EvidenceRevision.model_validate_json(row[0]) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            next_cursor = self._encode_cursor(
                offset=offset + limit,
                key=key,
                states=states,
                source_id=source_id,
                watermark=watermark,
            )
        return EvidenceRevisionPage(items=items, next_cursor=next_cursor, authority_watermark=watermark)

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> EvidenceOperationReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT operation_id, command_digest, action, result_kind, resource_id, "
                "committed_at, authority_sequence FROM af_evidence_operations "
                "WHERE scope_key=? AND operation_id=?",
                (scope_key(scope), operation_id),
            ).fetchone()
        if row is None:
            return None
        return EvidenceOperationReceipt(
            operation_id=row["operation_id"],
            command_digest=row["command_digest"],
            action=row["action"],
            result_kind=row["result_kind"],
            resource_id=row["resource_id"],
            committed_at=row["committed_at"],
            authority_watermark=row["authority_sequence"],
        )

    def authority_watermark(self, scope: ScopeRef) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM af_evidence_audit WHERE scope_key=?",
                (scope_key(scope),),
            ).fetchone()
        return int(row[0])

    def candidates(
        self,
        scope: ScopeRef,
        *,
        source_ids: tuple[str, ...],
        revision_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[EvidenceRevision, ...]:
        clauses = ["r.scope_key=?", "json_extract(r.payload, '$.status')='active'", "r.source_version=(SELECT MAX(x.source_version) FROM af_evidence_revisions x WHERE x.scope_key=r.scope_key AND x.source_id=r.source_id)"]
        parameters: list[object] = [scope_key(scope)]
        if source_ids:
            clauses.append(f"r.source_id IN ({','.join('?' for _ in source_ids)})")
            parameters.extend(source_ids)
        if revision_ids:
            clauses.append(f"r.revision_id IN ({','.join('?' for _ in revision_ids)})")
            parameters.extend(revision_ids)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT r.payload FROM af_evidence_revisions r WHERE {' AND '.join(clauses)} ORDER BY r.source_id, r.source_version DESC LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(EvidenceRevision.model_validate_json(row[0]) for row in rows)

    def revoke(
        self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str
    ) -> EvidenceRevision:
        with self._lock, self._connection:
            existing = self._existing(scope, operation_id, command_digest, "revision", EvidenceRevision)
            if existing is not None:
                return existing
            revision = self._required_revision(scope, revision_id)
            revoked = revision.model_copy(update={"status": "revoked"})
            self._connection.execute(
                "UPDATE af_evidence_revisions SET payload=? WHERE scope_key=? AND revision_id=?",
                (revoked.model_dump_json(), scope_key(scope), revision_id),
            )
            self._record(scope, operation_id, command_digest, "revision", revoked, "revoke", revision_id)
            return revoked

    def revalidate(
        self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str
    ) -> EvidenceRevision:
        with self._lock, self._connection:
            existing = self._existing(scope, operation_id, command_digest, "revision", EvidenceRevision)
            if existing is not None:
                return existing
            revision = self._required_revision(scope, revision_id)
            if revision.status != "active":
                raise FoundationError("evidence_revoked", "Revoked Evidence cannot be revalidated")
            updated = revision.model_copy(update={"validated_at": datetime.now(timezone.utc)})
            self._connection.execute(
                "UPDATE af_evidence_revisions SET payload=? WHERE scope_key=? AND revision_id=?",
                (updated.model_dump_json(), scope_key(scope), revision_id),
            )
            self._record(scope, operation_id, command_digest, "revision", updated, "revalidate", revision_id)
            return updated

    def put_support(
        self, support: EvidenceSupport, *, operation_id: str, command_digest: str
    ) -> EvidenceSupport:
        with self._lock, self._connection:
            existing = self._existing(support.scope, operation_id, command_digest, "support", EvidenceSupport)
            if existing is not None:
                return existing
            self._connection.execute(
                "INSERT INTO af_evidence_supports VALUES (?, ?, ?)",
                (scope_key(support.scope), support.support_id, support.model_dump_json()),
            )
            self._record(
                support.scope, operation_id, command_digest, "support", support, "support", support.support_id
            )
            return support

    def get_support(self, scope: ScopeRef, support_id: str) -> EvidenceSupport | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM af_evidence_supports WHERE scope_key=? AND support_id=?",
                (scope_key(scope), support_id),
            ).fetchone()
        return None if row is None else EvidenceSupport.model_validate_json(row[0])

    def is_current(self, ref: ResourceRef) -> bool:
        if ref.domain != "evidence" or ref.resource_type != "evidence_revision" or ref.revision != ref.resource_id:
            return False
        revision = self.get_revision(ref.scope, ref.resource_id)
        if revision is None or revision.status != "active":
            return False
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(source_version) FROM af_evidence_revisions WHERE scope_key=? AND source_id=?",
                (scope_key(ref.scope), revision.source_id),
            ).fetchone()
        return row is not None and int(row[0]) == revision.source_version

    def close(self) -> None:
        self._connection.close()

    def _required_revision(self, scope: ScopeRef, revision_id: str) -> EvidenceRevision:
        revision = self.get_revision(scope, revision_id)
        if revision is None:
            raise FoundationError("evidence_not_found", "Evidence revision does not exist")
        return revision

    def _existing(self, scope: ScopeRef, operation_id: str, digest: str, kind: str, model):
        row = self._connection.execute(
            "SELECT command_digest, result_kind, result_payload "
            "FROM af_evidence_operations WHERE scope_key=? AND operation_id=?",
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
        digest: str,
        kind: str,
        result,
        action: str,
        resource_id: str,
    ) -> None:
        committed_at = datetime.now(timezone.utc).isoformat()
        audit = self._connection.execute(
            "INSERT INTO af_evidence_audit(scope_key, operation_id, action, resource_id, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope_key(scope), operation_id, action, resource_id, committed_at),
        )
        self._connection.execute(
            "INSERT INTO af_evidence_operations "
            "(operation_id, scope_key, command_digest, action, result_kind, result_payload, resource_id, "
            "committed_at, authority_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                scope_key(scope),
                digest,
                action,
                kind,
                result.model_dump_json(),
                resource_id,
                committed_at,
                audit.lastrowid,
            ),
        )

    @staticmethod
    def _cursor_identity(*, key: str, states: tuple[str, ...], source_id: str | None) -> str:
        return stable_fingerprint({"scope_key": key, "states": states, "source_id": source_id})

    @classmethod
    def _encode_cursor(
        cls,
        *,
        offset: int,
        key: str,
        states: tuple[str, ...],
        source_id: str | None,
        watermark: int,
    ) -> str:
        payload = json.dumps(
            {
                "offset": offset,
                "identity": cls._cursor_identity(key=key, states=states, source_id=source_id),
                "watermark": watermark,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def _decode_cursor(
        cls,
        cursor: str | None,
        *,
        key: str,
        states: tuple[str, ...],
        source_id: str | None,
        watermark: int,
    ) -> int:
        if cursor is None:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
            raise FoundationError("evidence_cursor_invalid", "Evidence cursor is invalid") from error
        identity = cls._cursor_identity(key=key, states=states, source_id=source_id)
        if payload.get("identity") != identity:
            raise FoundationError("evidence_cursor_invalid", "Evidence cursor does not match this query")
        if payload.get("watermark") != watermark:
            raise FoundationError("evidence_cursor_stale", "Evidence changed while this page was being read")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise FoundationError("evidence_cursor_invalid", "Evidence cursor offset is invalid")
        return offset

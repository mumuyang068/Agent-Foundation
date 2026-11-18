from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json

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


def _payload(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class PostgresEvidenceAuthority:
    """PostgreSQL Evidence authority using per-scope transactional serialization.

    The host owns connection creation. Mutations lock one scope row, making
    version selection, prior-version retirement, audit sequencing, and the
    idempotency receipt one atomic authority transaction.
    """

    def __init__(self, connection) -> None:
        self._connection = connection
        self._connection.autocommit = True
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_evidence_scope_state (
                        scope_key TEXT PRIMARY KEY,
                        authority_sequence BIGINT NOT NULL DEFAULT 0
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_evidence_revisions (
                        scope_key TEXT NOT NULL,
                        revision_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL,
                        payload JSONB NOT NULL,
                        UNIQUE(scope_key, source_id, source_version)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_evidence_supports (
                        scope_key TEXT NOT NULL,
                        support_id TEXT PRIMARY KEY,
                        payload JSONB NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_evidence_operations (
                        operation_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        command_digest TEXT NOT NULL,
                        action TEXT NOT NULL,
                        result_kind TEXT NOT NULL,
                        result_payload JSONB NOT NULL,
                        resource_id TEXT NOT NULL,
                        committed_at TIMESTAMPTZ NOT NULL,
                        authority_sequence BIGINT NOT NULL,
                        PRIMARY KEY(scope_key, operation_id)
                    )
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
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, scope)
                existing = self._existing(cursor, scope, operation_id, command_digest, "revision", EvidenceRevision)
                if existing is not None:
                    return existing
                key = scope_key(scope)
                cursor.execute(
                    "SELECT COALESCE(MAX(source_version), 0) FROM af_evidence_revisions "
                    "WHERE scope_key=%s AND source_id=%s",
                    (key, source_id),
                )
                next_version = int(cursor.fetchone()[0]) + 1
                cursor.execute(
                    "SELECT revision_id, payload FROM af_evidence_revisions "
                    "WHERE scope_key=%s AND source_id=%s ORDER BY source_version DESC LIMIT 1 FOR UPDATE",
                    (key, source_id),
                )
                previous = cursor.fetchone()
                if previous is not None:
                    earlier = EvidenceRevision.model_validate_json(_payload(previous[1]))
                    if earlier.status == "active":
                        earlier = earlier.model_copy(update={"status": "superseded"})
                        cursor.execute(
                            "UPDATE af_evidence_revisions SET payload=%s::jsonb "
                            "WHERE scope_key=%s AND revision_id=%s",
                            (earlier.model_dump_json(), key, previous[0]),
                        )
                revision = EvidenceRevision(
                    scope=scope,
                    source_id=source_id,
                    source_version=next_version,
                    content=content,
                    source_range=source_range,
                )
                cursor.execute(
                    "INSERT INTO af_evidence_revisions "
                    "(scope_key, revision_id, source_id, source_version, payload) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb)",
                    (key, revision.revision_id, source_id, next_version, revision.model_dump_json()),
                )
                self._record(cursor, scope, operation_id, command_digest, "revision", revision, "admit", revision.revision_id)
                return revision

    def get_revision(self, scope: ScopeRef, revision_id: str) -> EvidenceRevision | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM af_evidence_revisions WHERE scope_key=%s AND revision_id=%s",
                (scope_key(scope), revision_id),
            )
            row = cursor.fetchone()
        return None if row is None else EvidenceRevision.model_validate_json(_payload(row[0]))

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
        clauses = ["scope_key=%s", "payload->>'status'=ANY(%s)"]
        parameters: list[object] = [key, list(states)]
        if source_id is not None:
            clauses.append("source_id=%s")
            parameters.append(source_id)
        parameters.extend((limit + 1, offset))
        with self._connection.cursor() as db_cursor:
            db_cursor.execute(
                f"SELECT payload FROM af_evidence_revisions WHERE {' AND '.join(clauses)} "
                "ORDER BY source_id, source_version, revision_id LIMIT %s OFFSET %s",
                parameters,
            )
            rows = db_cursor.fetchall()
        items = tuple(EvidenceRevision.model_validate_json(_payload(row[0])) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            next_cursor = self._encode_cursor(
                offset=offset + limit, key=key, states=states, source_id=source_id, watermark=watermark
            )
        return EvidenceRevisionPage(items=items, next_cursor=next_cursor, authority_watermark=watermark)

    def get_operation_receipt(self, scope: ScopeRef, operation_id: str) -> EvidenceOperationReceipt | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT operation_id, command_digest, action, result_kind, resource_id, "
                "committed_at, authority_sequence FROM af_evidence_operations "
                "WHERE scope_key=%s AND operation_id=%s",
                (scope_key(scope), operation_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return EvidenceOperationReceipt(
            operation_id=row[0], command_digest=row[1], action=row[2], result_kind=row[3],
            resource_id=row[4], committed_at=row[5], authority_watermark=int(row[6]),
        )

    def authority_watermark(self, scope: ScopeRef) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT authority_sequence FROM af_evidence_scope_state WHERE scope_key=%s",
                (scope_key(scope),),
            )
            row = cursor.fetchone()
        return 0 if row is None else int(row[0])

    def candidates(
        self,
        scope: ScopeRef,
        *,
        source_ids: tuple[str, ...],
        revision_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[EvidenceRevision, ...]:
        clauses = [
            "r.scope_key=%s",
            "r.payload->>'status'='active'",
            "r.source_version=(SELECT MAX(x.source_version) FROM af_evidence_revisions x "
            "WHERE x.scope_key=r.scope_key AND x.source_id=r.source_id)",
        ]
        parameters: list[object] = [scope_key(scope)]
        if source_ids:
            clauses.append("r.source_id=ANY(%s)")
            parameters.append(list(source_ids))
        if revision_ids:
            clauses.append("r.revision_id=ANY(%s)")
            parameters.append(list(revision_ids))
        parameters.append(limit)
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT r.payload FROM af_evidence_revisions r WHERE {' AND '.join(clauses)} "
                "ORDER BY r.source_id, r.source_version DESC LIMIT %s",
                parameters,
            )
            rows = cursor.fetchall()
        return tuple(EvidenceRevision.model_validate_json(_payload(row[0])) for row in rows)

    def revoke(self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str) -> EvidenceRevision:
        return self._change_revision(scope, revision_id, operation_id, command_digest, "revoke")

    def revalidate(self, scope: ScopeRef, revision_id: str, *, operation_id: str, command_digest: str) -> EvidenceRevision:
        return self._change_revision(scope, revision_id, operation_id, command_digest, "revalidate")

    def _change_revision(
        self, scope: ScopeRef, revision_id: str, operation_id: str, command_digest: str, action: str
    ) -> EvidenceRevision:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, scope)
                existing = self._existing(cursor, scope, operation_id, command_digest, "revision", EvidenceRevision)
                if existing is not None:
                    return existing
                cursor.execute(
                    "SELECT payload FROM af_evidence_revisions WHERE scope_key=%s AND revision_id=%s FOR UPDATE",
                    (scope_key(scope), revision_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise FoundationError("evidence_not_found", "Evidence revision does not exist")
                revision = EvidenceRevision.model_validate_json(_payload(row[0]))
                if action == "revalidate":
                    if revision.status != "active":
                        raise FoundationError("evidence_revoked", "Revoked Evidence cannot be revalidated")
                    updated = revision.model_copy(update={"validated_at": datetime.now(timezone.utc)})
                else:
                    updated = revision.model_copy(update={"status": "revoked"})
                cursor.execute(
                    "UPDATE af_evidence_revisions SET payload=%s::jsonb WHERE scope_key=%s AND revision_id=%s",
                    (updated.model_dump_json(), scope_key(scope), revision_id),
                )
                self._record(cursor, scope, operation_id, command_digest, "revision", updated, action, revision_id)
                return updated

    def put_support(self, support: EvidenceSupport, *, operation_id: str, command_digest: str) -> EvidenceSupport:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, support.scope)
                existing = self._existing(cursor, support.scope, operation_id, command_digest, "support", EvidenceSupport)
                if existing is not None:
                    return existing
                cursor.execute(
                    "INSERT INTO af_evidence_supports(scope_key, support_id, payload) VALUES (%s, %s, %s::jsonb)",
                    (scope_key(support.scope), support.support_id, support.model_dump_json()),
                )
                self._record(cursor, support.scope, operation_id, command_digest, "support", support, "support", support.support_id)
                return support

    def get_support(self, scope: ScopeRef, support_id: str) -> EvidenceSupport | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM af_evidence_supports WHERE scope_key=%s AND support_id=%s",
                (scope_key(scope), support_id),
            )
            row = cursor.fetchone()
        return None if row is None else EvidenceSupport.model_validate_json(_payload(row[0]))

    def is_current(self, ref: ResourceRef) -> bool:
        if ref.domain != "evidence" or ref.resource_type != "evidence_revision" or ref.revision != ref.resource_id:
            return False
        revision = self.get_revision(ref.scope, ref.resource_id)
        if revision is None or revision.status != "active":
            return False
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT MAX(source_version) FROM af_evidence_revisions WHERE scope_key=%s AND source_id=%s",
                (scope_key(ref.scope), revision.source_id),
            )
            row = cursor.fetchone()
        return row is not None and int(row[0]) == revision.source_version

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _lock_scope(cursor, scope: ScopeRef) -> None:
        key = scope_key(scope)
        cursor.execute(
            "INSERT INTO af_evidence_scope_state(scope_key, authority_sequence) VALUES (%s, 0) "
            "ON CONFLICT (scope_key) DO NOTHING",
            (key,),
        )
        cursor.execute(
            "SELECT authority_sequence FROM af_evidence_scope_state WHERE scope_key=%s FOR UPDATE",
            (key,),
        )
        cursor.fetchone()

    @staticmethod
    def _existing(cursor, scope: ScopeRef, operation_id: str, digest: str, kind: str, model):
        cursor.execute(
            "SELECT command_digest, result_kind, result_payload "
            "FROM af_evidence_operations WHERE scope_key=%s AND operation_id=%s",
            (scope_key(scope), operation_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row[0] != digest or row[1] != kind:
            raise FoundationError("operation_id_conflict", "Operation ID already has different command content")
        return model.model_validate_json(_payload(row[2]))

    @staticmethod
    def _record(cursor, scope: ScopeRef, operation_id: str, digest: str, kind: str, result, action: str, resource_id: str) -> None:
        committed_at = datetime.now(timezone.utc)
        key = scope_key(scope)
        cursor.execute(
            "UPDATE af_evidence_scope_state SET authority_sequence=authority_sequence+1 "
            "WHERE scope_key=%s RETURNING authority_sequence",
            (key,),
        )
        sequence = int(cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO af_evidence_operations "
            "(operation_id, scope_key, command_digest, action, result_kind, result_payload, resource_id, "
            "committed_at, authority_sequence) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
            (operation_id, key, digest, action, kind, result.model_dump_json(), resource_id, committed_at, sequence),
        )

    @staticmethod
    def _cursor_identity(*, key: str, states: tuple[str, ...], source_id: str | None) -> str:
        return stable_fingerprint({"scope_key": key, "states": states, "source_id": source_id})

    @classmethod
    def _encode_cursor(cls, *, offset: int, key: str, states: tuple[str, ...], source_id: str | None, watermark: int) -> str:
        payload = json.dumps(
            {"offset": offset, "identity": cls._cursor_identity(key=key, states=states, source_id=source_id), "watermark": watermark},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def _decode_cursor(cls, cursor: str | None, *, key: str, states: tuple[str, ...], source_id: str | None, watermark: int) -> int:
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

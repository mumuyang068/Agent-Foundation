from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from threading import RLock

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.modules.access.domain import AccessState


@dataclass(frozen=True)
class StoredAccessState:
    revision: int
    state: AccessState


class InMemoryAccessStateRepository:
    def __init__(self) -> None:
        self._current = StoredAccessState(revision=0, state=AccessState())
        self._lock = RLock()

    def read(self) -> StoredAccessState:
        with self._lock:
            return self._current

    def compare_and_swap(self, expected_revision: int, state: AccessState) -> StoredAccessState:
        with self._lock:
            if self._current.revision != expected_revision:
                raise FoundationError("access_state_conflict", "Access state changed", retryable=True)
            self._current = StoredAccessState(revision=expected_revision + 1, state=state)
            return self._current

    def close(self) -> None:
        return None


class SqliteAccessStateRepository:
    """One-row access authority with compare-and-swap revision semantics."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS af_access_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO af_access_state(singleton, revision, payload) VALUES (1, 0, ?)",
                (AccessState().model_dump_json(),),
            )

    def read(self) -> StoredAccessState:
        with self._lock:
            row = self._connection.execute(
                "SELECT revision, payload FROM af_access_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise FoundationError("access_state_unavailable", "Access state was not initialized", retryable=True)
        return StoredAccessState(revision=int(row[0]), state=AccessState.model_validate_json(row[1]))

    def compare_and_swap(self, expected_revision: int, state: AccessState) -> StoredAccessState:
        encoded = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE af_access_state SET revision=revision+1, payload=? WHERE singleton=1 AND revision=?",
                (encoded, expected_revision),
            )
            if cursor.rowcount != 1:
                raise FoundationError("access_state_conflict", "Access state changed", retryable=True)
        return StoredAccessState(revision=expected_revision + 1, state=state)

    def close(self) -> None:
        self._connection.close()


class PostgresAccessStateRepository:
    """PostgreSQL access authority with transactional compare-and-swap semantics.

    The adapter accepts an already-created DB-API connection so the host remains
    the only layer that selects drivers and opens infrastructure resources.
    """

    def __init__(self, connection) -> None:
        self._connection = connection
        # Reads must not leave an implicit transaction holding the singleton
        # row while another repository performs compare-and-swap.
        self._connection.autocommit = True
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_access_state (
                        singleton SMALLINT PRIMARY KEY CHECK(singleton = 1),
                        revision BIGINT NOT NULL,
                        payload JSONB NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO af_access_state(singleton, revision, payload)
                    VALUES (1, 0, %s::jsonb)
                    ON CONFLICT (singleton) DO NOTHING
                    """,
                    (AccessState().model_dump_json(),),
                )

    def read(self) -> StoredAccessState:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT revision, payload FROM af_access_state WHERE singleton=1")
            row = cursor.fetchone()
        if row is None:
            raise FoundationError(
                "access_state_unavailable", "Access state was not initialized", retryable=True
            )
        payload = row[1] if isinstance(row[1], str) else json.dumps(row[1])
        return StoredAccessState(revision=int(row[0]), state=AccessState.model_validate_json(payload))

    def compare_and_swap(self, expected_revision: int, state: AccessState) -> StoredAccessState:
        encoded = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE af_access_state
                    SET revision=revision+1, payload=%s::jsonb
                    WHERE singleton=1 AND revision=%s
                    """,
                    (encoded, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise FoundationError(
                        "access_state_conflict", "Access state changed", retryable=True
                    )
        return StoredAccessState(revision=expected_revision + 1, state=state)

    def close(self) -> None:
        self._connection.close()

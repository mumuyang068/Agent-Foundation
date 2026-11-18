from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from agent_foundation.kernel.errors import FoundationError


def _safe(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not sanitized:
        raise FoundationError("management_artifact_id_invalid", "Artifact identity is invalid")
    return sanitized


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict) -> str:
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    expected = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _digest(path) != expected:
            raise FoundationError("management_effect_conflict", "Effect destination has different content")
        return expected
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return expected


def _replace_json(path: Path, payload: dict) -> str:
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest


def _atomic_bytes(path: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _digest(path) != digest:
            raise FoundationError("management_effect_conflict", "Managed artifact has different content")
        return digest
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest


class FilesystemReleaseEffect:
    _required = {
        "domain_policy": {"policy_id", "version", "domain"},
        "execution_profile": {"profile_id", "version", "domain", "algorithm_id", "implementation_version"},
        "extractor": {
            "extractor_id", "version", "input_schema", "output_schema",
            "implementation_id", "implementation_digest", "schema_id",
            "schema_version", "max_input_bytes", "configuration",
        },
        "provider": {"provider_id", "version", "kind", "endpoint_ref", "credential_ref", "capabilities"},
        "host_configuration": {"configuration_id", "version", "enabled_modules"},
        "graph_schema": {"schema_id", "version", "node_types", "relation_types"},
    }

    def __init__(self, root: Path, kind: str) -> None:
        self._root = root.resolve()
        self._kind = kind

    def validate(self, release) -> None:
        if release.kind != self._kind:
            raise FoundationError("management_release_kind_invalid", "Release effect kind does not match")
        missing = self._required[self._kind] - set(release.payload)
        if missing:
            raise FoundationError(
                "management_release_payload_invalid",
                "Release payload is missing: " + ", ".join(sorted(missing)),
            )
        if release.payload.get("version") != release.version:
            raise FoundationError("management_release_payload_invalid", "Payload version does not match release")

    def activate(self, release, *, operation_id: str) -> dict:
        self.validate(release)
        artifact = _safe(release.artifact_id)
        version_path = self._root / "releases" / release.kind / artifact / f"v{release.version}.json"
        version_digest = _atomic_json(version_path, {
            "release_id": release.release_id,
            "content_digest": release.content_digest,
            "payload": release.payload,
            "operation_id": operation_id,
        })
        pointer = self._root / "active" / release.kind / f"{artifact}.json"
        pointer_digest = _replace_json(pointer, {
            "release_id": release.release_id,
            "version": release.version,
            "content_digest": release.content_digest,
            "artifact_path": version_path.relative_to(self._root).as_posix(),
        })
        return {
            "effect": "filesystem-release-v1",
            "artifact_path": version_path.relative_to(self._root).as_posix(),
            "artifact_sha256": version_digest,
            "active_pointer": pointer.relative_to(self._root).as_posix(),
            "active_pointer_sha256": pointer_digest,
        }


class FilesystemIndexEffect:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def verify(self, generation) -> dict:
        path = Path(generation.artifact_path).resolve()
        if not path.is_file():
            raise FoundationError("index_artifact_unavailable", "Index artifact does not exist")
        digest = _digest(path)
        if digest != generation.artifact_digest:
            raise FoundationError("index_artifact_digest_mismatch", "Index artifact digest failed")
        return {"effect": "filesystem-index-verify-v1", "artifact_path": str(path), "sha256": digest}

    def activate(self, generation, *, operation_id: str) -> dict:
        verified = self.verify(generation)
        source = Path(verified["artifact_path"])
        managed = self._root / "index-artifacts" / f"{_safe(generation.generation_id)}.bin"
        managed_digest = _atomic_bytes(managed, source.read_bytes())
        pointer = self._root / "active-indexes" / f"{_safe(generation.domain)}.json"
        pointer_digest = _replace_json(pointer, {
            "generation_id": generation.generation_id,
            "source_revision": generation.source_revision,
            "execution_profile_fingerprint": generation.execution_profile_fingerprint,
            "artifact_path": managed.relative_to(self._root).as_posix(),
            "artifact_digest": managed_digest,
            "operation_id": operation_id,
        })
        return {
            **verified, "effect": "filesystem-index-activate-v1",
            "managed_artifact_path": managed.relative_to(self._root).as_posix(),
            "active_pointer": pointer.relative_to(self._root).as_posix(),
            "active_pointer_sha256": pointer_digest,
        }


class FilesystemTransferEffect:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def execute(self, plan, *, operation_id: str) -> dict:
        destination = (self._root / plan.destination).resolve()
        if destination != self._root and self._root not in destination.parents:
            raise FoundationError("transfer_destination_invalid", "Transfer destination escapes its root")
        if destination.suffix.lower() != ".json":
            raise FoundationError("transfer_destination_invalid", "Transfer destination must be a JSON file")
        digest = _atomic_json(destination, {
            "schema_version": "agent-foundation.controlled-transfer.v1",
            "transfer_id": plan.transfer_id,
            "kind": plan.kind,
            "scope": plan.scope.model_dump(mode="json"),
            "resource_refs": [ref.model_dump(mode="json") for ref in plan.resource_refs],
            "payload_digest": plan.payload_digest,
            "payload": plan.payload,
            "operation_id": operation_id,
        })
        return {
            "effect": "filesystem-transfer-v1", "destination": plan.destination,
            "sha256": digest, "payload_digest": plan.payload_digest,
        }

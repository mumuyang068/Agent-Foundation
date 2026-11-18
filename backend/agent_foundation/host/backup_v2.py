from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4
import zipfile

from agent_foundation.kernel.errors import FoundationError

from .configuration import HostConfiguration


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_stores(
    configuration: HostConfiguration,
) -> tuple[dict[str, Path], tuple[dict[str, str], ...]]:
    result: dict[str, Path] = {}
    roles: list[dict[str, str]] = []
    for module in configuration.enabled_modules:
        store_id = configuration.module_stores.get(module)
        declared = []
        if store_id is not None:
            declared.append(("control" if module == "graph" else "authority", store_id))
        write_store_id = configuration.module_write_stores.get(module)
        if write_store_id is not None:
            declared.append(("write", write_store_id))
        for role, declared_store_id in declared:
            store = configuration.stores[declared_store_id]
            if store.driver != "sqlite" or not store.endpoint_ref:
                raise FoundationError(
                    "backup_store_incompatible",
                    "Complete SQLite backup cannot cover "
                    f"{module!r} {role!r} store {declared_store_id!r} ({store.driver})",
                )
            resolved = Path(store.endpoint_ref).resolve()
            existing = result.get(declared_store_id)
            if existing is not None and existing != resolved:
                raise FoundationError(
                    "backup_store_identity_conflict",
                    f"Store {declared_store_id!r} resolves to different endpoints",
                )
            result[declared_store_id] = resolved
            roles.append({
                "module": module,
                "role": role,
                "store_id": declared_store_id,
            })
    return result, tuple(sorted(roles, key=lambda item: (item["module"], item["role"], item["store_id"])))


def _check_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise FoundationError("recovery_sqlite_invalid", "Backup SQLite file cannot be opened") from error
    if result is None or result[0] != "ok":
        raise FoundationError("recovery_sqlite_invalid", "Backup SQLite integrity check failed")


def create_sqlite_backup(configuration: HostConfiguration, destination: str | Path) -> dict:
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise FoundationError("backup_target_exists", "Backup destination must be empty")
    stores, store_roles = _sqlite_stores(configuration)
    if not stores:
        raise FoundationError("backup_store_missing", "No enabled SQLite stores are configured")
    root.mkdir(parents=True, exist_ok=True)
    records = []
    created = []
    try:
        for store_id, source in sorted(stores.items()):
            if not source.is_file():
                raise FoundationError("backup_source_missing", f"SQLite store {store_id!r} does not exist")
            target = root / f"{store_id}.sqlite3"
            source_connection = sqlite3.connect(source)
            backup_connection = sqlite3.connect(target)
            try:
                source_connection.backup(backup_connection)
            finally:
                backup_connection.close()
                source_connection.close()
            _check_sqlite(target)
            created.append(target)
            records.append({
                "store_id": store_id, "driver": "sqlite", "file": target.name,
                "sha256": _digest(target), "size": target.stat().st_size,
            })
        resources = []
        if "management" in configuration.enabled_modules:
            management_store_id = configuration.module_stores.get("management")
            if management_store_id is None:
                raise FoundationError("backup_store_missing", "Management store is not configured")
            effect_root = Path(configuration.stores[management_store_id].endpoint_ref).resolve().parent / "management-effects"
            archive = root / "management-effects.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                if effect_root.exists():
                    for member in sorted(item for item in effect_root.rglob("*") if item.is_file()):
                        bundle.write(member, member.relative_to(effect_root).as_posix())
            created.append(archive)
            resources.append({
                "resource_id": "management-effects", "kind": "zip-directory",
                "file": archive.name, "sha256": _digest(archive), "size": archive.stat().st_size,
            })
        manifest = {
            "schema_version": "agent-foundation.recovery-manifest.v2",
            "backup_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_configuration_fingerprint": configuration.fingerprint,
            "store_ids": sorted(stores),
            "store_roles": store_roles,
            "stores": records,
            "resources": resources,
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path)
        manifest["manifest_sha256"] = _digest(manifest_path)
        return manifest
    except BaseException:
        for path in created:
            if path.exists():
                path.unlink()
        raise


def restore_sqlite_backup(
    manifest_path: str | Path, target_configuration: HostConfiguration
) -> dict:
    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FoundationError("recovery_manifest_invalid", "Recovery manifest is unreadable") from error
    if manifest.get("schema_version") != "agent-foundation.recovery-manifest.v2":
        raise FoundationError("recovery_manifest_invalid", "Recovery manifest version is unsupported")
    targets, target_roles = _sqlite_stores(target_configuration)
    expected_ids = sorted(targets)
    records = manifest.get("stores")
    if (
        not isinstance(records, list)
        or sorted(manifest.get("store_ids", [])) != expected_ids
        or sorted(item.get("store_id") for item in records if isinstance(item, dict)) != expected_ids
        or len(records) != len(expected_ids)
        or manifest.get("store_roles") != list(target_roles)
    ):
        raise FoundationError("recovery_store_set_incomplete", "Backup store set does not match target")

    # Validate every member and every target before creating any target file.
    sources: dict[str, Path] = {}
    root = path.parent
    for item in records:
        store_id = item["store_id"]
        source = (root / str(item.get("file", ""))).resolve()
        if source.parent != root or not source.is_file():
            raise FoundationError("recovery_source_invalid", f"Backup file for {store_id!r} is unavailable")
        if source.stat().st_size != item.get("size") or _digest(source) != item.get("sha256"):
            raise FoundationError("recovery_digest_mismatch", f"Backup digest failed for {store_id!r}")
        _check_sqlite(source)
        target = targets[store_id]
        if target.exists():
            raise FoundationError("recovery_target_exists", f"Recovery target already exists for {store_id!r}")
        sources[store_id] = source

    resource_sources: dict[str, Path] = {}
    resources = manifest.get("resources", [])
    expected_resource_ids = ["management-effects"] if "management" in target_configuration.enabled_modules else []
    if sorted(item.get("resource_id") for item in resources if isinstance(item, dict)) != expected_resource_ids:
        raise FoundationError("recovery_resource_set_incomplete", "Backup resource set does not match target")
    for item in resources:
        resource_id = item["resource_id"]
        source = (root / str(item.get("file", ""))).resolve()
        if source.parent != root or not source.is_file():
            raise FoundationError("recovery_source_invalid", f"Backup resource {resource_id!r} is unavailable")
        if source.stat().st_size != item.get("size") or _digest(source) != item.get("sha256"):
            raise FoundationError("recovery_digest_mismatch", f"Backup resource digest failed for {resource_id!r}")
        try:
            with zipfile.ZipFile(source) as bundle:
                for member in bundle.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise FoundationError("recovery_archive_invalid", "Backup archive contains an unsafe path")
        except zipfile.BadZipFile as error:
            raise FoundationError("recovery_archive_invalid", "Backup resource archive is invalid") from error
        resource_sources[resource_id] = source

    staged: dict[str, Path] = {}
    installed: list[Path] = []
    staged_resources: dict[str, Path] = {}
    installed_resources: list[Path] = []
    try:
        for store_id in expected_ids:
            target = targets[store_id]
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore")
            shutil.copy2(sources[store_id], temporary)
            if _digest(temporary) != _digest(sources[store_id]):
                raise FoundationError("recovery_stage_mismatch", "Staged recovery file changed")
            _check_sqlite(temporary)
            staged[store_id] = temporary
        if "management-effects" in resource_sources:
            management_store_id = target_configuration.module_stores["management"]
            effect_root = Path(target_configuration.stores[management_store_id].endpoint_ref).resolve().parent / "management-effects"
            if effect_root.exists():
                raise FoundationError("recovery_target_exists", "Management effects target already exists")
            temporary_root = effect_root.with_name(f".{effect_root.name}.{uuid4().hex}.restore")
            temporary_root.mkdir(parents=True)
            with zipfile.ZipFile(resource_sources["management-effects"]) as bundle:
                bundle.extractall(temporary_root)
            staged_resources[str(effect_root)] = temporary_root
        for store_id in expected_ids:
            target = targets[store_id]
            os.replace(staged[store_id], target)
            installed.append(target)
        for destination, temporary in staged_resources.items():
            target = Path(destination)
            os.replace(temporary, target)
            installed_resources.append(target)
        receipt = {
            "schema_version": "agent-foundation.recovery-receipt.v2",
            "backup_id": manifest["backup_id"],
            "source_configuration_fingerprint": manifest["source_configuration_fingerprint"],
            "target_configuration_fingerprint": target_configuration.fingerprint,
            "restored_store_ids": expected_ids,
            "restored_sha256": {store_id: _digest(targets[store_id]) for store_id in expected_ids},
            "restored_resource_ids": sorted(resource_sources),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_path = path.parent / f"restore-{target_configuration.configuration_id}.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        receipt["receipt_path"] = str(receipt_path)
        return receipt
    except BaseException:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()
        for target in installed:
            if target.exists():
                target.unlink()
        for temporary in staged_resources.values():
            if temporary.exists():
                shutil.rmtree(temporary)
        for target in installed_resources:
            if target.exists():
                shutil.rmtree(target)
        raise

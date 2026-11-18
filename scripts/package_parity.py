"""Audit wheel contents against explicit source bytes, independently of building."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
import zipfile


class PackageParityError(ValueError):
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        super().__init__(json.dumps(report, sort_keys=True))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def distribution_directory(name: str, version: str) -> str:
    return f"{re.sub(r'[-_.]+', '_', name)}-{version.replace('-', '_')}.dist-info"


def audit_wheel(
    wheel: Path, expected_sources: Mapping[str, bytes], *, project_name: str, version: str,
) -> dict[str, object]:
    """Check every source byte and RECORD; generated metadata is separate.

    Allowed metadata is setuptools' output for this project's build contract,
    not source files expected under backend/. Unknown members fail the audit.
    """
    if not expected_sources:
        raise ValueError("Explicit source package inputs must not be empty")
    dist_info = distribution_directory(project_name, version)
    required_metadata = {f"{dist_info}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
    optional_metadata = {f"{dist_info}/{name}" for name in ("entry_points.txt", "top_level.txt")}
    allowed = set(expected_sources) | required_metadata | optional_metadata
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        counts = Counter(names)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        members = set(names)
        # Reject ambiguity before using ZipFile.read's last-member behavior.
        payloads = {name: archive.read(name) for name in members if counts[name] == 1}
    unsafe = sorted(
        name for name in names
        if "\\" in name or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts or name.endswith("/")
    )
    missing = sorted((set(expected_sources) | required_metadata) - members)
    extra = sorted(members - allowed)
    mismatches = sorted(
        name for name, data in expected_sources.items()
        if name in payloads and payloads[name] != data
    )
    metadata_errors: list[str] = []
    metadata_name = f"{dist_info}/METADATA"
    if metadata_name in payloads:
        metadata = BytesParser().parsebytes(payloads[metadata_name])
        for field, expected in (("Name", project_name), ("Version", version)):
            if metadata.get_all(field) != [expected]:
                metadata_errors.append(f"METADATA {field} does not match pyproject.toml")
    record_name = f"{dist_info}/RECORD"
    record_errors: list[str] = []
    if record_name in payloads:
        try:
            rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"))))
            record_paths = [row[0] for row in rows if len(row) == 3]
            if len(record_paths) != len(rows):
                record_errors.append("RECORD contains malformed rows")
            if len(set(record_paths)) != len(record_paths):
                record_errors.append("RECORD contains duplicate paths")
            if set(record_paths) != members:
                record_errors.append("RECORD member set differs from wheel")
            for row in rows:
                if len(row) != 3:
                    continue
                name, recorded_hash, recorded_size = row
                if name == record_name:
                    if recorded_hash or recorded_size:
                        record_errors.append("RECORD must not hash itself")
                    continue
                if name not in payloads:
                    continue
                data = payloads[name]
                expected_hash = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()
                ).rstrip(b"=").decode("ascii")
                if recorded_hash != expected_hash or recorded_size != str(len(data)):
                    record_errors.append(f"RECORD hash/size mismatch: {name}")
        except (UnicodeDecodeError, csv.Error) as error:
            record_errors.append(f"Invalid RECORD: {error}")
    failures = (duplicates, unsafe, missing, extra, mismatches, metadata_errors, record_errors)
    return {
        "schema_version": "agent-foundation.package-parity.v1",
        "status": "failed" if any(failures) else "verified",
        "wheel": wheel.name, "sha256": sha256(wheel.read_bytes()),
        "project_name": project_name, "version": version,
        "source_identities": {name: sha256(data) for name, data in sorted(expected_sources.items())},
        "source_members": len(expected_sources),
        "verified_source_members": sum(payloads.get(name) == data for name, data in expected_sources.items()),
        "members": sorted(members),
        "metadata_members": sorted(members & (required_metadata | optional_metadata)),
        "duplicate_members": duplicates, "unsafe_members": unsafe,
        "missing_members": missing, "unexpected_members": extra,
        "mismatched_members": mismatches,
        "metadata_errors": metadata_errors, "record_errors": record_errors,
    }


def verify_wheel(wheel: Path, expected_sources: Mapping[str, bytes], **identity: str) -> dict[str, object]:
    report = audit_wheel(wheel, expected_sources, **identity)
    if report["status"] != "verified":
        raise PackageParityError(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--source-input", type=Path, required=True,
                        help="Explicit clean build input containing backend/ and pyproject.toml")
    args = parser.parse_args()
    import tomllib

    root = args.source_input.resolve()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = {
        path.relative_to(root / "backend").as_posix(): path.read_bytes()
        for package in ("agent_foundation", "agent_foundation_contracts")
        for path in (root / "backend" / package).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if not expected:
        parser.error("The explicit source input has no backend package members")
    report = audit_wheel(args.wheel, expected, project_name=project["name"], version=project["version"])
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())

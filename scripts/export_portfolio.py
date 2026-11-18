"""Export a reviewable source snapshot without history, credentials or runtime data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TREES = (
    "backend/agent_foundation", "backend/agent_foundation_contracts", "public_contracts",
    "sdk/foundation-python", "sdk/typescript", "examples/portfolio", "docs/portfolio",
)
FILES = ("README.md", "pyproject.toml", ".gitignore", ".gitattributes", "docs/index.html", "docs/.nojekyll", "scripts/export_portfolio.py",
         "scripts/build_clean_service.py", "scripts/candidate_files.py", "scripts/package_parity.py")
TESTS = (
    "test_architecture_pl01.py", "test_architecture_pl02_evidence.py",
    "test_architecture_pl03_memory.py", "test_architecture_pl04_graph.py",
    "test_architecture_pl05_context.py", "test_architecture_pl06_jobs.py",
    "test_architecture_pl07_management.py", "test_architecture_pl09_assembly_matrix.py",
    "test_architecture_pl09_storage_adapters.py", "test_architecture_pl14_graph_authority.py",
    "test_architecture_pl14_graph_temporal.py", "test_architecture_pl14_extractors.py",
    "test_architecture_pl14_postgres_authorities.py",
)
EXCLUDED = {"__pycache__", "node_modules", "dist", "build", ".pytest_cache", ".ruff_cache"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT):
        raise ValueError("Export outside the source worktree")
    paths = {ROOT / name for name in FILES}
    for directory in TREES:
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file() or set(path.relative_to(ROOT).parts) & EXCLUDED:
                continue
            if path.name.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".whl", ".tgz")):
                continue
            if ".egg-info" in str(path) or path.name.startswith(".env"):
                continue
            paths.add(path)
    for name in TESTS:
        path = ROOT / "backend/tests" / name
        if not path.is_file():
            raise FileNotFoundError(path)
        paths.add(path)
    if any(path.is_symlink() or not path.resolve().is_relative_to(ROOT) for path in paths):
        raise ValueError("Source path escapes the approved worktree")
    destination.mkdir(parents=True, exist_ok=False)
    identities = []
    for source in sorted(paths):
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        identities.append({"path": relative.as_posix(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    manifest = {"schema_version": "agent-foundation.portfolio-snapshot.v1", "service_version": "1.0.0a32",
                "scope": "Service, contracts, both SDKs, public domain tests, demo and portable atlas; no private execution archive or Git history",
                "files": identities}
    (destination / "SNAPSHOT.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(destination), "files": len(identities)}))


if __name__ == "__main__":
    main()

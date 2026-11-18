"""Build and audit two wheels from independent, explicit clean source inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import tomllib
from typing import Callable, Iterable

from candidate_files import SOURCE_FILES
from package_parity import verify_wheel


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK_ROOT = Path("H:/CodexTemp/agent-foundation-pl13-build")
SOURCE_DATE_EPOCH = "1789390991"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_input(root: Path, relative: str) -> bytes:
    path = root / relative
    if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        raise ValueError(f"Build input must be relative to source root: {relative}")
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError(f"Build input escapes source root: {relative}")
    return path.read_bytes()


def collect_build_inputs(
    root: Path, source_files: Iterable[str] = SOURCE_FILES,
) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, str]]:
    """Freeze explicit inputs and reject undeclared service or contract files."""
    root = root.resolve()
    pyproject = _read_input(root, "pyproject.toml")
    project = tomllib.loads(pyproject.decode("utf-8"))["project"]
    identity = {key: project[key] for key in ("name", "version")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise ValueError("pyproject.toml must declare a static project name and version")
    declared = tuple(source_files)
    if len(set(declared)) != len(declared):
        raise ValueError("SOURCE_FILES contains duplicate inputs")
    service_root = root / "backend/agent_foundation"
    actual = {
        path.relative_to(service_root).as_posix()
        for path in service_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual != set(declared):
        raise ValueError(f"Service input closure differs: missing={sorted(set(declared) - actual)}, "
                         f"undeclared={sorted(actual - set(declared))}")
    sources = {
        f"agent_foundation/{name}": _read_input(root, f"backend/agent_foundation/{name}")
        for name in declared
    }
    contracts_root = root / "public_contracts"
    contract_names = {
        path.relative_to(contracts_root).as_posix()
        for path in contracts_root.rglob("*.json") if path.is_file()
    }
    if not contract_names:
        raise ValueError("Canonical public contracts are missing")
    mirror_root = root / "backend/agent_foundation_contracts"
    mirrored = {
        path.relative_to(mirror_root).as_posix()
        for path in mirror_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if mirrored != contract_names | {"__init__.py"}:
        raise ValueError("Installed contract mirror member set differs from canonical contracts")
    inputs = {"pyproject.toml": pyproject}
    readme = project.get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    if isinstance(readme, str):
        inputs[readme] = _read_input(root, readme)
    for name in sorted(contract_names):
        canonical = _read_input(root, f"public_contracts/{name}")
        mirror = _read_input(root, f"backend/agent_foundation_contracts/{name}")
        if mirror != canonical:
            raise ValueError(f"Contract mirror bytes differ: {name}")
        sources[f"agent_foundation_contracts/{name}"] = canonical
        inputs[f"public_contracts/{name}"] = canonical
    sources["agent_foundation_contracts/__init__.py"] = _read_input(
        root, "backend/agent_foundation_contracts/__init__.py"
    )
    inputs.update({f"backend/{name}": content for name, content in sources.items()})
    return inputs, sources, identity


def _write_inputs(directory: Path, inputs: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    for relative, content in inputs.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def run_wheel_build(source_input: Path, output: Path, environment: dict[str, str]) -> None:
    """Use the caller's H: build runtime with no cache or source-path injection."""
    validate_build_runtime(source_input)
    subprocess.run([
        sys.executable, "-I", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
        "--no-cache-dir", "--wheel-dir", str(output), str(source_input),
    ], cwd=source_input, env=environment, check=True)


def validate_build_runtime(source_input: Path) -> None:
    """--no-build-isolation must not bypass the project's build requirements."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement

    configuration = tomllib.loads((source_input / "pyproject.toml").read_text(encoding="utf-8"))
    for text in configuration["build-system"]["requires"]:
        requirement = Requirement(text)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError as error:
            raise ValueError(f"Build runtime requires {requirement}; package is missing") from error
        if installed not in requirement.specifier:
            raise ValueError(f"Build runtime requires {requirement}; installed version is {installed}")


def build_candidate(
    root: Path,
    output: Path,
    *,
    task_root: Path = DEFAULT_TASK_ROOT,
    source_files: Iterable[str] = SOURCE_FILES,
    runner: Callable[[Path, Path, dict[str, str]], None] = run_wheel_build,
) -> dict[str, object]:
    """Retain both clean inputs/builds; never remove or overwrite old evidence."""
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output must be new or empty; existing evidence is preserved: {output}")
    inputs, sources, identity = collect_build_inputs(root, source_files)
    task_root = task_root.resolve()
    task_root.mkdir(parents=True, exist_ok=True)
    task_directory = Path(tempfile.mkdtemp(prefix="build-", dir=task_root))
    input_hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in sorted(inputs.items())
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(input_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    builds: list[dict[str, object]] = []
    wheels: list[Path] = []
    for index in (1, 2):
        build_directory = task_directory / f"build-{index}"
        source_input = build_directory / "source"
        _write_inputs(source_input, inputs)
        wheel_output = build_directory / "wheels"
        wheel_output.mkdir()
        temporary = build_directory / "tmp"
        temporary.mkdir()
        cache = build_directory / "cache"
        cache.mkdir()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.update({
            "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary),
            "PIP_CACHE_DIR": str(cache), "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1", "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
        })
        runner(source_input, wheel_output, environment)
        generated = tuple(wheel_output.glob("*.whl"))
        if len(generated) != 1:
            raise ValueError(f"Expected exactly one wheel from clean input, found {len(generated)}")
        wheel = generated[0]
        parity = verify_wheel(wheel, sources, project_name=identity["name"], version=identity["version"])
        changed = [name for name, content in inputs.items() if (source_input / name).read_bytes() != content]
        if changed:
            raise ValueError(f"Build backend changed explicit inputs: {changed}")
        builds.append({"source_input": str(source_input), "wheel_path": str(wheel), "parity": parity})
        wheels.append(wheel)
    if wheels[0].read_bytes() != wheels[1].read_bytes():
        raise ValueError(f"Independent clean builds differ; retained evidence: {task_directory}")
    result = {
        "schema_version": "agent-foundation.clean-build.v2",
        "status": "verified",
        **identity,
        "wheel": wheels[0].name,
        "sha256": digest(wheels[0]),
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "source_input_fingerprint": input_fingerprint,
        "source_input_identities": input_hashes,
        "source_identities": builds[0]["parity"]["source_identities"],
        "members": builds[0]["parity"]["members"],
        "missing_members": [], "unexpected_package_members": [], "mismatched_members": [],
        "duplicate_members": [], "unexpected_members": [], "record_errors": [], "metadata_errors": [],
        "build_runtime": {
            "python": sys.version, "executable": sys.executable,
            "packages": {name: importlib.metadata.version(name) for name in ("pip", "setuptools", "wheel")},
        },
        "builds": builds,
        "reproducible": True,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / wheels[0].name).open("xb") as handle:
        handle.write(wheels[0].read_bytes())
    with (output / "build.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    args = parser.parse_args()
    result = build_candidate(ROOT, args.output, task_root=args.task_root)
    print(json.dumps({key: result[key] for key in ("status", "wheel", "sha256", "reproducible")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

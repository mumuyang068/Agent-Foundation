from __future__ import annotations

import argparse
import os
from pathlib import Path

from pydantic import ValidationError

from agent_foundation.adapters.inbound.access_http import create_access_app
from agent_foundation.adapters.inbound.evidence_http import add_evidence_routes
from agent_foundation.adapters.inbound.graph_http import add_graph_routes
from agent_foundation.adapters.inbound.memory_http import add_memory_routes
from agent_foundation.adapters.inbound.context_http import add_context_routes
from agent_foundation.adapters.inbound.jobs_http import add_jobs_routes
from agent_foundation.adapters.inbound.applications_http import add_applications_routes
from agent_foundation.adapters.inbound.management_http import add_management_routes
from agent_foundation.kernel.errors import FoundationError

from .configuration import HostConfiguration
from .minimal import (
    create_access_host,
    create_context_host,
    create_evidence_host,
    create_graph_host,
    create_memory_host,
    create_workflow_host,
    create_management_host,
)
from .backup_v2 import create_sqlite_backup, restore_sqlite_backup


def build_app(configuration_path: str | Path, *, bootstrap_token: str):
    configuration = HostConfiguration.model_validate_json(Path(configuration_path).read_text(encoding="utf-8"))
    if "management" in configuration.enabled_modules:
        host = create_management_host(configuration)
    elif {"jobs", "applications"} & set(configuration.enabled_modules):
        host = create_workflow_host(configuration)
    elif "context" in configuration.enabled_modules:
        host = create_context_host(configuration)
    elif "graph" in configuration.enabled_modules:
        host = create_graph_host(configuration)
    elif "memory" in configuration.enabled_modules:
        host = create_memory_host(configuration)
    elif "evidence" in configuration.enabled_modules:
        host = create_evidence_host(configuration)
    else:
        host = create_access_host(configuration)
    app = create_access_app(host.access, bootstrap_token=bootstrap_token)
    if "evidence" in configuration.enabled_modules:
        add_evidence_routes(app, host.evidence)
    if "memory" in configuration.enabled_modules:
        add_memory_routes(app, host.memory)
    if "graph" in configuration.enabled_modules:
        add_graph_routes(app, host.graph)
    if "context" in configuration.enabled_modules:
        add_context_routes(app, host.context)
    if "jobs" in configuration.enabled_modules:
        add_jobs_routes(app, host.jobs)
    if "applications" in configuration.enabled_modules:
        add_applications_routes(app, host.applications)
    if "management" in configuration.enabled_modules:
        def restore(manifest, target):
            try:
                target_configuration = HostConfiguration.model_validate(target)
            except ValidationError as error:
                raise FoundationError(
                    "recovery_configuration_invalid",
                    "Restore target configuration is invalid",
                    details={"errors": error.errors(include_url=False)},
                ) from error
            return restore_sqlite_backup(manifest, target_configuration)

        add_management_routes(
            app,
            host.management,
            backup=lambda destination: create_sqlite_backup(configuration, destination),
            restore=restore,
        )

    @app.on_event("shutdown")
    def close_host() -> None:
        host.close()

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the independent Agent Foundation service")
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    bootstrap_token = os.environ.get("AGENT_FOUNDATION_BOOTSTRAP_TOKEN")
    if not bootstrap_token:
        raise FoundationError(
            "bootstrap_credential_missing",
            "AGENT_FOUNDATION_BOOTSTRAP_TOKEN is required",
        )
    uvicorn.run(build_app(args.configuration, bootstrap_token=bootstrap_token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

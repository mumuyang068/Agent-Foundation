"""Create a new SQLite-only demo directory; never reuse existing data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    root = args.data.resolve()
    root.mkdir(parents=True, exist_ok=False)
    modules = ("access", "evidence", "graph")
    config = {
        "configuration_id": "portfolio-graph-evidence",
        "version": 1,
        "enabled_modules": list(modules),
        "stores": {
            name: {"store_id": name, "driver": "sqlite", "endpoint_ref": str(root / (name + ".sqlite3"))}
            for name in (*modules, "graph-write")
        },
        "module_stores": {name: name for name in modules},
        "module_write_stores": {"graph": "graph-write"},
    }
    (root / "host.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Configuration: {root / 'host.json'}")
    print("Enabled: Access, Evidence, Graph; Memory and Context are disabled.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate the runtime-owned, least-privilege Claude MCP config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_SERVER = "portfolio-tracker"


def validate(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"nie można odczytać JSON {path}: {exc}"

    if not isinstance(payload, dict):
        return "root JSON musi być obiektem"

    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return "brak obiektu mcpServers"
    if set(servers) != {REQUIRED_SERVER}:
        names = ", ".join(sorted(str(name) for name in servers)) or "brak"
        return f"mcpServers musi zawierać wyłącznie {REQUIRED_SERVER}; znaleziono: {names}"

    server = servers[REQUIRED_SERVER]
    if not isinstance(server, dict):
        return f"definicja {REQUIRED_SERVER} musi być obiektem"
    if not isinstance(server.get("command"), str) or not server["command"]:
        return f"{REQUIRED_SERVER}.command musi być niepustym stringiem"
    args = server.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        return f"{REQUIRED_SERVER}.args musi być niepustą listą stringów"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("użycie: validate_agent_krypto_mcp_config.py CONFIG.json", file=sys.stderr)
        return 2
    error = validate(Path(sys.argv[1]))
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


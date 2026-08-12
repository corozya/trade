#!/usr/bin/env python3
"""Validate source and installed least-privilege Codex profiles."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_SERVER = "portfolio-tracker"
REQUIRED_TOOLS = {"get_mandate", "get_portfolio", "execute_trade", "log_round"}


def _mcp_sections(text: str) -> dict[str, dict[str, object]]:
    """Parse the small TOML subset emitted for Codex MCP server sections."""
    servers: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        section = re.fullmatch(r"\[mcp_servers\.([A-Za-z0-9_-]+)\]", line)
        if section:
            current = servers.setdefault(section.group(1), {})
            continue
        if line.startswith("["):
            current = None
            continue
        if current is None or not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if key not in {"command", "args", "enabled", "enabled_tools", "default_tools_approval_mode"}:
            continue
        try:
            current[key] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"niepoprawna wartość {key}: {exc}") from exc
    return servers


def validate(path: Path, runtime_approval_mode: str | None = None) -> str | None:
    try:
        servers = _mcp_sections(path.read_text())
    except (OSError, ValueError) as exc:
        return f"nie można odczytać TOML {path}: {exc}"
    if not servers:
        return "brak tabeli mcp_servers"

    portfolio = servers.get(REQUIRED_SERVER)
    if not isinstance(portfolio, dict):
        return f"brak mcp_servers.{REQUIRED_SERVER}"
    if portfolio.get("enabled") is False:
        return f"mcp_servers.{REQUIRED_SERVER} jest wyłączony"
    if not isinstance(portfolio.get("command"), str) or not portfolio["command"]:
        return f"mcp_servers.{REQUIRED_SERVER}.command musi być niepustym stringiem"
    args = portfolio.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        return f"mcp_servers.{REQUIRED_SERVER}.args musi być niepustą listą stringów"
    tools = portfolio.get("enabled_tools")
    if not isinstance(tools, list) or set(tools) != REQUIRED_TOOLS or len(tools) != len(REQUIRED_TOOLS):
        return (
            f"mcp_servers.{REQUIRED_SERVER}.enabled_tools musi zawierać dokładnie: "
            + ", ".join(sorted(REQUIRED_TOOLS))
        )
    effective_approval = portfolio.get("default_tools_approval_mode", runtime_approval_mode)
    if effective_approval != "approve":
        return f"mcp_servers.{REQUIRED_SERVER} wymaga default_tools_approval_mode=approve"

    enabled_extras = sorted(
        name
        for name, entry in servers.items()
        if name != REQUIRED_SERVER
        and isinstance(entry, dict)
        and entry.get("enabled") is not False
    )
    if enabled_extras:
        return "inne MCP muszą być wyłączone; aktywne: " + ", ".join(enabled_extras)
    return None


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "użycie: validate_agent_krypto_codex_profile.py SOURCE_PROFILE INSTALLED_PROFILE RUNTIME_APPROVAL_MODE",
            file=sys.stderr,
        )
        return 2
    for raw_path in sys.argv[1:3]:
        error = validate(Path(raw_path), runtime_approval_mode=sys.argv[3])
        if error:
            print(error, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


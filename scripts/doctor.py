#!/usr/bin/env python3
"""Non-destructive doctor checks for the imsg Codex plugin."""

from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MCP_PATH = ROOT / "scripts" / "imsg_mcp.py"


def load_mcp() -> Any:
    spec = importlib.util.spec_from_file_location("imsg_mcp", MCP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MCP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contains_schema_composition(value: Any) -> bool:
    if isinstance(value, dict):
        if any(key in value for key in ("anyOf", "oneOf", "allOf", "not")):
            return True
        return any(contains_schema_composition(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_schema_composition(item) for item in value)
    return False


def main() -> int:
    checks: dict[str, Any] = {
        "python": {
            "version": platform.python_version(),
            "ok": sys.version_info >= (3, 10),
        },
        "files": {
            "plugin_json": (ROOT / ".codex-plugin" / "plugin.json").is_file(),
            "mcp_json": (ROOT / ".mcp.json").is_file(),
            "mcp_server": MCP_PATH.is_file(),
            "run_mcp": (ROOT / "scripts" / "run_mcp.sh").is_file(),
            "marketplace_json": (ROOT / ".agents" / "plugins" / "marketplace.json").is_file(),
        },
    }
    ok = checks["python"]["ok"] and all(checks["files"].values())
    try:
        mcp = load_mcp()
        state = mcp.get_state({"include_cli_status": False})
        tool_schemas = mcp.tool_definitions()
        schema_ok = not contains_schema_composition(tool_schemas)
        checks["imsg"] = state["imsg_command"]
        checks["messages_db"] = state["messages_db"]
        checks["tool_count"] = len(tool_schemas)
        checks["schema_compatible"] = schema_ok
        ok = ok and schema_ok and state["imsg_command"]["available"]
    except Exception as exc:
        checks["mcp_error"] = f"{type(exc).__name__}: {exc}"
        ok = False
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

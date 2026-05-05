#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

candidates=()
if [[ -n "${IMSG_MCP_PYTHON:-}" ]]; then
  candidates+=("$IMSG_MCP_PYTHON")
fi
candidates+=(python3.12 python3.11 python3.10 python3)

for candidate in "${candidates[@]}"; do
  if ! command -v "$candidate" >/dev/null 2>&1; then
    continue
  fi
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    exec "$candidate" "$ROOT/doctor.py" "$@"
  fi
done

echo "imsg Codex plugin doctor requires Python 3.10+. Install Python 3.10+ or set IMSG_MCP_PYTHON=/path/to/python." >&2
exit 1

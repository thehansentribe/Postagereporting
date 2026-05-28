#!/usr/bin/env bash
# Quick smoke check: app imports and a fast pytest subset.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -z "${PYTHON:-}" ]] && [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python}"
fi
echo "Using: $($PYTHON --version 2>&1)"
$PYTHON -c "import app; print('import app: OK')"
$PYTHON -m pytest tests/test_exports.py -q
echo "check_env.sh: OK"

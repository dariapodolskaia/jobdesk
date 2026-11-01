#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Сначала выполните: bash setup.command"
  exit 1
fi
if command -v open >/dev/null 2>&1; then
  (sleep 2; open http://localhost:8765) &
fi
exec .venv/bin/python server.py --port 8765

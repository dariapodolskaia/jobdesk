#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo 'Сначала bash setup.command'; exit 1; fi
.venv/bin/python -m pip install 'playwright==1.56.0'
.venv/bin/python -m playwright install chromium
printf '\nБраузер готов. Перезапустите JobDesk.\n'

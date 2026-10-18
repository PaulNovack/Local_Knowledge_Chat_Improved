#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
mkdir -p logs
printf 'Opening local app at http://127.0.0.1:8000\n'
exec .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --ws-max-size 32768 2>&1 | tee -a logs/backend.log

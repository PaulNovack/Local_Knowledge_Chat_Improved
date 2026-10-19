#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt -r asr-service/requirements.txt
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
mkdir -p logs
start_local_asr=${START_LOCAL_ASR:-1}
asr_pid=""
if [[ "$start_local_asr" == "1" ]]; then
  printf 'Starting local ASR service on http://127.0.0.1:9000\n'
  .venv/bin/python -m uvicorn --app-dir asr-service server:app --host 127.0.0.1 --port 9000 >> logs/asr-service.log 2>&1 &
  asr_pid=$!
else
  printf 'Skipping local ASR service because START_LOCAL_ASR=0\n'
fi
printf 'Starting Piper service with Docker Compose on http://127.0.0.1:8001\n'
docker compose up -d piper-service >> logs/piper-service.log 2>&1
cleanup() {
  kill "$backend_pid" 2>/dev/null || true
  if [[ -n "$asr_pid" ]]; then
    kill "$asr_pid" 2>/dev/null || true
    wait "$asr_pid" 2>/dev/null || true
  fi
  wait "$backend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
printf 'Opening local app at http://127.0.0.1:8000\n'
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --ws-max-size 32768 >> logs/backend.log 2>&1 &
backend_pid=$!
if [[ -n "$asr_pid" ]]; then
  wait -n "$backend_pid" "$asr_pid"
else
  wait "$backend_pid"
fi
exit_code=$?
cleanup
exit "$exit_code"

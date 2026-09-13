#!/usr/bin/env bash
# Runs the host-side speech-to-text service (speech-service/) so the chat
# tab's mic button works. Not Dockerized, same reason Ollama isn't: GPU
# access without fighting Docker GPU passthrough. Start this alongside
# `docker compose up`, same as you already do with Ollama.
#
# First time: pip install -r speech-service/requirements.txt into whatever
# Python environment $PYTHON_BIN below resolves to (a venv is recommended
# but not required). speech-service/smoke_test.py is worth running once
# too, to check GPU vs CPU support on this machine.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
service_dir="$repo_root/speech-service"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "$PYTHON_BIN not found on PATH. Install Python 3, then: pip install -r speech-service/requirements.txt" >&2
  exit 1
fi

cd "$service_dir"

# Must bind 0.0.0.0, not 127.0.0.1/localhost — the backend container reaches
# this via host.docker.internal (wired up on Linux too, via the extra_hosts:
# host-gateway entry in docker-compose.yml), not loopback.
echo "==> Starting speech service on 0.0.0.0:8090 (reachable from Docker as host.docker.internal:8090)"
exec "$PYTHON_BIN" -m uvicorn server:app --host 0.0.0.0 --port 8090

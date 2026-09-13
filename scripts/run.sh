#!/usr/bin/env bash
# One-shot setup + launch for this app: checks the required host-side
# dependencies (Docker, Ollama), makes sure .env and the HTTPS cert exist,
# optionally starts the (optional) voice-input speech service in the
# background, then builds and starts the docker compose stack.
#
# Safe to re-run any time — every step here is a no-op (or close to it) if
# it already happened on a previous run. Pass -f/--force to rotate the
# HTTPS cert even if a valid one already exists.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

FORCE_CERT=0
for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE_CERT=1 ;;
  esac
done

step() {
  echo ""
  echo "==> $1"
}

fail() {
  echo ""
  echo "ERROR: $1" >&2
}

env_value() {
  local name="$1" default="$2" value=""
  if [ -f .env ]; then
    value="$(grep -E "^${name}=" .env | tail -n1 | cut -d= -f2- | tr -d '\r' | xargs || true)"
  fi
  echo "${value:-$default}"
}

# ---------------------------------------------------------------------------
# 1. Docker

step "Checking for Docker"
if ! command -v docker >/dev/null 2>&1; then
  fail "Docker not found on PATH. Install Docker Engine + compose plugin: https://docs.docker.com/get-docker/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  fail "Docker is installed but not responding — is the Docker daemon running?"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  fail "The 'docker compose' plugin isn't available: https://docs.docker.com/compose/install/"
  exit 1
fi
echo "Docker OK."

# ---------------------------------------------------------------------------
# 2. Ollama

step "Checking for Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  fail "Ollama not found on PATH. Install it (needed for the LLMs this app calls out to): https://ollama.com/download — then ./scripts/pull-models.sh"
  exit 1
fi
echo "Ollama OK."

# ---------------------------------------------------------------------------
# 3. .env

step "Checking for .env"
if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    fail ".env.example is missing — can't create a default .env from it."
    exit 1
  fi
  cp .env.example .env
  echo "Created .env from .env.example (defaults: DOMAIN_NAME=localhost, HTTPS_PORT=443). Edit it any time — re-run this script to pick up changes."
else
  echo ".env already exists, leaving it as-is."
fi

# ---------------------------------------------------------------------------
# 4. HTTPS cert

step "Checking for HTTPS certificate"
cert_args=()
if [ "$FORCE_CERT" -eq 1 ]; then
  cert_args+=(--force)
fi
if ! ./scripts/generate-certs.sh "${cert_args[@]}"; then
  fail "Certificate generation failed (see above) — nginx won't start without it."
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Optional speech service

step "Voice input (speech-to-text)"
echo "The mic button in the chat tab needs speech-service/ running on the host (GPU access, so it isn't Dockerized — see README)."
answer=""
read -r -p "Start it now in the background? [y/N] " answer || true
if [[ "$answer" =~ ^[Yy] ]]; then
  log_out="$repo_root/speech-service/speech-service.log"
  echo "Launching speech service in the background (first run downloads the Whisper model — check $log_out for progress)..."
  nohup "$repo_root/scripts/run-speech-service.sh" >"$log_out" 2>&1 &
  speech_pid=$!
  disown "$speech_pid" 2>/dev/null || true
  echo "Started in the background (PID $speech_pid). Logs: $log_out"
  echo "The mic button enables itself automatically once it's up (usually a few seconds, longer on first run). To stop it later: kill $speech_pid"
else
  echo "Skipped — the mic button will just stay disabled until you run ./scripts/run-speech-service.sh yourself."
fi

# ---------------------------------------------------------------------------
# 6. Build + start

step "Building and starting the app (docker compose up --build -d)"
if ! docker compose up --build -d; then
  fail "docker compose up failed (see above)."
  exit 1
fi

# ---------------------------------------------------------------------------
# Done

domain="$(env_value DOMAIN_NAME localhost)"
https_port="$(env_value HTTPS_PORT 443)"
if [ "$https_port" = "443" ]; then
  url="https://$domain/"
else
  url="https://$domain:$https_port/"
fi

echo ""
echo "==> Up and running"
echo "App:              $url"
echo "Backend API docs: http://localhost:8000/docs"
echo "(Self-signed cert — your browser will warn once until you trust it.)"

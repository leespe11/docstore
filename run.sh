#!/usr/bin/env bash
# One-shot setup + launch for this app: checks the required host-side
# dependencies (Docker, Ollama), requires .env to already exist, pulls
# whichever configured Ollama models aren't installed yet, generates the
# HTTPS cert if needed, optionally starts (and/or registers for automatic
# boot-time startup) the optional voice-input speech service, then builds
# and starts the docker compose stack.
#
# Safe to re-run any time — every step here is a no-op (or close to it) if
# it already happened on a previous run.
#
# This one file replaces what used to be scripts/generate-certs.sh,
# scripts/pull-models.sh, and scripts/run-speech-service.sh — their logic
# lives inline below instead of in separate scripts.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")" && pwd)"
cd "$repo_root"

FORCE_CERT=0
SPEECH_SERVICE_ONLY=0
REMOVE_SPEECH_STARTUP=0
for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE_CERT=1 ;;
    --speech-service-only) SPEECH_SERVICE_ONLY=1 ;;
    --remove-speech-service-startup) REMOVE_SPEECH_STARTUP=1 ;;
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

speech_cron_marker="$repo_root/run.sh --speech-service-only"

# ---------------------------------------------------------------------------
# Internal mode: just run the speech service in the foreground (this is
# what actually ends up backgrounded/cron-started by step 5 below), and do
# nothing else.

if [ "$SPEECH_SERVICE_ONLY" -eq 1 ]; then
  service_dir="$repo_root/speech-service"
  if [ ! -d "$service_dir" ]; then
    fail "speech-service/ not found under $repo_root."
    exit 1
  fi

  PYTHON_BIN="${PYTHON_BIN:-python3}"
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    fail "$PYTHON_BIN not found on PATH. Install Python 3, then: pip install -r speech-service/requirements.txt"
    exit 1
  fi

  # Must bind 0.0.0.0, not 127.0.0.1/localhost — the backend container
  # reaches this via host.docker.internal (wired up via the extra_hosts:
  # host-gateway entry in docker-compose.yml), not loopback. --app-dir
  # (rather than cd) keeps this from depending on the caller's directory.
  echo "==> Starting speech service on 0.0.0.0:8090 (reachable from Docker as host.docker.internal:8090)"
  exec "$PYTHON_BIN" -m uvicorn server:app --app-dir "$service_dir" --host 0.0.0.0 --port 8090
fi

# ---------------------------------------------------------------------------
# Internal mode: remove the @reboot crontab entry, then exit.

if [ "$REMOVE_SPEECH_STARTUP" -eq 1 ]; then
  existing_crontab="$(crontab -l 2>/dev/null || true)"
  if grep -Fq "$speech_cron_marker" <<<"$existing_crontab"; then
    grep -Fv "$speech_cron_marker" <<<"$existing_crontab" | crontab -
    echo "Removed the @reboot crontab entry — the speech service will no longer start automatically at boot."
  else
    echo "No matching crontab entry found — nothing to remove."
  fi
  exit 0
fi

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
# 2. .env — required up front (not auto-created): the model-pull step below
# and the HTTPS cert both read settings from it, so a silently-defaulted
# .env would mean pulling the wrong models or minting a cert for the wrong
# domain without you noticing.

step "Checking for .env"
if [ ! -f .env ]; then
  fail ".env not found. Copy .env.example to .env and configure it (at least DOMAIN_NAME, and OLLAMA_BASE_URL if Ollama isn't on the default port), then re-run this script."
  exit 1
fi
echo ".env found."

# ---------------------------------------------------------------------------
# 3. Ollama — installed, and every model this app is configured to use
# (per .env, falling back to backend/app/config.py's defaults) actually
# pulled.

step "Checking for Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  fail "Ollama not found on PATH. Install it (needed for the LLMs this app calls out to): https://ollama.com/download"
  exit 1
fi
echo "Ollama OK."

llm_model="$(env_value OLLAMA_LLM_MODEL qwen2.5:14b)"
vision_model="$(env_value OLLAMA_VISION_MODEL qwen2.5vl:7b)"
embed_model="$(env_value OLLAMA_EMBED_MODEL bge-large)"
extract_model="$(env_value OLLAMA_EXTRACT_MODEL "")"

needed_models=("$llm_model" "$vision_model" "$embed_model")
if [ -n "$extract_model" ] && [ "$extract_model" != "$llm_model" ]; then
  needed_models+=("$extract_model")
fi

# `ollama list` shows an explicit :latest tag even for models pulled
# without one (e.g. "bge-large" pulls as "bge-large:latest") - strip that
# suffix on both sides before comparing so an untagged config value still
# matches an already-pulled model.
installed_models="$(ollama list | tail -n +2 | awk '{print $1}' | sed 's/:latest$//')"

for m in "${needed_models[@]}"; do
  m_norm="${m%:latest}"
  if grep -Fxq "$m_norm" <<<"$installed_models"; then
    echo "$m already installed."
  else
    echo "Pulling $m (not installed yet)..."
    if ! ollama pull "$m"; then
      fail "ollama pull $m failed (see above)."
      exit 1
    fi
  fi
done

# ---------------------------------------------------------------------------
# 4. HTTPS cert — self-signed, generated inside a throwaway Docker
# container (not on the host - nothing extra to install beyond Docker
# itself). Not Let's Encrypt: this app is meant to stay off the public
# internet (see README), so there's no reachable public domain to satisfy
# an ACME challenge against.

step "Checking for HTTPS certificate"
certs_dir="./certs"
cert_file="$certs_dir/fullchain.pem"
key_file="$certs_dir/privkey.pem"
cert_days=825
cert_image="alpine:3"

mkdir -p "$certs_dir"
domain_name="$(env_value DOMAIN_NAME localhost)"

if [ "$FORCE_CERT" -eq 0 ] && [ -s "$cert_file" ] && [ -s "$key_file" ]; then
  echo "Cert already exists: $cert_file (pass --force to regenerate)"
else
  # Docker Desktop on native Windows (Git Bash/MSYS) needs a genuine
  # Windows-style host path for bind mounts (C:/Users/...), not MSYS's
  # internal /c/Users/... form — `pwd -W` (a Git Bash builtin) gives that.
  # Plain `pwd` is already correct on real Linux/Mac, where -W doesn't exist.
  certs_dir_abs="$(cd "$certs_dir" && { pwd -W 2>/dev/null || pwd; })"

  echo "Generating self-signed cert for DOMAIN_NAME=$domain_name (valid $cert_days days) via a throwaway '$cert_image' container..."
  # MSYS_NO_PATHCONV stops Git Bash from "helpfully" mangling the
  # container-side /certs path in -v as if it were a host path — harmless/
  # unused outside Git Bash. The openssl -subj/-addext values live inside
  # the one big sh -c string below, which doesn't start with `/`, so
  # they're never at risk of that same mangling regardless.
  MSYS_NO_PATHCONV=1 docker run --rm -v "${certs_dir_abs}:/certs" "$cert_image" sh -c "
    apk add --no-cache openssl >/dev/null &&
    openssl req -x509 -newkey rsa:2048 -sha256 -days $cert_days -nodes \
      -keyout /certs/privkey.pem -out /certs/fullchain.pem \
      -subj '/CN=$domain_name' \
      -addext 'subjectAltName=DNS:$domain_name,DNS:localhost,IP:127.0.0.1'
  "
  echo "Wrote $cert_file and $key_file"
  echo "Self-signed — your browser will warn until you trust it manually (import $cert_file, or click through the warning)."
fi

# ---------------------------------------------------------------------------
# 5. Optional speech service

step "Voice input (speech-to-text)"
echo "The mic button in the chat tab needs the speech service running on the host (GPU access, so it isn't Dockerized — see README)."
echo "  [N] No (default) — the mic button stays disabled"
echo "  [Y] Yes, start it now (background, until you reboot/log out)"
echo "  [B] Yes, start it now AND register it to start automatically at boot (crontab @reboot)"
answer=""
read -r -p "Choice [N/y/b] " answer || true

start_now=0
register_at_boot=0
if [[ "$answer" =~ ^[Yy] ]] || [[ "$answer" =~ ^[Bb] ]]; then
  start_now=1
fi
if [[ "$answer" =~ ^[Bb] ]]; then
  register_at_boot=1
fi

if [ "$start_now" -eq 1 ]; then
  log_out="$repo_root/speech-service/speech-service.log"
  echo "Launching speech service in the background (first run downloads the Whisper model — check $log_out for progress)..."
  nohup "$repo_root/run.sh" --speech-service-only >"$log_out" 2>&1 &
  speech_pid=$!
  disown "$speech_pid" 2>/dev/null || true
  echo "Started in the background (PID $speech_pid). Logs: $log_out"
  echo "The mic button enables itself automatically once it's up (usually a few seconds, longer on first run). To stop it: kill $speech_pid"
fi

if [ "$register_at_boot" -eq 1 ]; then
  if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab not found on this system — can't register a @reboot entry (the service is still running now, just not across a reboot)."
  else
    existing_crontab="$(crontab -l 2>/dev/null || true)"
    if grep -Fq "$speech_cron_marker" <<<"$existing_crontab"; then
      echo "Already registered in crontab — leaving it as-is."
    else
      cron_line="@reboot $speech_cron_marker >> $repo_root/speech-service/speech-service.log 2>&1"
      { [ -n "$existing_crontab" ] && echo "$existing_crontab"; echo "$cron_line"; } | crontab -
      echo "Registered a crontab @reboot entry — the speech service will start automatically at boot."
    fi
    echo "To remove it later: ./run.sh --remove-speech-service-startup   (or: crontab -e, then delete the @reboot line mentioning run.sh)"
  fi
fi

if [ "$start_now" -eq 0 ]; then
  echo "Skipped — the mic button will just stay disabled until you run ./run.sh --speech-service-only yourself, or re-run this script and choose Y/B."
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

https_port="$(env_value HTTPS_PORT 443)"
if [ "$https_port" = "443" ]; then
  url="https://$domain_name/"
else
  url="https://$domain_name:$https_port/"
fi

echo ""
echo "==> Up and running"
echo "App:              $url"
echo "Backend API docs: http://localhost:8000/docs"
echo "(Self-signed cert — your browser will warn once until you trust it.)"
echo "(restart: unless-stopped is set on every service — the stack comes back up on its own after a reboot or Docker restart, but not the speech service unless you chose B above.)"

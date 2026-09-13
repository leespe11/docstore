#!/usr/bin/env bash
# Generates a self-signed TLS cert/key for the `nginx` reverse-proxy service
# (see docker-compose.yml) and writes them into ./certs/, which is
# bind-mounted read-only into that container. Idempotent — safe to run on
# every `docker compose up`; it skips regeneration whenever both files
# already exist (pass -f/--force to rotate the cert regardless).
#
# Runs openssl inside a throwaway Docker container instead of requiring it
# on the host — this app already requires Docker for everything else, so
# this is one less thing to separately install/find on PATH. The container
# is removed immediately after (--rm); nothing lingers.
#
# Self-signed, not Let's Encrypt: this app is meant to stay off the public
# internet (see README), so there's no reachable public domain to satisfy an
# ACME HTTP-01/TLS-ALPN-01 challenge against. Browsers will show a trust
# warning until you import fullchain.pem yourself.
set -euo pipefail

cd "$(dirname "$0")/.."

CERTS_DIR="./certs"
CERT_FILE="$CERTS_DIR/fullchain.pem"
KEY_FILE="$CERTS_DIR/privkey.pem"
DAYS=825
IMAGE="alpine:3"
FORCE=0

for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE=1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found on PATH — this script generates the cert inside a throwaway container, so Docker is required (same as the rest of this app)." >&2
  exit 1
fi

DOMAIN_NAME="${DOMAIN_NAME:-}"
if [ -z "$DOMAIN_NAME" ] && [ -f .env ]; then
  DOMAIN_NAME="$(grep -E '^DOMAIN_NAME=' .env | tail -n1 | cut -d= -f2- | tr -d '\r' | xargs || true)"
fi
DOMAIN_NAME="${DOMAIN_NAME:-localhost}"

mkdir -p "$CERTS_DIR"

if [ "$FORCE" -eq 0 ] && [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
  echo "Cert already exists: $CERT_FILE (pass --force to regenerate)"
  exit 0
fi

# Docker Desktop on native Windows (Git Bash/MSYS) needs a genuine
# Windows-style host path for bind mounts (C:/Users/...), not MSYS's
# internal /c/Users/... form — `pwd -W` (a Git Bash builtin) gives that.
# Plain `pwd` is already correct on real Linux/Mac, where -W doesn't exist.
CERTS_DIR_ABS="$(cd "$CERTS_DIR" && { pwd -W 2>/dev/null || pwd; })"

echo "Generating self-signed cert for DOMAIN_NAME=$DOMAIN_NAME (valid $DAYS days) via a throwaway '$IMAGE' container..."
# MSYS_NO_PATHCONV stops Git Bash from "helpfully" mangling the container-side
# /certs path in -v as if it were a host path — harmless/unused outside Git
# Bash. The openssl -subj/-addext values live inside the one big sh -c
# string below, which doesn't start with `/`, so they're never at risk of
# that same mangling regardless.
MSYS_NO_PATHCONV=1 docker run --rm -v "${CERTS_DIR_ABS}:/certs" "$IMAGE" sh -c "
  apk add --no-cache openssl >/dev/null &&
  openssl req -x509 -newkey rsa:2048 -sha256 -days $DAYS -nodes \
    -keyout /certs/privkey.pem -out /certs/fullchain.pem \
    -subj '/CN=$DOMAIN_NAME' \
    -addext 'subjectAltName=DNS:$DOMAIN_NAME,DNS:localhost,IP:127.0.0.1'
"

echo "Wrote $CERT_FILE and $KEY_FILE"
echo "Self-signed — your browser will warn until you trust it manually (import $CERT_FILE, or click through the warning)."

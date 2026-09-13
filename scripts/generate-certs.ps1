# Generates a self-signed TLS cert/key for the `nginx` reverse-proxy service
# (see docker-compose.yml) and writes them into ./certs/, which is
# bind-mounted read-only into that container. Idempotent -- safe to run on
# every `docker compose up`; it skips regeneration whenever both files
# already exist (pass -Force to rotate the cert regardless).
#
# Runs openssl inside a throwaway Docker container instead of requiring it
# on the host -- this app already requires Docker for everything else, so
# this is one less thing to separately install/find on PATH. The container
# is removed immediately after (--rm); nothing lingers.
#
# Self-signed, not Let's Encrypt: this app is meant to stay off the public
# internet (see README), so there's no reachable public domain to satisfy an
# ACME HTTP-01/TLS-ALPN-01 challenge against. Browsers will show a trust
# warning until you import fullchain.pem yourself.
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$certsDir = "./certs"
$certFile = Join-Path $certsDir "fullchain.pem"
$keyFile = Join-Path $certsDir "privkey.pem"
$days = 825
$image = "alpine:3"

$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    # Write-Host, not Write-Error: with $ErrorActionPreference = "Stop"
    # (set above), Write-Error is a terminating error that would propagate
    # past this script's own `exit 1` into any caller's call site (e.g.
    # scripts/run.ps1) instead of being reported via a normal exit code.
    Write-Host "docker not found on PATH - this script generates the cert inside a throwaway container, so Docker is required (same as the rest of this app)." -ForegroundColor Red
    exit 1
}

$domainName = $env:DOMAIN_NAME
if (-not $domainName -and (Test-Path ".env")) {
    $line = Get-Content ".env" | Where-Object { $_ -match '^DOMAIN_NAME=' } | Select-Object -Last 1
    if ($line) { $domainName = ($line -split '=', 2)[1].Trim() }
}
if (-not $domainName) { $domainName = "localhost" }

New-Item -ItemType Directory -Force -Path $certsDir | Out-Null

$certExists = (Test-Path $certFile) -and (Get-Item $certFile).Length -gt 0
$keyExists = (Test-Path $keyFile) -and (Get-Item $keyFile).Length -gt 0
if (-not $Force -and $certExists -and $keyExists) {
    Write-Host "Cert already exists: $certFile (pass -Force to regenerate)"
    exit 0
}

$certsDirAbs = (Resolve-Path $certsDir).Path

Write-Host "Generating self-signed cert for DOMAIN_NAME=$domainName (valid $days days) via a throwaway '$image' container..."
$opensslCmd = "apk add --no-cache openssl >/dev/null && openssl req -x509 -newkey rsa:2048 -sha256 -days $days -nodes -keyout /certs/privkey.pem -out /certs/fullchain.pem -subj '/CN=$domainName' -addext 'subjectAltName=DNS:$domainName,DNS:localhost,IP:127.0.0.1'"
docker run --rm -v "${certsDirAbs}:/certs" $image sh -c $opensslCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "Cert generation failed inside the container (see above)." -ForegroundColor Red
    exit 1
}

Write-Host "Wrote $certFile and $keyFile"
Write-Host "Self-signed -- your browser will warn until you trust it manually (import $certFile, or click through the warning)."

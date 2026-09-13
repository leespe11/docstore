#!/usr/bin/env pwsh
# One-shot setup + launch for this app: checks the required host-side
# dependencies (Docker, Ollama), makes sure .env and the HTTPS cert exist,
# optionally starts the (optional) voice-input speech service, then builds
# and starts the docker compose stack.
#
# Safe to re-run any time — every step here is a no-op (or close to it) if
# it already happened on a previous run.
param(
    # Passed through to scripts/generate-certs.ps1 to force-regenerate the
    # HTTPS cert even if a valid one already exists.
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Fail($text) {
    Write-Host ""
    Write-Host "ERROR: $text" -ForegroundColor Red
}

function Get-EnvValue([string]$name, [string]$default) {
    if (Test-Path ".env") {
        $line = Get-Content ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
        if ($line) {
            $value = ($line -split '=', 2)[1].Trim()
            if ($value) { return $value }
        }
    }
    return $default
}

# ---------------------------------------------------------------------------
# 1. Docker

Write-Step "Checking for Docker"
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Fail "Docker not found on PATH. Install Docker Desktop (Windows/Mac) or Docker Engine + compose plugin (Linux): https://docs.docker.com/get-docker/"
    exit 1
}

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker is installed but not responding - is Docker Desktop running?"
    exit 1
}

docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "The 'docker compose' plugin isn't available. Update Docker Desktop, or install the compose plugin: https://docs.docker.com/compose/install/"
    exit 1
}
Write-Host "Docker OK." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. Ollama

Write-Step "Checking for Ollama"
$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollamaCmd) {
    Write-Fail "Ollama not found on PATH. Install it (needed for the LLMs this app calls out to) and pull the models: https://ollama.com/download - then ./scripts/pull-models.ps1"
    exit 1
}
Write-Host "Ollama OK." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. .env

Write-Step "Checking for .env"
if (-not (Test-Path ".env")) {
    if (-not (Test-Path ".env.example")) {
        Write-Fail ".env.example is missing - can't create a default .env from it."
        exit 1
    }
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (defaults: DOMAIN_NAME=localhost, HTTPS_PORT=443). Edit it any time - re-run this script to pick up changes." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists, leaving it as-is." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. HTTPS cert

Write-Step "Checking for HTTPS certificate"
$certArgs = @()
if ($Force) { $certArgs += "-Force" }
& "$repoRoot\scripts\generate-certs.ps1" @certArgs
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Certificate generation failed (see above) - nginx won't start without it."
    exit 1
}

# ---------------------------------------------------------------------------
# 5. Optional speech service

Write-Step "Voice input (speech-to-text)"
Write-Host "The mic button in the chat tab needs speech-service/ running on the host (GPU access, so it isn't Dockerized - see README)."
$answer = ""
try {
    $answer = Read-Host "Start it now in the background? [y/N]"
} catch {
    # Read-Host throws when there's no interactive console attached (e.g. a
    # non-interactive/automated invocation) instead of just returning an
    # empty string - fall back to the same default as an empty answer: no.
    Write-Host "(no interactive console available - skipping this prompt)" -ForegroundColor Yellow
}
if ($answer -match '^[Yy]') {
    # Prefer pwsh (PowerShell 7+) if it's installed, but plain Windows
    # PowerShell (powershell.exe) is present on every Windows machine and
    # works just as well for this - don't require installing pwsh just to
    # use this script.
    $pwshExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
    $logDir = Join-Path $repoRoot "speech-service"
    $logOut = Join-Path $logDir "speech-service.log"
    $logErr = Join-Path $logDir "speech-service.err.log"
    Write-Host "Launching speech service in the background (first run downloads the Whisper model - check $logOut for progress)..." -ForegroundColor Cyan
    $proc = Start-Process $pwshExe -ArgumentList "-File", "$repoRoot\scripts\run-speech-service.ps1" `
        -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
    Write-Host "Started in the background (PID $($proc.Id)). Logs: $logOut" -ForegroundColor Green
    Write-Host "The mic button enables itself automatically once it's up (usually a few seconds, longer on first run). To stop it later: Stop-Process -Id $($proc.Id)" -ForegroundColor Green
} else {
    Write-Host "Skipped - the mic button will just stay disabled until you run ./scripts/run-speech-service.ps1 yourself." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 6. Build + start

Write-Step "Building and starting the app (docker compose up --build -d)"
docker compose up --build -d
if ($LASTEXITCODE -ne 0) {
    Write-Fail "docker compose up failed (see above)."
    exit 1
}

# ---------------------------------------------------------------------------
# Done

$domain = Get-EnvValue "DOMAIN_NAME" "localhost"
$httpsPort = Get-EnvValue "HTTPS_PORT" "443"
$url = if ($httpsPort -eq "443") { "https://$domain/" } else { "https://${domain}:${httpsPort}/" }

Write-Host ""
Write-Host "==> Up and running" -ForegroundColor Green
Write-Host "App:              $url"
Write-Host "Backend API docs: http://localhost:8000/docs"
Write-Host "(Self-signed cert - your browser will warn once until you trust it.)"

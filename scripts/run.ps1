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
# 2. .env — required up front (not auto-created): the model-pull step below
# and the HTTPS cert both read settings from it, so a silently-defaulted
# .env would mean pulling the wrong models or minting a cert for the wrong
# domain without you noticing.

Write-Step "Checking for .env"
if (-not (Test-Path ".env")) {
    Write-Fail ".env not found. Copy .env.example to .env and configure it (at least DOMAIN_NAME, and OLLAMA_BASE_URL if Ollama isn't on the default port), then re-run this script."
    exit 1
}
Write-Host ".env found." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. Ollama — installed, and every model this app is configured to use
# (per .env, falling back to backend/app/config.py's defaults) actually
# pulled. Mirrors scripts/pull-models.ps1 rather than shelling out to it,
# since it needs to skip models that are already present instead of always
# re-pulling.

Write-Step "Checking for Ollama"
$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollamaCmd) {
    Write-Fail "Ollama not found on PATH. Install it (needed for the LLMs this app calls out to): https://ollama.com/download"
    exit 1
}
Write-Host "Ollama OK." -ForegroundColor Green

$llmModel = Get-EnvValue "OLLAMA_LLM_MODEL" "qwen2.5:14b"
$visionModel = Get-EnvValue "OLLAMA_VISION_MODEL" "qwen2.5vl:7b"
$embedModel = Get-EnvValue "OLLAMA_EMBED_MODEL" "bge-large"
$extractModel = Get-EnvValue "OLLAMA_EXTRACT_MODEL" ""

$neededModels = @($llmModel, $visionModel, $embedModel)
if ($extractModel -and $extractModel -ne $llmModel) {
    $neededModels += $extractModel
}

# `ollama list` shows an explicit :latest tag even for models pulled
# without one (e.g. "bge-large" pulls as "bge-large:latest") - strip that
# suffix on both sides before comparing so an untagged config value still
# matches an already-pulled model.
$installedModels = @(ollama list | Select-Object -Skip 1 | ForEach-Object {
    ($_ -split '\s+')[0] -replace ':latest$', ''
})

foreach ($m in $neededModels) {
    $mNorm = $m -replace ':latest$', ''
    if ($installedModels -contains $mNorm) {
        Write-Host "$m already installed." -ForegroundColor Green
    } else {
        Write-Host "Pulling $m (not installed yet)..." -ForegroundColor Cyan
        ollama pull $m
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "ollama pull $m failed (see above)."
            exit 1
        }
    }
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

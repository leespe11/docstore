#!/usr/bin/env pwsh
# One-shot setup + launch for this app: checks the required host-side
# dependencies (Docker, Ollama), requires .env to already exist, pulls
# whichever configured Ollama models aren't installed yet, generates the
# HTTPS cert if needed, optionally starts (and/or registers for automatic
# login-time startup) the optional voice-input speech service, then builds
# and starts the docker compose stack.
#
# Safe to re-run any time — every step here is a no-op (or close to it) if
# it already happened on a previous run.
#
# This one file replaces what used to be scripts/generate-certs.ps1,
# scripts/pull-models.ps1, and scripts/run-speech-service.ps1 — their logic
# lives inline below instead of in separate scripts.
param(
    # Force-regenerate the HTTPS cert even if a valid one already exists.
    [switch]$Force,

    # Internal: used when this script re-invokes itself to actually run the
    # speech service (background launch, and the login-startup scheduled
    # task both call `run.ps1 -SpeechServiceOnly`) instead of the full flow
    # above. Not something you'd normally pass yourself.
    [switch]$SpeechServiceOnly,

    # Unregisters the "start speech service at login" scheduled task
    # created by answering "b" at the voice-input prompt, then exits.
    [switch]$RemoveSpeechServiceStartup
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
Set-Location $repoRoot

$speechTaskName = "DocstoreSpeechService"

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
# Internal mode: just run the speech service in the foreground (this is
# what actually ends up backgrounded/scheduled by step 5 below), and do
# nothing else.

if ($SpeechServiceOnly) {
    $serviceDir = Join-Path $repoRoot "speech-service"
    if (-not (Test-Path $serviceDir)) {
        Write-Fail "speech-service/ not found under $repoRoot."
        exit 1
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        Write-Fail "python not found on PATH. Install Python 3, then: pip install -r speech-service/requirements.txt"
        exit 1
    }

    # Must bind 0.0.0.0, not 127.0.0.1/localhost - Docker Desktop reaches
    # the host through a virtual NAT adapter, not loopback, so the backend
    # container's host.docker.internal:8090 only resolves if this listens
    # on all interfaces. --app-dir (rather than Set-Location) so this
    # doesn't change the calling shell's working directory - `&`-invoked
    # scripts share the caller's PowerShell process, so Set-Location here
    # would otherwise leak out to whatever terminal ran this.
    Write-Host "==> Starting speech service on 0.0.0.0:8090 (reachable from Docker as host.docker.internal:8090)" -ForegroundColor Cyan
    & python -m uvicorn server:app --app-dir $serviceDir --host 0.0.0.0 --port 8090
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Internal mode: remove the login-startup scheduled task, then exit.

if ($RemoveSpeechServiceStartup) {
    $existing = Get-ScheduledTask -TaskName $speechTaskName -ErrorAction SilentlyContinue
    if ($existing) {
        try {
            Unregister-ScheduledTask -TaskName $speechTaskName -Confirm:$false -ErrorAction Stop
            Write-Host "Removed the '$speechTaskName' scheduled task - the speech service will no longer start automatically at login." -ForegroundColor Green
        } catch {
            Write-Fail "Couldn't remove the scheduled task: $($_.Exception.Message)"
            Write-Host "Try running this from an elevated (Run as Administrator) terminal, or delete '$speechTaskName' manually in Task Scheduler." -ForegroundColor Yellow
            exit 1
        }
    } else {
        Write-Host "No '$speechTaskName' scheduled task found - nothing to remove." -ForegroundColor Yellow
    }
    exit 0
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
# 3. Ollama - installed, and every model this app is configured to use (per
# .env, falling back to backend/app/config.py's defaults) actually pulled.

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
# 4. HTTPS cert - self-signed, generated inside a throwaway Docker
# container (not on the host - nothing extra to install beyond Docker
# itself). Not Let's Encrypt: this app is meant to stay off the public
# internet (see README), so there's no reachable public domain to satisfy
# an ACME challenge against.

Write-Step "Checking for HTTPS certificate"
$certsDir = "./certs"
$certFile = Join-Path $certsDir "fullchain.pem"
$keyFile = Join-Path $certsDir "privkey.pem"
$certDays = 825
$certImage = "alpine:3"

New-Item -ItemType Directory -Force -Path $certsDir | Out-Null

$domainName = Get-EnvValue "DOMAIN_NAME" "localhost"
$certExists = (Test-Path $certFile) -and (Get-Item $certFile).Length -gt 0
$keyExists = (Test-Path $keyFile) -and (Get-Item $keyFile).Length -gt 0

if (-not $Force -and $certExists -and $keyExists) {
    Write-Host "Cert already exists: $certFile (pass -Force to regenerate)" -ForegroundColor Green
} else {
    $certsDirAbs = (Resolve-Path $certsDir).Path
    Write-Host "Generating self-signed cert for DOMAIN_NAME=$domainName (valid $certDays days) via a throwaway '$certImage' container..." -ForegroundColor Cyan
    $opensslCmd = "apk add --no-cache openssl >/dev/null && openssl req -x509 -newkey rsa:2048 -sha256 -days $certDays -nodes -keyout /certs/privkey.pem -out /certs/fullchain.pem -subj '/CN=$domainName' -addext 'subjectAltName=DNS:$domainName,DNS:localhost,IP:127.0.0.1'"
    docker run --rm -v "${certsDirAbs}:/certs" $certImage sh -c $opensslCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Cert generation failed inside the container (see above) - nginx won't start without it."
        exit 1
    }
    Write-Host "Wrote $certFile and $keyFile" -ForegroundColor Green
    Write-Host "Self-signed - your browser will warn until you trust it manually (import $certFile, or click through the warning)." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 5. Optional speech service

Write-Step "Voice input (speech-to-text)"
Write-Host "The mic button in the chat tab needs the speech service running on the host (GPU access, so it isn't Dockerized - see README)."
Write-Host "  [N] No (default) - the mic button stays disabled"
Write-Host "  [Y] Yes, start it now (background, until you reboot/log out)"
Write-Host "  [B] Yes, start it now AND register it to start automatically every time you log in"
$answer = ""
try {
    $answer = Read-Host "Choice [N/y/b]"
} catch {
    # Read-Host throws when there's no interactive console attached (e.g. a
    # non-interactive/automated invocation) instead of just returning an
    # empty string - fall back to the same default as an empty answer: no.
    Write-Host "(no interactive console available - skipping this prompt)" -ForegroundColor Yellow
}

$startNow = $answer -match '^[Yy]' -or $answer -match '^[Bb]'
$registerAtLogon = $answer -match '^[Bb]'

if ($startNow) {
    # Prefer pwsh (PowerShell 7+) if it's installed, but plain Windows
    # PowerShell (powershell.exe) is present on every Windows machine and
    # works just as well for this - don't require installing pwsh just to
    # use this script.
    $pwshExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
    $logDir = Join-Path $repoRoot "speech-service"
    $logOut = Join-Path $logDir "speech-service.log"
    $logErr = Join-Path $logDir "speech-service.err.log"
    Write-Host "Launching speech service in the background (first run downloads the Whisper model - check $logOut for progress)..." -ForegroundColor Cyan
    $proc = Start-Process $pwshExe -ArgumentList "-File", "$repoRoot\run.ps1", "-SpeechServiceOnly" `
        -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
    Write-Host "Started in the background (PID $($proc.Id)). Logs: $logOut" -ForegroundColor Green
    Write-Host "The mic button enables itself automatically once it's up (usually a few seconds, longer on first run). To stop it: Stop-Process -Id $($proc.Id)" -ForegroundColor Green
}

if ($registerAtLogon) {
    try {
        $pwshExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
        $action = New-ScheduledTaskAction -Execute $pwshExe -Argument "-WindowStyle Hidden -File `"$repoRoot\run.ps1`" -SpeechServiceOnly"
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $speechTaskName -Action $action -Trigger $trigger -Settings $settings `
            -Description "Starts the docstore speech-to-text service at login (see $repoRoot\run.ps1)" -Force -ErrorAction Stop | Out-Null
        Write-Host "Registered '$speechTaskName' in Task Scheduler - it'll start automatically the next time you log in (not immediately at system boot before login)." -ForegroundColor Green
        Write-Host "To remove it later: ./run.ps1 -RemoveSpeechServiceStartup   (or open Task Scheduler, find '$speechTaskName', and delete it)" -ForegroundColor Green
    } catch {
        Write-Fail "Couldn't register the Task Scheduler entry: $($_.Exception.Message)"
        Write-Host "This usually means local policy restricts creating scheduled tasks - try running this script from an elevated (Run as Administrator) terminal, or register it yourself in Task Scheduler pointing at: $pwshExe -WindowStyle Hidden -File `"$repoRoot\run.ps1`" -SpeechServiceOnly (trigger: at log on)." -ForegroundColor Yellow
        Write-Host "The speech service is still running now (started above) - this only affects whether it restarts automatically next time you log in." -ForegroundColor Yellow
    }
}

if (-not $startNow) {
    Write-Host "Skipped - the mic button will just stay disabled until you run ./run.ps1 -SpeechServiceOnly yourself, or re-run this script and choose Y/B." -ForegroundColor Yellow
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

$httpsPort = Get-EnvValue "HTTPS_PORT" "443"
$url = if ($httpsPort -eq "443") { "https://$domainName/" } else { "https://${domainName}:${httpsPort}/" }

Write-Host ""
Write-Host "==> Up and running" -ForegroundColor Green
Write-Host "App:              $url"
Write-Host "Backend API docs: http://localhost:8000/docs"
Write-Host "(Self-signed cert - your browser will warn once until you trust it.)"
Write-Host "(restart: unless-stopped is set on every service - the stack comes back up on its own after a reboot or Docker Desktop restart, but not the speech service unless you chose B above.)"

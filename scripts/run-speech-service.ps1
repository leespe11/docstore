#!/usr/bin/env pwsh
# Runs the host-side speech-to-text service (speech-service/) so the chat
# tab's mic button works. Not Dockerized, same reason Ollama isn't: GPU
# access without fighting Docker GPU passthrough on Windows. Start this
# alongside `docker compose up`, same as you already do with Ollama.
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$serviceDir = Join-Path $repoRoot "speech-service"

$python = "C:\Users\spenc\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Falling back to 'python' on PATH (expected path not found: $python)" -ForegroundColor Yellow
    $python = "python"
}

Set-Location $serviceDir

# Must bind 0.0.0.0, not 127.0.0.1/localhost — Docker Desktop reaches the
# host through a virtual NAT adapter, not loopback, so the backend
# container's host.docker.internal:8090 only resolves if this listens on
# all interfaces.
Write-Host "==> Starting speech service on 0.0.0.0:8090 (reachable from Docker as host.docker.internal:8090)" -ForegroundColor Cyan
& $python -m uvicorn server:app --host 0.0.0.0 --port 8090

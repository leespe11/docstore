#!/usr/bin/env pwsh
# Pull the models this app needs into your local Ollama, based on the
# OLLAMA_*_MODEL settings in .env (falling back to this app's own defaults
# -- see backend/app/config.py -- for anything not set, or if there's no
# .env yet).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

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

$llmModel = Get-EnvValue "OLLAMA_LLM_MODEL" "qwen2.5:14b"
$visionModel = Get-EnvValue "OLLAMA_VISION_MODEL" "qwen2.5vl:7b"
$embedModel = Get-EnvValue "OLLAMA_EMBED_MODEL" "bge-large"
$extractModel = Get-EnvValue "OLLAMA_EXTRACT_MODEL" ""

$models = @(
    $llmModel,     # reasoning / chat LLM (OLLAMA_LLM_MODEL)
    $visionModel,  # vision OCR (OLLAMA_VISION_MODEL)
    $embedModel    # embeddings (OLLAMA_EMBED_MODEL) -- must match EMBED_DIM in .env
)

# OLLAMA_EXTRACT_MODEL is optional and blank by default -- config.py reuses
# OLLAMA_LLM_MODEL when it's blank, so there's nothing extra to pull unless
# it's actually set to something different.
if ($extractModel -and $extractModel -ne $llmModel) {
    $models += $extractModel  # ingest-time field extraction (OLLAMA_EXTRACT_MODEL)
}

foreach ($m in $models) {
    Write-Host "==> ollama pull $m" -ForegroundColor Cyan
    ollama pull $m
}

Write-Host "Done. Installed models:" -ForegroundColor Green
ollama list

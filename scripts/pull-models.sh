#!/usr/bin/env bash
# Pull the models this app needs into your local Ollama, based on the
# OLLAMA_*_MODEL settings in .env (falling back to this app's own defaults
# — see backend/app/config.py — for anything not set, or if there's no
# .env yet).
set -euo pipefail

cd "$(dirname "$0")/.."

env_value() {
  local name="$1" default="$2" value=""
  if [ -f .env ]; then
    value="$(grep -E "^${name}=" .env | tail -n1 | cut -d= -f2- | tr -d '\r' | xargs || true)"
  fi
  echo "${value:-$default}"
}

llm_model="$(env_value OLLAMA_LLM_MODEL qwen2.5:14b)"
vision_model="$(env_value OLLAMA_VISION_MODEL qwen2.5vl:7b)"
embed_model="$(env_value OLLAMA_EMBED_MODEL bge-large)"
extract_model="$(env_value OLLAMA_EXTRACT_MODEL "")"

models=(
  "$llm_model"     # reasoning / chat LLM (OLLAMA_LLM_MODEL)
  "$vision_model"  # vision OCR (OLLAMA_VISION_MODEL)
  "$embed_model"   # embeddings (OLLAMA_EMBED_MODEL) — must match EMBED_DIM in .env
)

# OLLAMA_EXTRACT_MODEL is optional and blank by default — config.py reuses
# OLLAMA_LLM_MODEL when it's blank, so there's nothing extra to pull unless
# it's actually set to something different.
if [ -n "$extract_model" ] && [ "$extract_model" != "$llm_model" ]; then
  models+=("$extract_model")  # ingest-time field extraction (OLLAMA_EXTRACT_MODEL)
fi

for m in "${models[@]}"; do
  echo "==> ollama pull $m"
  ollama pull "$m"
done

echo "Done. Installed models:"
ollama list

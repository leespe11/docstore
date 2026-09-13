from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # infra
    database_url: str = "postgresql+asyncpg://docstore:docstore@db:5432/docstore"
    redis_url: str = "redis://redis:6379"
    storage_dir: str = "/data/files"

    # ollama
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_llm_model: str = "qwen2.5:14b"
    ollama_vision_model: str = "qwen2.5vl:7b"
    # Used for ingest-time field extraction (documents -> structured JSON).
    # That's a more mechanical task than open-ended chat, so a smaller/faster
    # model is often good enough — leave blank to reuse ollama_llm_model.
    ollama_extract_model: str = ""
    ollama_embed_model: str = "bge-large"
    # A large num_ctx (below) makes prompt processing much slower on a dense
    # document, and this was never actually exposed as a tunable — raise
    # further via .env if you still see httpx.ReadTimeout in worker logs.
    ollama_timeout_s: float = 1800.0
    # A rasterized page + OCR prompt easily exceeds Ollama's 4096-token
    # default context window once the vision model tokenizes the image, and
    # the field-extraction call combines a long system prompt with a dense
    # document's full OCR text. Raise further via .env (uses more VRAM) if
    # you still see "exceeds the available context size" in worker logs, or
    # documents silently coming back with category "other"/doc_type
    # "unknown" and an empty facts — that's this limit being hit on the
    # extraction call specifically (see classify.py's error surfacing).
    ollama_num_ctx: int = 32768

    # pipeline
    embed_dim: int = 1024
    max_upload_mb: int = 400
    ocr_dpi: int = 200
    # Longest edge (px) an image sent to the vision model is allowed to be —
    # downscaled and re-encoded as JPEG above this, to avoid oversized
    # request bodies (Ollama/any proxy in front of it can 413) and to keep
    # vision-token usage (and therefore required num_ctx) in check.
    ocr_max_dimension: int = 2000
    ocr_jpeg_quality: int = 90
    chunk_chars: int = 1000
    chunk_overlap: int = 150

    # near-duplicate detection (see ingest/dedupe.py) — a new document is
    # flagged against existing ones in the same category within this many
    # days of its date (if it has one), and flagged when the combined
    # text/facts/amount similarity score reaches the threshold.
    dedupe_similarity_threshold: float = 0.72
    dedupe_date_window_days: int = 3

    # agent
    agent_max_steps: int = 8
    retrieval_k: int = 8

    # speech-to-text (voice input in the chat tab) — a separate host-side
    # service (speech-service/), not Dockerized, for the same reason Ollama
    # isn't: GPU access without fighting Docker GPU passthrough on Windows.
    speech_base_url: str = "http://host.docker.internal:8090"
    speech_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

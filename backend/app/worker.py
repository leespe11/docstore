import logging

from arq.connections import RedisSettings

from .config import settings
from .ingest.pipeline import process_document

# arq runs this as its own process (never imports main.py), so without this
# the root logger defaults to WARNING and every log.info() in the ingest
# pipeline — including the extraction diagnostics — is silently dropped.
logging.basicConfig(level=logging.INFO)


async def startup(ctx):  # noqa: ANN001
    pass


async def shutdown(ctx):  # noqa: ANN001
    pass


class WorkerSettings:
    functions = [process_document]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 2  # OCR is GPU/VRAM bound on a single Ollama instance; keep this low
    # Pages are OCR'd one vision-model call at a time, so a long multi-page
    # scan can take a while — 30min wasn't enough headroom now that large
    # (200MB+) files are expected. Raise further if you still see jobs killed
    # mid-run on very long documents.
    job_timeout = 3600

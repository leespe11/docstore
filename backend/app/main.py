from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .db import Base, engine
from .queue import close_pool
from .routers import chat, chats, documents, jobs, speech

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# `Base.metadata.create_all` only creates tables that don't exist yet — it
# never ALTERs an existing table to add a column a model gained later. Until
# this project has a real migration tool (Alembic), any new column added to
# an *existing* table (brand-new tables are unaffected) needs a matching
# idempotent line here, or it'll silently never reach the live database and
# every query touching it will start failing with UndefinedColumnError.
_SCHEMA_PATCHES = [
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS duplicate_of_id uuid "
    "REFERENCES documents(id) ON DELETE SET NULL",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS duplicate_similarity numeric(5,4)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS duplicate_reviewed boolean NOT NULL DEFAULT false",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS quality_score numeric(6,2)",
    "CREATE INDEX IF NOT EXISTS ix_documents_duplicate_of_id ON documents (duplicate_of_id)",
    # Collapsed issuer/issue_date/period_start/period_end/currency/
    # total_amount into one `date` column (facts now carries everything
    # else — it's exhaustive per ingest/classify.py). Runs unconditionally;
    # a fresh install's `documents` table never had the old columns, so the
    # backfill/drop steps below are gated on the old columns actually
    # existing rather than assuming a legacy database.
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS date date",
    "CREATE INDEX IF NOT EXISTS ix_documents_date ON documents (date)",
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'documents' AND column_name = 'period_end'
        ) THEN
            UPDATE documents SET date = COALESCE(period_end, issue_date) WHERE date IS NULL;
            ALTER TABLE documents DROP COLUMN IF EXISTS issuer;
            ALTER TABLE documents DROP COLUMN IF EXISTS issue_date;
            ALTER TABLE documents DROP COLUMN IF EXISTS period_start;
            ALTER TABLE documents DROP COLUMN IF EXISTS period_end;
            ALTER TABLE documents DROP COLUMN IF EXISTS currency;
            ALTER TABLE documents DROP COLUMN IF EXISTS total_amount;
        END IF;
    END $$;
    """,
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        for statement in _SCHEMA_PATCHES:
            await conn.execute(text(statement))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw "
                "ON chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
    log.info("schema ready")
    yield
    await close_pool()


app = FastAPI(title="Document Storage API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(jobs.router)
app.include_router(chat.router)
app.include_router(chats.router)
app.include_router(speech.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

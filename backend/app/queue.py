from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from .config import settings

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def enqueue_process_document(document_id: str, job_id: str) -> None:
    pool = await get_pool()
    await pool.enqueue_job("process_document", document_id, job_id)

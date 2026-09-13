"""Thin async client around the Ollama HTTP API.

Endpoints used:
  POST /api/chat   -- chat, tool-calling, vision (images on a message), streaming NDJSON
  POST /api/embed  -- batch embeddings
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import settings


class OllamaError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=settings.ollama_timeout_s)


async def _raise_for_status(resp: httpx.Response) -> None:
    """Like resp.raise_for_status(), but surfaces Ollama's own error message
    (e.g. "model 'x' not found, try pulling it first") instead of just the
    generic HTTP status text."""
    if resp.status_code < 400:
        return
    detail = f"HTTP {resp.status_code}"
    try:
        body = await resp.aread()
        data = json.loads(body)
        detail = data.get("error") or body.decode(errors="replace") or detail
    except Exception:
        pass
    raise OllamaError(
        f"Ollama request to {resp.request.url.path} failed ({resp.status_code}): {detail}"
    )


def _with_default_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """num_ctx defaults to settings.ollama_num_ctx everywhere; callers can
    still override it (or anything else) by passing it explicitly."""
    return {"num_ctx": settings.ollama_num_ctx, **(options or {})}


async def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    async with _client() as client:
        resp = await client.post(
            "/api/embed",
            json={"model": model or settings.ollama_embed_model, "input": texts},
        )
        await _raise_for_status(resp)
        data = resp.json()
        embeddings = data.get("embeddings")
        if not embeddings:
            raise OllamaError(f"no embeddings returned: {data}")
        return embeddings


async def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    format: str | dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single non-streaming chat call. Returns the `message` dict."""
    payload: dict[str, Any] = {
        "model": model or settings.ollama_llm_model,
        "messages": messages,
        "stream": False,
        "options": _with_default_options(options),
    }
    if tools:
        payload["tools"] = tools
    if format:
        payload["format"] = format

    async with _client() as client:
        resp = await client.post("/api/chat", json=payload)
        await _raise_for_status(resp)
        data = resp.json()
        message = data.get("message")
        if message is None:
            raise OllamaError(f"no message returned: {data}")
        return message


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming chat call. Yields each NDJSON line's `message` dict as it
    arrives (content deltas and/or a final tool_calls payload), plus a last
    item with done=True."""
    payload: dict[str, Any] = {
        "model": model or settings.ollama_llm_model,
        "messages": messages,
        "stream": True,
        "options": _with_default_options(options),
    }
    if tools:
        payload["tools"] = tools

    async with _client() as client:
        async with client.stream("POST", "/api/chat", json=payload) as resp:
            await _raise_for_status(resp)
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                message = chunk.get("message", {})
                yield {**message, "done": chunk.get("done", False)}


async def vision_transcribe(image_b64: str, prompt: str, *, model: str | None = None) -> str:
    message = await chat(
        [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
        model=model or settings.ollama_vision_model,
        options={"temperature": 0.0},
    )
    return message.get("content", "")

"""Thin async client for the host-side speech-to-text service
(speech-service/), reached the same way as Ollama: host.docker.internal,
because it needs to run outside Docker for GPU access. See
run.ps1|sh -SpeechServiceOnly (repo root).
"""

from __future__ import annotations

import json

import httpx

from .config import settings


class SpeechError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.speech_base_url, timeout=settings.speech_timeout_s)


async def _raise_for_status(resp: httpx.Response) -> None:
    """Like resp.raise_for_status(), but surfaces the speech service's own
    FastAPI `detail` message instead of just the generic HTTP status text."""
    if resp.status_code < 400:
        return
    detail = f"HTTP {resp.status_code}"
    try:
        body = await resp.aread()
        data = json.loads(body)
        detail = data.get("detail") or body.decode(errors="replace") or detail
    except Exception:
        pass
    raise SpeechError(f"Speech service request failed ({resp.status_code}): {detail}")


async def check_status() -> bool:
    """Quick reachability check for the mic button's enabled state — a short
    fixed timeout regardless of settings.speech_timeout_s, since this is
    polled from the frontend and shouldn't ever make the UI wait tens of
    seconds to find out the host service is just off."""
    try:
        async with httpx.AsyncClient(base_url=settings.speech_base_url, timeout=3.0) as client:
            resp = await client.get("/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def transcribe(audio_bytes: bytes, filename: str, content_type: str) -> str:
    try:
        async with _client() as client:
            resp = await client.post(
                "/transcribe",
                files={"audio": (filename, audio_bytes, content_type)},
            )
            await _raise_for_status(resp)
            data = resp.json()
            return data.get("text", "")
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # By far the most likely failure: the host script just isn't
        # running (it's manually started, not managed by docker compose) —
        # worth a specific, actionable message rather than a generic one.
        raise SpeechError(
            "Speech service isn't reachable — start it with ./run.ps1 or ./run.sh (see README)"
        ) from exc

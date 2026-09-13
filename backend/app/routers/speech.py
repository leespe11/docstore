from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from .. import speech_client

router = APIRouter(tags=["speech"])


@router.get("/speech/status")
async def status_endpoint() -> dict:
    """Lets the frontend enable/disable the mic button based on whether the
    (optional, host-side) speech service is actually reachable, instead of
    only finding out on the first failed transcription attempt."""
    return {"available": await speech_client.check_status()}


@router.post("/speech/transcribe")
async def transcribe_endpoint(audio: UploadFile = File(...)) -> dict:
    data = await audio.read()
    if not data:
        raise HTTPException(422, "empty audio")
    try:
        text = await speech_client.transcribe(data, audio.filename or "clip.webm", audio.content_type or "audio/webm")
    except speech_client.SpeechError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"text": text}

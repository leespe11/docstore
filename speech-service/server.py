"""Standalone speech-to-text service, run directly on the host (not
Dockerized) so it can use the GPU without fighting Docker GPU passthrough on
Windows — the same reason Ollama runs on the host in this project. Reached
from the backend container via host.docker.internal. See
run.ps1|sh -SpeechServiceOnly (repo root) to start it and
speech-service/smoke_test.py to check GPU vs CPU support first.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("speech-service")

# Default is "cpu", not "cuda": smoke-tested on this host's RTX 5070 Ti and
# ctranslate2's CUDA kernels don't support it yet (cuBLAS_STATUS_NOT_SUPPORTED
# — a Blackwell/CUDA-13-vs-ctranslate2 gap, not a bug in this code). A
# `small` model transcribes a short dictated clip on CPU in a few seconds,
# which is fine for this use case. Set WHISPER_DEVICE=cuda to retry GPU
# after a ctranslate2 upgrade — the fallback-to-CPU logic below still
# applies if that attempt fails.
MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")
PREFERRED_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
GPU_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8_float16")
CPU_COMPUTE_TYPE = os.environ.get("WHISPER_CPU_COMPUTE_TYPE", "int8")
IDLE_UNLOAD_S = float(os.environ.get("WHISPER_IDLE_UNLOAD_S", "600"))

# Single mutable slot for the loaded model — a personal, single-user service,
# so an asyncio.Lock (not a real queue) is enough to stop two concurrent
# requests from both trying to allocate GPU memory at once.
_lock = asyncio.Lock()
_state: dict = {"model": None, "device": None, "last_used": 0.0}


def _compute_type_for(device: str) -> str:
    return GPU_COMPUTE_TYPE if device == "cuda" else CPU_COMPUTE_TYPE


def _load_model(device: str) -> WhisperModel:
    compute_type = _compute_type_for(device)
    log.info("loading whisper model=%s device=%s compute_type=%s", MODEL_SIZE, device, compute_type)
    return WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)


def _ensure_model_loaded() -> None:
    if _state["model"] is None:
        try:
            _state["model"] = _load_model(PREFERRED_DEVICE)
            _state["device"] = PREFERRED_DEVICE
        except Exception:
            # Real risk on newer GPUs (e.g. Blackwell) / newer CUDA majors:
            # ctranslate2's bundled CUDA kernels can simply fail to init.
            # A `small` model on CPU is fast enough for a short dictated
            # clip, so falling back here is a legitimate answer, not just a
            # degraded patch.
            log.exception("failed to load model on device=%s, falling back to cpu", PREFERRED_DEVICE)
            _state["model"] = _load_model("cpu")
            _state["device"] = "cpu"
    _state["last_used"] = time.monotonic()


def _unload_model() -> None:
    if _state["model"] is not None:
        log.info("unloading idle whisper model (was on device=%s)", _state["device"])
    _state["model"] = None
    _state["device"] = None


async def _idle_unloader() -> None:
    """Mirrors Ollama's own idle-unload behavior, which this app already
    relies on elsewhere — keeps VRAM free for Ollama between voice-input
    bursts instead of holding it permanently for a service that might sit
    open all day."""
    while True:
        await asyncio.sleep(30)
        if _state["model"] is not None and time.monotonic() - _state["last_used"] > IDLE_UNLOAD_S:
            async with _lock:
                _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_idle_unloader())
    yield
    task.cancel()


app = FastAPI(title="speech-service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model_size": MODEL_SIZE,
        "model_loaded": _state["model"] is not None,
        "device": _state["device"],
    }


def _run_transcribe(data: bytes, language: str | None) -> str:
    buf = io.BytesIO(data)
    segments, _info = _state["model"].transcribe(buf, language=language or None, vad_filter=True, beam_size=5)
    return " ".join(seg.text.strip() for seg in segments).strip()


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str | None = Form(None)) -> dict:
    data = await audio.read()
    if not data:
        raise HTTPException(422, "empty audio")

    async with _lock:
        _ensure_model_loaded()
        try:
            text = _run_transcribe(data, language)
        except Exception:
            log.exception("transcription failed on device=%s", _state["device"])
            if _state["device"] == "cpu":
                raise HTTPException(500, "transcription failed") from None
            # One retry on CPU for this request only — cheap insurance
            # against a transient GPU OOM (e.g. Ollama grabbed memory at the
            # same moment) rather than failing the whole request.
            _unload_model()
            _state["model"] = _load_model("cpu")
            _state["device"] = "cpu"
            try:
                text = _run_transcribe(data, language)
            except Exception:
                log.exception("transcription failed on cpu fallback too")
                raise HTTPException(500, "transcription failed") from None
        _state["last_used"] = time.monotonic()

    return {"text": text}

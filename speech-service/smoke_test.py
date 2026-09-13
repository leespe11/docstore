"""Run this once after `pip install -r requirements.txt`, before relying on
the service, to find out whether GPU transcription actually works on this
machine — newer GPUs/CUDA majors (e.g. Blackwell + CUDA 13) can be ahead of
what ctranslate2's bundled CUDA kernels support, so this is checked directly
rather than assumed. Generates its own test tone — no sample audio file
needed; the point is confirming the pipeline runs end-to-end; the tone's
transcribed content (if any) is meaningless and safe to ignore.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave


def _make_test_wav(duration_s: float = 2.0, freq: float = 440.0, sample_rate: int = 16000) -> io.BytesIO:
    n_samples = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            val = int(3000 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    buf.seek(0)
    return buf


def _try(device: str, compute_type: str) -> bool:
    from faster_whisper import WhisperModel

    print(f"Trying device={device} compute_type={compute_type} ...")
    try:
        model = WhisperModel("small", device=device, compute_type=compute_type)
        audio = _make_test_wav()
        segments, _info = model.transcribe(audio, beam_size=1, vad_filter=False)
        text = " ".join(seg.text for seg in segments)
        print(f"  OK — model loaded and ran inference on {device}. (test-tone output, ignore content: {text!r})")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED on {device}: {exc}")
        return False


def main() -> None:
    if _try("cuda", "int8_float16"):
        print("\nRESULT: GPU works. Use WHISPER_DEVICE=cuda WHISPER_COMPUTE_TYPE=int8_float16 (already the default).")
        return

    print()
    if _try("cpu", "int8"):
        print("\nRESULT: GPU failed, but CPU works. Set WHISPER_DEVICE=cpu before running the service.")
        print("(A 'small' model on CPU is still fine for short dictated clips.)")
        return

    print("\nRESULT: faster-whisper could not run on this machine at all, on GPU or CPU.")
    print("Check the installation (pip install -r requirements.txt) and the errors above.")
    sys.exit(1)


if __name__ == "__main__":
    main()

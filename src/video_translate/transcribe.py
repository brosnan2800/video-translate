"""Chunked, resumable transcription with faster-whisper (CPU / int8).

WHY CHUNKED: on a machine without an NVIDIA GPU, large-v3 runs at ~1x realtime on
CPU. A full 12-minute video therefore exceeds the per-process time limit of some
execution environments and gets SIGKILLed. We split the audio into chunks, persist
each chunk's result as chunk_N.json, and merge at the end. If the process is killed,
re-running skips already-completed chunks (true resume — the original script lacked this).

GOTCHA: CTranslate2 only supports NVIDIA CUDA, so device/compute_type are forced to
cpu/int8. faster_whisper is imported lazily so that unit/contract tests and the
translate/generate subcommands don't need the heavy library or the 3GB model.
"""
from __future__ import annotations

import os
from typing import Any

from .ffmpeg_utils import extract_chunk, probe_duration
from .io_utils import load_json, load_json_default, save_json

# Forced constants — see module docstring.
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
BEAM_SIZE = 1
BEST_OF = 1
VAD_PARAMS = {"min_silence_duration_ms": 500, "speech_pad_ms": 200}


def plan_chunks(total: float, chunk: float) -> list[tuple[int, float, float]]:
    """Plan chunk boundaries.

    Returns a list of (chunk_index, start_sec, duration_sec) covering [0, total).
    Mirrors the original `n = int(total // chunk) + 1` scheme.
    """
    if total <= 0:
        return []
    n_chunks = int(total // chunk) + 1
    plan: list[tuple[int, float, float]] = []
    for ci in range(n_chunks):
        cstart = ci * chunk
        cdur = min(chunk, total - cstart)
        if cdur <= 0:
            break
        plan.append((ci, cstart, cdur))
    return plan


def merge_chunks(chunk_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Flatten per-chunk segment lists into one ordered list."""
    merged: list[dict[str, Any]] = []
    for cl in chunk_lists:
        merged.extend(cl)
    return merged


def _chunk_json_path(outdir: str, ci: int) -> str:
    return os.path.join(outdir, f"chunk_{ci}.json")


def transcribe_video(
    input_path: str,
    outdir: str,
    *,
    base: str = "apollo_story",
    model_name: str = "large-v3",
    chunk: float = 240.0,
    threads: int | None = None,
    lang: str = "en",
    progress=print,
) -> str:
    """Transcribe `input_path` into `{outdir}/{base}.segments_en.json`.

    Resumable: any chunk whose chunk_N.json already exists is reused, not re-run.

    Returns:
        Path to the merged segments JSON.
    """
    from faster_whisper import WhisperModel  # lazy: heavy import

    os.makedirs(outdir, exist_ok=True)
    threads = threads or os.cpu_count()
    total = probe_duration(input_path)
    plan = plan_chunks(total, chunk)

    model: WhisperModel | None = None
    chunk_lists: list[list[dict[str, Any]]] = []

    for ci, cstart, cdur in plan:
        cjson = _chunk_json_path(outdir, ci)
        existing = load_json_default(cjson, None)
        if existing is not None:
            progress(f"[skip] chunk {ci} already done (segs={len(existing)})")
            chunk_lists.append(existing)
            continue

        if model is None:  # defer model load until we actually need it (resume-friendly)
            progress(f"[load] {model_name} device={DEVICE} compute={COMPUTE_TYPE} threads={threads}")
            model = WhisperModel(model_name, device=DEVICE, compute_type=COMPUTE_TYPE, cpu_threads=threads)

        wav = os.path.join(outdir, f"chunk_{ci}.wav")
        extract_chunk(input_path, wav, cstart, cdur)
        segs, _info = model.transcribe(
            wav, language=lang, task="transcribe",
            beam_size=BEAM_SIZE, best_of=BEST_OF,
            vad_filter=True, vad_parameters=dict(VAD_PARAMS),
        )
        chunk_segs = [
            {"start": round(s.start + cstart, 2),
             "end": round(s.end + cstart, 2),
             "text": s.text.strip()}
            for s in segs
        ]
        save_json(cjson, chunk_segs, indent=0)
        chunk_lists.append(chunk_segs)
        progress(f"[done] chunk {ci} start={cstart:.0f}s segs={len(chunk_segs)}")
        if os.path.exists(wav):
            os.remove(wav)

    all_segs = merge_chunks(chunk_lists)
    out = os.path.join(outdir, f"{base}.segments_en.json")
    save_json(out, all_segs, indent=0)
    progress(f"[merge] total {len(all_segs)} segments -> {out}")
    return out

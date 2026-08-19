"""Chunked, resumable transcription with faster-whisper (CPU / int8).

WHY CHUNKED: on a machine without an NVIDIA GPU, large-v3 runs at ~1x realtime on
CPU. A full 12-minute video therefore exceeds the per-process time limit of some
execution environments and gets SIGKILLed. We split the audio into chunks, persist
each chunk's result as chunk_N.json, and merge at the end. If the process is killed,
re-running skips already-completed chunks (true resume — the original script lacked this).

GOTCHA: CTranslate2 only supports NVIDIA CUDA (no AMD/Metal GPU path), so the
default device/compute_type is cpu/int8. Since V5 the device is no longer a
hard-coded constant: it resolves from ``device=auto`` (CUDA when an NVIDIA GPU
is present, else cpu) and can be pinned via CLI/config/env (ADR-014). On a
machine without CUDA this resolves to the exact historical cpu/int8 behaviour,
so Mac output is byte-for-byte unchanged. faster_whisper is imported lazily so
that unit/contract tests and the translate/generate subcommands don't need the
heavy library or the 3GB model.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .ffmpeg_utils import extract_chunk, probe_duration
from .io_utils import load_json, load_json_default, save_json

# Default device/compute_type — kept as module-level defaults for backward
# compatibility, but no longer forced: transcribe_video/transcribe_window accept
# ``device``/``compute_type`` (defaulting to "auto") and resolve them at call
# time. Mac without CUDA resolves to cpu/int8 exactly as before.
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "auto"


def _cuda_available() -> bool:
    """True when an NVIDIA GPU usable by CTranslate2 is present.

    Prefers ``nvidia-smi`` (no heavy import) and falls back to ``torch`` only
    if it is already installed. Never imports torch just to probe.
    """
    if shutil.which("nvidia-smi"):
        return True
    try:
        import torch  # noqa: F401  # lazy, may be absent on CPU-only boxes
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_device(device: str | None = None,
                   compute_type: str | None = None) -> tuple[str, str]:
    """Resolve ``device``/``compute_type`` ("auto" or None -> concrete values).

    - device "auto"/None -> "cuda" if an NVIDIA GPU is present, else "cpu".
    - compute_type "auto"/None -> "int8_float16" on cuda (8GB-friendly), "int8"
      on cpu (preserves the historical cpu/int8 output exactly).

    Returns ``(device, compute_type)``. On a CUDA-free machine the result is
    always ``("cpu", "int8")`` — identical to the pre-V5 hard-coded constants.
    """
    dev = (device or DEFAULT_DEVICE).lower()
    if dev == "auto":
        dev = "cuda" if _cuda_available() else "cpu"
    ct = (compute_type or DEFAULT_COMPUTE_TYPE).lower()
    if ct == "auto":
        ct = "int8_float16" if dev == "cuda" else "int8"
    return dev, ct
# V4 (quality pass): beam search instead of greedy. greedy (beam=1) is the
# single biggest driver of repetition hallucinations ("movie is a movie") and
# filler-word misreads ("I" for "uh"). beam=5 costs ~3-5x CPU time but is the
# standard anti-hallucination setting.
BEAM_SIZE = 5
BEST_OF = 5
# V4: do NOT condition each segment on the previous segment's text — carrying
# context is what makes hallucinations self-reinforcing across segment
# boundaries (the "and at the end of the day" repeat). Cost: slightly less
# cross-sentence coherence, acceptable for interview content.
CONDITION_ON_PREVIOUS_TEXT = False
# V4: mild repetition penalty inside a single segment (1.0 = off).
REPETITION_PENALTY = 1.1
# A: recover stylized / sung / impression audio that whisper's DEFAULT
# no_speech gate (0.6) silently drops as "non-speech". 0.0 = never suppress a
# window as silence. The only downside is a little more hallucination in true
# silence, which the downstream hallucination filter already bounds.
NO_SPEECH_THRESHOLD = 0.0
# A: temperature fallback — whisper retries at higher temperature when the
# first pass is low-confidence, recovering mumbled / stylized lines instead of
# emitting nothing.
TEMPERATURE_FALLBACK = [0.0, 0.2, 0.4]
# V4: wider speech pad so short interjections at segment edges are not clipped
# by VAD (the 45-46s missed host line). 200ms was too tight for talk-show pace.
# V6 (B2): Silero's default speech threshold of 0.5 drops quiet or
# music-underscored utterances entirely — a whole isolated line ("Saladin" over
# the opening score) never reached the decoder, so no amount of downstream
# fixing could recover it. 0.35 is the standard "recall over precision" setting;
# the extra noise it admits is what the V4 hallucination filter already handles.
# neg_threshold is left unset: faster-whisper derives max(threshold-0.15, 0.01).
VAD_THRESHOLD = 0.35
VAD_PARAMS = {
    "threshold": VAD_THRESHOLD,
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 400,
}


def build_vad_params(threshold: float | None = None) -> dict[str, Any]:
    """VAD parameter dict, optionally overriding the speech threshold.

    Exposed so a single hard video can be re-run more (or less) aggressively
    without editing source; the value feeds the cache fingerprint, so changing
    it invalidates chunk caches automatically.
    """
    params = dict(VAD_PARAMS)
    if threshold is not None:
        params["threshold"] = threshold
    return params


def transcribe_fingerprint(
    model_name: str, chunk: float, lang: str | None,
    vad: dict[str, Any] | None = None,
    use_vad: bool = False,
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    temperature: list[float] | None = None,
    device: str | None = None,
    compute_type: str | None = None,
) -> str:
    """Content hash of every parameter that changes transcription output.

    WHY: chunk caches ({base}.chunk_N.json) must be invalidated when the
    transcription recipe changes — otherwise a beam=1 cache would be silently
    reused for a beam=5 run (the same contamination class as the multi-video
    cache collision, now extended to parameter drift).

    The fingerprint now includes the VAD on/off flag, the no_speech gate and
    the temperature fallback, so any change to the recovery recipe forces a
    clean re-transcription instead of reusing a stale chunk cache.

    V5 (ADR-014): the RESOLVED device/compute_type are part of the fingerprint,
    so a cpu/int8 cache is never reused for a cuda/int8_float16 run (or vice
    versa). On a CUDA-free machine the resolved values are always cpu/int8, so
    historical fingerprints are unchanged.
    """
    dev, ct = resolve_device(device, compute_type)
    payload = {
        "model": model_name,
        "beam": BEAM_SIZE,
        "best_of": BEST_OF,
        "lang": lang,
        "device": dev,
        "compute": ct,
        "chunk": chunk,
        "vad_filter": use_vad,
        "vad": vad if (use_vad and vad is not None) else None,
        "cond_prev": CONDITION_ON_PREVIOUS_TEXT,
        "rep_penalty": REPETITION_PENALTY,
        "no_speech": no_speech_threshold,
        "temp": temperature or TEMPERATURE_FALLBACK,
        "word_ts": True,
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:8]


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


def _chunk_json_path(outdir: str, base: str, ci: int, fingerprint: str) -> str:
    return os.path.join(outdir, f"{base}.{fingerprint}.chunk_{ci}.json")


def transcribe_video(
    input_path: str,
    outdir: str,
    *,
    base: str | None = None,
    model_name: str = "large-v3",
    chunk: float = 240.0,
    threads: int | None = None,
    lang: str | None = None,
    vad_threshold: float | None = None,
    use_vad: bool = False,
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    temperature: list[float] | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    progress=print,
) -> str:
    """Transcribe `input_path` into `{outdir}/{base}.segments_en.json`.

    V2: ``base`` defaults to the input filename stem; ``lang`` defaults to None
    (Whisper auto-detect). Resumable: any chunk whose chunk_N.json already
    exists is reused, not re-run.

    V5 (ADR-014): ``device``/``compute_type`` default to "auto", resolved via
    :func:`resolve_device` (CUDA when available, else the historical cpu/int8).

    Returns:
        Path to the merged segments JSON.
    """
    from faster_whisper import WhisperModel  # lazy: heavy import

    os.makedirs(outdir, exist_ok=True)
    base = base or Path(input_path).stem
    threads = threads or os.cpu_count()
    total = probe_duration(input_path)
    plan = plan_chunks(total, chunk)
    vad_params = build_vad_params(vad_threshold)
    dev, ct = resolve_device(device, compute_type)
    fp = transcribe_fingerprint(model_name, chunk, lang, vad_params,
                                use_vad=use_vad,
                                no_speech_threshold=no_speech_threshold,
                                temperature=temperature,
                                device=dev, compute_type=ct)

    model: WhisperModel | None = None
    chunk_lists: list[list[dict[str, Any]]] = []

    for ci, cstart, cdur in plan:
        cjson = _chunk_json_path(outdir, base, ci, fp)
        existing = load_json_default(cjson, None)
        if existing is not None:
            progress(f"[skip] chunk {ci} already done (segs={len(existing)})")
            chunk_lists.append(existing)
            continue

        if model is None:  # defer model load until we actually need it (resume-friendly)
            progress(f"[load] {model_name} device={dev} compute={ct} threads={threads}")
            model = WhisperModel(model_name, device=dev, compute_type=ct, cpu_threads=threads)

        wav = os.path.join(outdir, f"{base}.{fp}.chunk_{ci}.wav")
        extract_chunk(input_path, wav, cstart, cdur)
        segs, _info = model.transcribe(
            wav, language=lang, task="transcribe",
            beam_size=BEAM_SIZE, best_of=BEST_OF,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            repetition_penalty=REPETITION_PENALTY,
            vad_filter=use_vad,
            **(dict(vad_parameters=vad_params) if use_vad else {}),
            no_speech_threshold=no_speech_threshold,
            temperature=temperature or TEMPERATURE_FALLBACK,
            word_timestamps=True,   # V3: word-level timestamps for split + silence
        )
        chunk_segs = [
            {
                "start": round(s.start + cstart, 2),
                "end": round(s.end + cstart, 2),
                "text": s.text.strip(),
                "words": [
                    {"word": w.word, "start": round(w.start + cstart, 2),
                     "end": round(w.end + cstart, 2)}
                    for w in (s.words or [])
                ],
            }
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


def transcribe_window(
    input_path: str, start: float, end: float,
    *,
    lang: str | None = None,
    use_vad: bool = False,
    model_name: str = "large-v3",
    threads: int | None = None,
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    temperature: list[float] | None = None,
    device: str | None = None,
    compute_type: str | None = None,
) -> list[dict[str, Any]]:
    """Transcribe a single [start, end) window with a forced ``lang``.

    Returns segments with ABSOLUTE timestamps (each segment/word time is
    shifted by ``start``). Used by the ``resegment`` command to fix
    mis-detected language spans (e.g. Japanese lines heard as English)
    without re-running the whole video.
    """
    import tempfile
    from faster_whisper import WhisperModel

    threads = threads or os.cpu_count()
    vad_params = build_vad_params(None)
    dev, ct = resolve_device(device, compute_type)
    dur = max(0.1, end - start)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = tf.name
    try:
        extract_chunk(input_path, wav, start, dur)
        model = WhisperModel(model_name, device=dev, compute_type=ct,
                             cpu_threads=threads)
        segs, _info = model.transcribe(
            wav, language=lang, task="transcribe",
            beam_size=BEAM_SIZE, best_of=BEST_OF,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            repetition_penalty=REPETITION_PENALTY,
            vad_filter=use_vad,
            **(dict(vad_parameters=vad_params) if use_vad else {}),
            no_speech_threshold=no_speech_threshold,
            temperature=temperature or TEMPERATURE_FALLBACK,
            word_timestamps=True,
        )
        out = []
        for s in segs:
            words = [
                {"word": w.word,
                 "start": round(w.start + start, 2),
                 "end": round(w.end + start, 2)}
                for w in (s.words or [])
            ]
            out.append({
                "start": round(s.start + start, 2),
                "end": round(s.end + start, 2),
                "text": s.text.strip(),
                "words": words,
            })
        return out
    finally:
        if os.path.exists(wav):
            os.remove(wav)

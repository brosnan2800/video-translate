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

import gc
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import torch  # core dependency (T2/demucs); used for CUDA memory cleanup

from .ffmpeg_utils import extract_chunk, probe_duration
from .io_utils import load_json, load_json_default, save_json
from .text_utils import to_single_line
from .audio_profile import analyze_audio, route_vad_chunk
from . import align as _align
from .capabilities import GateFail
from .artifacts import workdir
from .model_cache import resolve_model_path

import re  # for ALL-CAPS artifact normalization (see _normalize_caps)


def _normalize_caps(text: str) -> str:
    """Normalize faster-whisper's ALL-CAPS artifact on loud/shouted speech.

    Whisper returns fully-uppercase segments when the audio is loud or shouted.
    That is a transcription artifact, not intentional emphasis, and reads as
    broken in subtitles. We lowercase such segments (requiring >=4 letters so
    genuine short tokens like "OK", "ID", "TV", "AI" survive) and restore
    sentence casing.
    """
    if not text:
        return text
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4 or not all(c.isupper() for c in letters):
        return text
    low = text.lower()
    # Capitalize the first letter of every sentence (split on . ! ?).
    return re.sub(r"(^|[.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), low)


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
    # Rule 3: ask the central registry — the same answer at every stage and in
    # every CWD — instead of a per-call PATH search.
    from .toolchain import tool_available

    if tool_available("nvidia-smi"):
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
    adaptive_vad: bool = False,
    compute_type: str | None = None,
    # T2: vocal separation dims (ADR-017 / Spec 19).
    # Only appended to the hash payload when separate_vocals=True, so the
    # default-OFF path keeps byte-for-byte parity with historical hashes.
    separate_vocals: bool = False,
    vocal_sep_backend: str = "demucs",
    vocal_sep_model: str = "htdemucs",
    vocal_sep_input_hash: str | None = None,
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
    if adaptive_vad:  # ADR-015: only present when on, so OFF keeps the historical hash
        payload["adaptive"] = True
    if separate_vocals:  # ADR-017 / T2: only present when on — default hash unchanged
        payload["vsep"] = True
        payload["vsep_backend"] = vocal_sep_backend
        payload["vsep_model"] = vocal_sep_model
        payload["vsep_fp"] = vocal_sep_input_hash  # None-safe — json.dumps handles it
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
    return os.path.join(workdir(outdir, base), f"{base}.{fingerprint}.chunk_{ci}.json")


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
    adaptive_vad: bool = False,
    # T2 (ADR-017): vocal-separated audio source. None → use original input_path.
    # When separate_vocals flow runs, the CLI orchestrates vocal_sep FIRST
    # (releasing GPU memory before Whisper load — 8GB safe), then passes the
    # resulting vocals.wav here as audio_source.
    audio_source: str | None = None,
    # T2 fingerprint extras (must match how CLI built audio_source).
    # Pass None/False when audio_source is None → fingerprint stays historical.
    separate_vocals: bool = False,
    vocal_sep_backend: str = "demucs",
    vocal_sep_model: str = "htdemucs",
    vocal_sep_input_hash: str | None = None,
    # T4 (ADR-028 / Spec 22): forced-acoustic-alignment backend.
    # "auto" (default, T4 默认化) -> whisperx when the host can run it (CUDA +
    # whisperx installed), else none. "whisperx" forces it (macOS / not-installed
    # gracefully falls back to none). "none" = no alignment, byte-identical to
    # historical output. Does NOT alter the transcribe fingerprint — alignment
    # is a separate cache layer.
    align_backend: str = "auto",
    # Control plane 裁决一 escape hatch: an EXPLICIT align request that cannot
    # be honored hard-stops (exit 8) unless this is explicitly enabled by the
    # caller passing --allow-degrade (brew/CI automation's opt-out).
    align_allow_degrade: bool = False,
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

    base = base or Path(input_path).stem
    os.makedirs(workdir(outdir, base), exist_ok=True)
    threads = threads or os.cpu_count()
    total = probe_duration(input_path)
    plan = plan_chunks(total, chunk)
    vad_params = build_vad_params(vad_threshold)
    dev, ct = resolve_device(device, compute_type)
    # T2: duration/invariant assert — audio_source (if provided) MUST match
    # the input timeline. If this drifts, every cue timestamp is globally
    # offset (ADR-017 §2 acoustic-truth guard).
    if audio_source is not None:
        try:
            src_dur = probe_duration(audio_source)
            if abs(src_dur - total) >= 0.05:
                raise RuntimeError(
                    f"[vsep] audio_source duration ({src_dur:.3f}s) != "
                    f"input video ({total:.3f}s). Refusing to run — timestamps "
                    f"would be globally offset."
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"[vsep] failed to probe audio_source duration: {exc}")
    # Resolve chunk extraction source (original input, or vocals.wav):
    _extract_src: str = audio_source if audio_source else input_path
    fp = transcribe_fingerprint(
        model_name, chunk, lang, vad_params,
        use_vad=use_vad,
        no_speech_threshold=no_speech_threshold,
        temperature=temperature,
        adaptive_vad=adaptive_vad,
        device=dev, compute_type=ct,
        # T2 dims — only change the hash when separate_vocals is True
        # (byte-for-byte parity with historical hashes when False).
        separate_vocals=separate_vocals,
        vocal_sep_backend=vocal_sep_backend,
        vocal_sep_model=vocal_sep_model,
        vocal_sep_input_hash=vocal_sep_input_hash,
    )

    model: WhisperModel | None = None
    chunk_lists: list[list[dict[str, Any]]] = []
    detected_language: str | None = lang  # None -> auto-detected; pass to align

    for ci, cstart, cdur in plan:
        cjson = _chunk_json_path(outdir, base, ci, fp)
        existing = load_json_default(cjson, None)
        if existing is not None:
            progress(f"[skip] chunk {ci} already done (segs={len(existing)})")
            chunk_lists.append(existing)
            continue

        if model is None:  # defer model load until we actually need it (resume-friendly)
            progress(f"[load] {model_name} device={dev} compute={ct} threads={threads}")
            model = WhisperModel(resolve_model_path(model_name), device=dev,
                                 compute_type=ct, cpu_threads=threads)

        wav = os.path.join(workdir(outdir, base), f"{base}.{fp}.chunk_{ci}.wav")
        extract_chunk(_extract_src, wav, cstart, cdur)
        chunk_vad = use_vad
        if adaptive_vad:  # ADR-015: route VAD per chunk from its local profile
            try:
                cprof = analyze_audio(wav)
                chunk_vad = route_vad_chunk(cprof, cdur)
            except Exception:  # noqa: BLE001 - profiling failure -> safe bare default
                chunk_vad = False
        segs, info = model.transcribe(
            wav, language=lang, task="transcribe",
            beam_size=BEAM_SIZE, best_of=BEST_OF,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            repetition_penalty=REPETITION_PENALTY,
            vad_filter=chunk_vad,
            **(dict(vad_parameters=vad_params) if chunk_vad else {}),
            no_speech_threshold=no_speech_threshold,
            temperature=temperature or TEMPERATURE_FALLBACK,
            word_timestamps=True,   # V3: word-level timestamps for split + silence
        )
        if info is not None and getattr(info, "language", None):
            detected_language = info.language  # capture auto-detect for alignment
            # Persist for resume: when all chunks are cached and the loop is
            # skipped, detection never runs — this sidecar keeps the align
            # fingerprint stable across re-runs (ADR-028 / Spec 22).
            save_json(os.path.join(workdir(outdir, base), f"{base}.{fp}.detected_lang.json"),
                      {"language": detected_language}, indent=0)
        chunk_segs = [
            _seg_to_dict(s, cstart)
            for s in segs
        ]
        save_json(cjson, chunk_segs, indent=0)
        chunk_lists.append(chunk_segs)
        progress(f"[done] chunk {ci} start={cstart:.0f}s segs={len(chunk_segs)}")
        if os.path.exists(wav):
            os.remove(wav)

    # --- T4 (ADR-028 / Spec 22): forced-acoustic-alignment pass ---
    # Runs AFTER the transcription loop and BEFORE merge_chunks. Steps below keep
    # the 8GB red line (Whisper released before wav2vec2 is loaded) and the
    # independent alignment cache layer (no transcribe-fingerprint coupling).

    # If the whole transcription was resumed (loop skipped), `detected_language`
    # is still None — restore it from the sidecar so the align fingerprint and
    # wav2vec2 model selection stay consistent with the original run.
    if detected_language is None:
        lang_sidecar = load_json_default(
            os.path.join(workdir(outdir, base), f"{base}.{fp}.detected_lang.json"), None
        )
        if lang_sidecar and lang_sidecar.get("language"):
            detected_language = lang_sidecar["language"]

    if align_backend != "none":
        align_backend = _resolve_align_backend(
            align_backend, progress, allow_degrade=align_allow_degrade,
        )
    if align_backend != "none":
        # Release Whisper (8GB budget) before loading wav2vec2.
        del model
        model = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        align_fp = _align.align_fingerprint(fp, detected_language,
                                            backend=align_backend)
        for ci, cstart, cdur in plan:
            acache = _align.align_cache_path(outdir, base, ci, align_fp)
            cached = load_json_default(acache, None)
            if cached is not None:
                progress(f"[align skip] chunk {ci} already aligned")
                chunk_lists[ci] = cached
                continue
            # Prefer the transcribed chunk cache (absolute timeline, +cstart).
            chunk_segs = load_json_default(
                _chunk_json_path(outdir, base, ci, fp), None
            ) or chunk_lists[ci]
            if not chunk_segs:
                continue
            # whisperx aligns in chunk-LOCAL time (0-based); subtract cstart.
            local_segs = [
                {**s, "start": round(s["start"] - cstart, 2),
                 "end": round(s["end"] - cstart, 2),
                 "words": [{"word": w["word"],
                            "start": round(w["start"] - cstart, 2),
                            "end": round(w["end"] - cstart, 2)}
                           for w in s.get("words", [])]}
                for s in chunk_segs
            ]
            wav = os.path.join(workdir(outdir, base), f"{base}.{fp}.chunk_{ci}.wav")
            extract_chunk(_extract_src, wav, cstart, cdur)
            try:
                aligned_local = _align.align_segments(
                    local_segs, wav, detected_language,
                    align_backend=align_backend, progress=progress,
                )
                # Re-add cstart to land back on the absolute timeline.
                aligned_abs = [
                    {**s, "start": round(s["start"] + cstart, 2),
                     "end": round(s["end"] + cstart, 2),
                     "words": [{"word": w["word"],
                                "start": round(w["start"] + cstart, 2),
                                "end": round(w["end"] + cstart, 2)}
                               for w in s.get("words", [])]}
                    for s in aligned_local
                ]
                save_json(acache, aligned_abs, indent=0)
                chunk_lists[ci] = aligned_abs
                progress(f"[align done] chunk {ci} segs={len(aligned_abs)}")
            finally:
                if os.path.exists(wav):
                    os.remove(wav)
        # Final cleanup of the alignment model.
        _align.release_align_memory()

    all_segs = merge_chunks(chunk_lists)
    out = os.path.join(workdir(outdir, base), f"{base}.segments_en.json")
    save_json(out, all_segs, indent=0)
    progress(f"[merge] total {len(all_segs)} segments -> {out}")
    return out


def _resolve_align_backend(
    requested: str,
    progress=print,
    *,
    allow_degrade: bool = False,
) -> str:
    """Resolve the effective alignment backend (裁决一).

    - ``none`` → none.
    - ``auto`` (default) → whisperx on hosts that can run it (CUDA + package),
      else degrade to none with an INFO reason (defaults may degrade; the
      reason is recorded in the state file).
    - ``whisperx`` (explicit) → must be honored: raise ``GateFail`` (→ exit 8)
      when unavailable. Explicit requests are NEVER silently degraded — unless
      ``allow_degrade`` (the ``--allow-degrade`` escape hatch) explicitly opts
      out, in which case it warns and falls back (reason still recorded).
    - a name outside ALIGN_BACKENDS → hard-stop (explicit misuse).

    Returns "none" when alignment cannot/should not run.
    """
    if requested == "none":
        return "none"
    if requested == "auto":
        # T4 默认化: this is the DEFAULT — run whisperx only when the host can do
        # it (CUDA + package), otherwise degrade silently (info, not a warning).
        if _align.whisperx_available() and torch.cuda.is_available():
            return "whisperx"
        print("[align] auto: whisperx alignment unavailable on this host "
              "(need NVIDIA CUDA + whisperx installed); using none.",
              file=sys.stderr)
        return "none"
    if requested not in _align.ALIGN_BACKENDS:
        return _gate_align_fail(
            f"unknown alignment backend {requested!r}",
            "choose one of: auto, none, whisperx",
            allow_degrade,
        )
    if not _align.whisperx_available():
        return _gate_align_fail(
            "--align whisperx requires WhisperX (NVIDIA CUDA + package), which "
            "is not available on this host. Explicit requests are never "
            "silently degraded.",
            "On a CUDA machine run `uv sync --extra gpu`. To proceed without "
            "alignment use --align auto/none, or explicitly bypass this gate "
            "with --allow-degrade.",
            allow_degrade,
        )
    return requested


def _gate_align_fail(message: str, guidance: str, allow_degrade: bool) -> str:
    """Hard-stop for an explicit-but-unhonorable align request (exit 8), unless
    the caller explicitly opted into degradation (which still leaves a trace)."""
    if allow_degrade:
        print(
            f"[align] WARNING: {message} --allow-degrade set, degrading to none "
            f"(reason recorded in state)",
            file=sys.stderr,
        )
        return "none"
    raise GateFail(message, guidance)


def _seg_to_dict(s, offset: float) -> dict[str, Any]:
    """Build a serializable segment dict from a faster-whisper Segment.

    Carries acoustic-confidence fields (V5: ADR-020) so the hallucination
    filter in merge.py can use them as a fifth signal without re-decoding.
    """
    seg: dict[str, Any] = {
        "start": round(s.start + offset, 2),
        "end": round(s.end + offset, 2),
        "text": to_single_line(s.text),   # ADR-040: 字幕文本单行不变量
        "words": [
            {"word": w.word, "start": round(w.start + offset, 2),
             "end": round(w.end + offset, 2)}
            for w in (s.words or [])
        ],
    }
    for fld in ("avg_logprob", "no_speech_prob", "compression_ratio"):
        v = getattr(s, fld, None)
        if v is not None:
            seg[fld] = v
    return seg


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
    # T2 (ADR-017): optional alternate audio source (vocals.wav after sep).
    # MUST be duration-matched to input_path (guard in caller / CLI).
    audio_source: str | None = None,
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
    _extract_src: str = audio_source if audio_source else input_path
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = tf.name
    try:
        extract_chunk(_extract_src, wav, start, dur)
        model = WhisperModel(resolve_model_path(model_name), device=dev,
                             compute_type=ct, cpu_threads=threads)
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
        out = [_seg_to_dict(s, start) for s in segs]
        return out
    finally:
        if os.path.exists(wav):
            os.remove(wav)
"""WhisperX forced-acoustic-alignment pass (Spec 22, ADR-028).

WHY A SEPARATE MODULE: alignment only *borrows* WhisperX's wav2vec2 alignment
capability — the transcription core stays on faster-whisper==1.2.1 (ADR-013 /
ADR-028 decision 1: "only borrow alignment, don't swap the core"). This keeps the
dependency-conflict surface minimal.

DESIGN:
- The alignment pass runs AFTER the chunk transcription loop and BEFORE
  merge_chunks (ADR-013 placement). It is a *separate* pass with its own cache
  layer ({base}.{transcribe_fp}.chunk_{ci}.whisperx.json) — it does NOT enter the
  transcribe_fingerprint (ADR-028 decision 2: avoid re-transcription on align
  toggle, keep golden byte-identical).
- GPU memory: Whisper is released (del + gc + empty_cache) before the wav2vec2
  model is loaded (loaded once, reused for all chunks), then released again
  (ADR-028 decision 3: 8GB red line, stepwise execution).
- Invariants (铁律 1): align_segments only rewrites words[].start/end (and the
  segment start/end derived from them). text / segment count / order / grouping /
  confidence fields are NEVER touched.
- Per-segment safe fallback (ADR-028 decision 4): if whisperx returns a different
  word count for a segment (rare re-tokenization), that segment keeps its DTW
  word timestamps + a warning; the rest are rewritten normally. Never fails the
  whole batch.
- Graceful degradation (铁律 2): explicit --align whisperx but unavailable
  (macOS / not installed / language lacks a wav2vec2 model) -> warning + fall
  back to none, process continues, exit code unchanged.
"""
from __future__ import annotations

import gc
import hashlib
import os
import sys
from typing import Any, Callable

import torch  # torch is already a core dependency (T2/demucs)

# Alignment backends. "none" is the default (no-op). "whisperx" uses WhisperX's
# wav2vec2 align. Other backends (e.g. stable-ts) are reserved for future use and
# are isolated purely by name in the cache file — adding one is zero-conflict.
ALIGN_BACKENDS = ("none", "whisperx")


def whisperx_available() -> bool:
    """Lazily probe whether WhisperX can be imported AND the host can run it.

    Returns False on macOS (no CUDA — whisperx alignment needs an NVIDIA GPU)
    and when the package is not installed. Never raises.
    """
    if sys.platform == "darwin":
        return False
    try:
        import whisperx  # noqa: F401  (import for side effects / availability)
        return True
    except Exception:  # noqa: BLE001 - any import failure -> unavailable
        return False


def align_fingerprint(transcribe_fp: str, language: str | None,
                     *, backend: str = "whisperx") -> str:
    """Stable fingerprint for the *independent* alignment cache layer.

    Decoupled from the transcribe_fingerprint (which it takes as an input, so a
    re-transcription invalidates the alignment cache too). Only changes with the
    backend, the language, or the underlying transcription fingerprint.
    """
    h = hashlib.sha1()
    h.update(transcribe_fp.encode("utf-8"))
    h.update(b"|")
    h.update((language or "auto").encode("utf-8"))
    h.update(b"|")
    h.update(backend.encode("utf-8"))
    return h.hexdigest()[:12]


def release_align_memory() -> None:
    """Release the wav2vec2 model (and any CUDA tensors) before/after alignment.

    Mirrors T2's vocal_sep GPU cleanup: del + gc + torch.cuda.empty_cache.
    No-op safe on CPU.
    """
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _align_cache_path(outdir: str, base: str, ci: int, fp: str) -> str:
    """Per-chunk alignment cache path. Backend is baked into the name so different
    backends never collide (ADR-028 decision 2)."""
    return os.path.join(outdir, f"{base}.{fp}.chunk_{ci}.whisperx.json")


def align_cache_path(outdir: str, base: str, ci: int, fp: str) -> str:
    """Public alias for _align_cache_path (used by transcribe_video)."""
    return _align_cache_path(outdir, base, ci, fp)


def _ensure_nltk_on_path() -> None:
    """Register the project-local NLTK corpora before whisperx reaches for them.

    whisperx sentence-splits with nltk, which needs the punkt corpora. `setup
    --align` downloads them into <repo>/models/nltk_data; putting that dir on
    nltk's search path here means the align pass works with no NLTK_DATA env var
    and no per-machine fiddling. Without it whisperx raises LookupError and the
    pass silently degrades to DTW timestamps — it "succeeds" while changing
    nothing, which is the worst kind of failure.
    """
    try:
        from .toolchain import register_nltk_path
    except Exception:  # noqa: BLE001
        return
    try:
        register_nltk_path()
    except Exception:  # noqa: BLE001
        pass


def align_segments(
    segments: list[dict[str, Any]],
    audio_path: str,
    language: str | None,
    *,
    align_backend: str = "whisperx",
    progress: Callable[..., None] = print,
) -> list[dict[str, Any]]:
    """Force-align word-level timestamps for a single chunk's segments in place
    (returns a new list; inputs are not mutated).

    Invariants (铁律 1): text / segment count / order / grouping / confidence
    fields are preserved. Only ``words[].start/end`` (and segment ``start/end``
    derived from them) are rewritten.

    ``audio_path`` is a chunk-local wav already shifted to 0-based time; the
    caller is responsible for adding the chunk start offset back to the absolute
    timeline if needed.
    """
    import whisperx  # lazy: heavy import, only on the align path

    if align_backend != "whisperx":
        return [dict(s) for s in segments]

    _ensure_nltk_on_path()

    if not segments:
        return []

    # Load alignment model once for this chunk (cheap relative to transcription).
    load_kwargs: dict[str, Any] = {}
    if language:
        load_kwargs["language_code"] = language
    try:
        align_model, metadata = whisperx.load_align_model(device="cuda",
                                                          **load_kwargs)
    except Exception as exc:  # noqa: BLE001 - language has no wav2vec2 model, etc.
        msg = (f"[align] WARNING: alignment model unavailable "
               f"({'lang=' + language if language else 'auto'}): {exc}; "
               f"keeping DTW word timestamps")
        print(msg, file=sys.stderr)
        progress(msg)
        return [dict(s) for s in segments]

    def _norm(w: dict[str, Any]) -> str:
        """Whitespace/case-insensitive word key.

        whisperx re-normalises text (it strips the leading space faster-whisper
        keeps, and folds curly quotes/dashes), so compare loosely.
        """
        s = str(w.get("word", "")).strip().lower()
        return (s.replace("\u2019", "'").replace("\u2018", "'")
                 .replace("\u201c", '"').replace("\u201d", '"')
                 .replace("\u2013", "-").replace("\u2014", "-"))

    def _aligned_words_for(seg: dict[str, Any]) -> list[dict[str, Any]]:
        """Align ONE segment and return its flat aligned word list ([] on failure).

        Per-segment calls are deliberate. A single batched call is unusable
        against faster-whisper output for two reasons:
          1. whisperx >=3.8 re-splits its input on sentence boundaries, so the
             returned `segments` no longer line up 1:1 with our input;
          2. its word stream is not a strict 1:1 mirror of ours — it merges some
             tokens (245 DTW words came back as 243 here), and `word_segments` is
             time-ordered while our segments are not (fill_gaps appends recovered
             segments at the end).
        Any of that drifts, and every segment after the drift point is mispaired
        — the whole pass silently degrades to "keep DTW". Aligning per segment
        contains a tokenisation difference to the one segment that has it: it
        keeps its DTW timestamps (ADR-028 decision 4) while the rest still get
        refined.
        """
        try:
            res = whisperx.align(
                [{"text": seg["text"], "start": seg["start"],
                  "end": seg["end"], "words": seg.get("words", [])}],
                align_model, metadata, audio, device="cuda",
                return_char_alignments=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[align] WARNING: align failed for segment "
                  f"{seg.get('text', '')!r}: {exc}; keeping DTW timestamps",
                  file=sys.stderr)
            return []
        words = list(res.get("word_segments") or [])
        if not words:  # other builds nest words inside segments
            for aseg in res.get("segments", []):
                words.extend(list(aseg.get("words") or []))
        return words

    try:
        audio = whisperx.load_audio(audio_path)
    except Exception as exc:  # noqa: BLE001
        msg = f"[align] WARNING: could not load audio {audio_path!r}: {exc}; " \
              f"keeping DTW word timestamps"
        print(msg, file=sys.stderr)
        progress(msg)
        return [dict(s) for s in segments]

    try:
        out: list[dict[str, Any]] = []
        for orig in segments:
            new_seg = dict(orig)  # shallow copy; preserve all original fields
            owords = list(orig.get("words") or [])
            if not owords:
                out.append(new_seg)
                continue
            a_words = _aligned_words_for(orig)
            take = a_words[:len(owords)]
            # Same count AND same words — otherwise this segment's alignment is
            # not trustworthy (re-tokenisation, OOV digits/symbols...).
            if len(take) != len(owords) or any(
                _norm(o) != _norm(a) for o, a in zip(owords, take)
            ):
                # Per-segment safe fallback (ADR-028 decision 4): keep DTW words.
                msg = (f"[align] WARNING: word mismatch "
                       f"({len(owords)} DTW vs {len(take)} aligned) for segment "
                       f"{orig.get('text', '')!r}; keeping DTW timestamps")
                print(msg, file=sys.stderr)
                progress(msg)
                out.append(new_seg)
                continue
            new_words = []
            for ow, aw in zip(owords, take):
                # whisperx can emit None for words it could not align (digits,
                # symbols outside the align model's dictionary) — keep ours then.
                astart, aend = aw.get("start"), aw.get("end")
                new_words.append({
                    "word": ow["word"],
                    "start": round(astart, 2) if astart is not None else ow.get("start"),
                    "end": round(aend, 2) if aend is not None else ow.get("end"),
                })
            new_seg["words"] = new_words
            new_seg["start"] = new_words[0]["start"]
            new_seg["end"] = new_words[-1]["end"]
            out.append(new_seg)
        return out
    finally:
        # release wav2vec2 tensors promptly (chunk-local)
        del align_model
        release_align_memory()

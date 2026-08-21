"""Command-line interface for video-translate (V2).

Subcommands: transcribe / translate / generate / run / setup / doctor / backfill.

Exit codes:
    0  success
    1  runtime error
    2  argument error (argparse default)
    3  missing dependency (ffmpeg / HF model)
    4  proxy error (e.g. SOCKS proxy given)
    5  transcription killed (SIGKILL); some chunks completed, safe to re-run
    6  awaiting agent action (transcribe + task done; agent must translate)
    7  doctor --strict: a required environment check failed (e.g. Google
       translate endpoint unreachable). Only raised with --strict; doctor
       otherwise prints [MISS] and still returns 0.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import DEFAULT_HF_CACHE, resolve_config
from .io_utils import load_json, save_json
from .proxy import detect_proxy, setup_http_proxy
from .audio_profile import analyze_audio
from .toolchain import init_toolchain
from .verify import (
    UNCOVERED_AUDIO, find_uncovered_speech, find_untranslated_latin_words,
    verify_acoustic, verify_presentation,
)
from .translate import validate_zh
from .verify_align import report as align_report

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_ARGS = 2
EXIT_MISSING_DEP = 3
EXIT_PROXY = 4
EXIT_KILLED = 5
EXIT_AWAITING_AGENT = 6
EXIT_DOCTOR_FAIL = 7


# --------------------------- helpers ---------------------------

def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def _hf_cache_dir() -> str:
    return os.environ.get("HF_HOME", DEFAULT_HF_CACHE)


def _model_cached(model_name: str = "large-v3") -> bool:
    """Is a faster-whisper model present (in-repo OR HF cache), file-complete?

    Checks for model.bin so an incomplete snapshot (dir present but model.bin
    missing) is NOT falsely reported as cached.
    """
    # 1) in-repo local model dir
    cand = os.path.join(_LOCAL_MODEL_DIR, model_name)
    if os.path.isfile(os.path.join(cand, "model.bin")):
        return True
    # 2) HF hub snapshot with model.bin present
    hub = os.path.join(_hf_cache_dir(), "hub")
    if not os.path.isdir(hub):
        return False
    needle = model_name.replace("/", "--").lower()
    for d in os.listdir(hub):
        if needle in d.lower():
            snap_root = os.path.join(hub, d, "snapshots")
            if not os.path.isdir(snap_root):
                continue
            for snap in os.listdir(snap_root):
                if os.path.isfile(os.path.join(snap_root, snap, "model.bin")):
                    return True
    return False


# Project-local model dir: <repo_root>/models/<name>. Lets users drop a model
# in-repo (e.g. from a mirror) and bypass HF Hub / network entirely.
# cli.py lives at <repo>/src/video_translate/cli.py → repo root is three dirs up.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_LOCAL_MODEL_DIR = os.path.join(_REPO_ROOT, "models")


def _resolve_model_path(model_name: str) -> str:
    """Return a local in-repo model dir if it holds model.bin, else pass through.

    This lets `--model large-v3` resolve to `<repo>/models/large-v3` (which
    contains model.bin) instead of forcing an HF Hub download. Faster-whisper's
    WhisperModel accepts either a repo-id or a local directory path.
    """
    if os.path.sep not in model_name and not os.path.isabs(model_name):
        cand = os.path.join(_LOCAL_MODEL_DIR, model_name)
        if os.path.isfile(os.path.join(cand, "model.bin")):
            return cand
    return model_name


def _cuda_available() -> bool:
    return _has("nvidia-smi")


def _default_outdir(input_path: str) -> str:
    """V2: default output dir = the video's own directory."""
    return str(Path(input_path).parent)


def _default_base(input_path: str) -> str:
    """V2: default base = the video filename stem."""
    return Path(input_path).stem


def _derive_base(path: str) -> str:
    """Derive <base> from a segments/zh file path (strips known suffixes)."""
    name = Path(path).name
    for suffix in (".segments_en.json", ".segments_raw.json", ".zh_segments.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def _resolve_proxy(args: argparse.Namespace) -> str | None:
    """Resolve proxy from --no-proxy / --proxy / env / TCP probe."""
    cli_proxy = getattr(args, "proxy", None)
    cli_no_proxy = getattr(args, "no_proxy", False)
    return detect_proxy(cli_proxy=cli_proxy, cli_no_proxy=cli_no_proxy)


def _translate_proxy(args: argparse.Namespace) -> str | None:
    """Resolve + apply the HTTP proxy, but ONLY for the google translate engine.

    The default ``agent`` engine does NOT make any network call — the calling
    agent (WorkBuddy / Claude Code / ...) does the translation — so for it we
    never touch proxy env vars and never probe Google. Returns the resolved
    proxy url (or None for a direct connection). Raises ValueError (-> exit 4)
    on a SOCKS proxy, which would break huggingface_hub's httpx client.
    """
    proxy = _resolve_proxy(args)
    try:
        setup_http_proxy(proxy)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        raise
    return proxy


# --------------------------- doctor / setup ---------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    """Report environment readiness. Returns 0 unless --strict and a check fails."""
    strict = getattr(args, "strict", False)
    toolchain = init_toolchain(force=strict)
    print(f"video-translate {__version__} — environment check\n")

    if toolchain.loaded_files:
        files_str = ", ".join(os.path.basename(f) for f in toolchain.loaded_files)
        print(f"  [OK ] env config   : {files_str}")
    else:
        print(f"  [INFO] env config   : default (no .env file loaded)")

    checks = [
        ("ffmpeg", _has("ffmpeg")),
        ("ffprobe", _has("ffprobe")),
        (f"HF cache dir ({_hf_cache_dir()})", os.path.isdir(_hf_cache_dir())),
        ("large-v3 model cached (reuse, no re-download)", _model_cached("large-v3")),
    ]
    failed = False
    for name, ok in checks:
        print(f"  [{'OK ' if ok else 'MISS'}] {name}")
        if not ok:
            failed = True

    cfg = resolve_config(cwd=os.getcwd())
    from .transcribe import resolve_device
    dev, ct = resolve_device(cfg.device, cfg.compute_type)
    print(f"\n  device        : {dev} (configured '{cfg.device}'; "
          f"CUDA {'yes' if _cuda_available() else 'no'})")
    print(f"  compute_type  : {ct} (configured '{cfg.compute_type}')")
    print(f"  NVIDIA CUDA   : {'yes' if _cuda_available() else 'no (CPU-only path)'}")

    try:
        import faster_whisper  # noqa: F401
        print(f"  faster-whisper: installed")
    except Exception:
        print(f"  faster-whisper: NOT installed (pip install -r requirements.txt)")
    try:
        import deep_translator  # noqa: F401
        print(f"  deep-translator: installed")
    except Exception:
        print(f"  deep-translator: NOT installed")
    # T2 / ADR-017: demucs status (vocal separation preprocessing, now a core dep)
    try:
        from .vocal_sep import demucs_available
        if demucs_available():
            import torch as _torch  # type: ignore
            _on_gpu = _cuda_available()
            if _on_gpu:
                print(f"  demucs (htdemucs): OK — GPU available (separate vocals --separate-vocals)")
            else:
                print(f"  demucs (htdemucs): CPU only — separation ~10x slower; GPU RECOMMENDED")
        else:
            print(f"  demucs         : not installed — install for vocal/BGM separation:\n"
                  f"                    pip install -e .   (core dependency)")
    except Exception:
        print(f"  demucs         : probe failed — vocal separation unavailable")
    print(f"  engine        : {cfg.engine}")
    print(f"  lang          : {'auto-detect' if cfg.lang is None else cfg.lang}")

    # The default `agent` engine translates locally via the calling agent and
    # never touches the network — so we do NOT probe Google for it. Only the
    # headless `google` engine needs the proxy + endpoint reachability check.
    if cfg.engine == "google":
        print(f"  proxy         : auto-detect (--no-proxy for direct)")
        proxy = detect_proxy()
        try:
            from .proxy import _probe_google_endpoint
            reachable = _probe_google_endpoint(proxy)
        except Exception:
            reachable = False
        tag = "via proxy" if proxy else "via direct"
        print(f"  Google translate endpoint ({tag}): {'OK' if reachable else 'MISS'}")
        if not reachable:
            failed = True
    else:
        print(f"  proxy         : n/a (agent engine translates locally; no network)")

    # ADR-012 / ADR-017: audio profile + automatic VAD & vocal separation recommendation.
    video = getattr(args, "video", None)
    if video:
        try:
            from .audio_profile import analyze_audio, recommend_vad
            prof = analyze_audio(video)
            if prof.ok:
                flag, rationale = recommend_vad(prof)
                print(f"\n  audio profile : mean={prof.mean_vol} dB, max={prof.max_vol} dB, "
                      f"{len(prof.silence_intervals)} silence gap(s)")
                print(f"  VAD routing   : {flag}")
                print(f"                  ({rationale})")
                # ADR-015: a clean-but-continuous (low silence fraction) profile
                # means speech sits under laughter/cheer/music — prefer per-chunk
                # adaptive routing over a single global VAD decision.
                from .audio_profile import _silence_fraction, CLEAN_SILENCE_FRACTION
                from .ffmpeg_utils import probe_duration
                dur = probe_duration(video)
                sf = _silence_fraction(prof.silence_intervals, dur) if dur else 0.0
                if flag and sf < CLEAN_SILENCE_FRACTION:
                    print(f"  note          : low silence fraction ({sf:.2f} < "
                          f"{CLEAN_SILENCE_FRACTION}) suggests continuous noise — "
                          f"consider --adaptive-vad for per-chunk routing")
                # ADR-017 / T2: recommend --separate-vocals if continuous noise or heavy background
                from .vocal_sep import demucs_available
                if sf < CLEAN_SILENCE_FRACTION:
                    if demucs_available():
                        print(f"  vocal separation: RECOMMENDED (--separate-vocals) — high density audio detected")
                    else:
                        print(f"  vocal separation: RECOMMENDED but demucs not installed (pip install -e .)")
            else:
                print(f"\n  audio profile : unavailable (ffmpeg profile failed; default bare run)")
        except Exception as e:  # noqa: BLE001
            print(f"\n  audio profile : unavailable ({e}); default bare run")

    if strict and failed:
        return EXIT_DOCTOR_FAIL
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """Ensure the HF model is present; download it if missing (reuse if present)."""
    model = args.model
    if _model_cached(model):
        print(f"[setup] {model} already cached in {_hf_cache_dir()} — reusing, no download.")
        return EXIT_OK
    print(f"[setup] {model} not found; downloading into {_hf_cache_dir()} (~3GB for large-v3)...")
    proxy = _resolve_proxy(args)
    try:
        setup_http_proxy(proxy)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY
    try:
        from faster_whisper import WhisperModel
        from .transcribe import resolve_device
        dev, ct = resolve_device(getattr(args, "device", None),
                                 getattr(args, "compute_type", None))
        WhisperModel(model, device=dev, compute_type=ct)  # triggers download
        print(f"[setup] {model} ready.")
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] model download failed: {e}", file=sys.stderr)
        return EXIT_MISSING_DEP


# --------------------------- pipeline subcommands ---------------------------

def _require_ffmpeg() -> int | None:
    if not (_has("ffmpeg") and _has("ffprobe")):
        print("[error] ffmpeg/ffprobe not found in PATH", file=sys.stderr)
        return EXIT_MISSING_DEP
    return None


def _vocal_sep_step(
    args: argparse.Namespace, cfg, input_path: str, outdir: str, base: str,
) -> tuple[bool, str | None, str, str, str | None]:
    """Run vocal separation BEFORE loading Whisper (8GB GPU safe).

    Spec 19 / ADR-017 §5: we MUST run demucs FIRST, release ALL demucs GPU
    memory, THEN start Whisper — otherwise two big models on an 8GB card OOM.

    Returns:
      (do_separate_was_requested_flag,
       audio_source_path_or_None,
       vsep_backend ("demucs"),
       vsep_model,
       vsep_input_hash_or_None)
    """
    sep = bool(getattr(args, "separate_vocals", False) or getattr(cfg, "separate_vocals", False))
    dm_model = (
        getattr(args, "demucs_model", None)
        or getattr(cfg, "demucs_model", None)
        or "htdemucs"
    )
    backend = "demucs"
    if not sep:
        return False, None, backend, dm_model, None
    # user explicitly asked for separation — probe & try
    from .vocal_sep import demucs_available, separate_vocals, separate_fingerprint, _input_fingerprint
    if not demucs_available():
        print("[warn] --separate-vocals requested but 'demucs' package not installed.\n"
              "       To enable: pip install -e .   (core dependency)\n"
              "       (CPU fallback is very slow; GPU recommended).\n"
              "       Falling back to original audio.")
        return False, None, backend, dm_model, None
    try:
        audio_source = separate_vocals(
            input_path, outdir, base=base,
            backend=backend, model_name=dm_model,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] demucs separation failed ({exc}); falling back to original audio",
              file=sys.stderr)
        audio_source = None
    if audio_source is None:
        # Separation failed or demucs didn't actually run (e.g. backend mismatch)
        return False, None, backend, dm_model, None
    vsep_input_hash = _input_fingerprint(input_path)
    # Force-assert the duration invariant (ADR-017 §2 load-bearing guard)
    try:
        from .ffmpeg_utils import probe_duration
        din = probe_duration(input_path)
        dout = probe_duration(audio_source)
        if abs(din - dout) >= 0.05:
            print(f"[warn] demucs output duration {dout:.3f}s ≠ input {din:.3f}s;\n"
                  f"       refusing to shift timestamps — falling back to original audio")
            return False, None, backend, dm_model, None
    except Exception:
        # profile failed — be safe and keep the separation output only if
        # demucs itself already enforced the invariant inside separate_vocals;
        # here we just trust the step.
        pass
    # Spec 19 / ADR-017 §5 — EXPLICIT demucs / torch GPU memory release BEFORE
    # WhisperModel is constructed anywhere downstream:
    try:
        import gc
        gc.collect()
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    except Exception:
        pass
    return True, audio_source, backend, dm_model, vsep_input_hash


def cmd_transcribe(args: argparse.Namespace) -> int:
    dep = _require_ffmpeg()
    if dep is not None:
        return dep
    input_path = args.input
    outdir = args.outdir or _default_outdir(input_path)
    base = args.base or _default_base(input_path)
    cfg = resolve_config(
        {"model": args.model, "chunk": args.chunk, "lang": args.lang,
         "merge_max_chars": getattr(args, "merge_max_chars", None),
         "device": getattr(args, "device", None),
         "compute_type": getattr(args, "compute_type", None),
         "separate_vocals": getattr(args, "separate_vocals", None),
         "demucs_model": getattr(args, "demucs_model", None)},
        cwd=os.getcwd(),
    )
    cfg.model = _resolve_model_path(cfg.model)

    # T2 (ADR-017 / Spec 19) — vocal separation MUST happen FIRST, before any
    # heavy Whisper import (8GB GPU sequential scheduling). If this returns
    # (False, None, ...) we run on the normal path with zero behaviour change.
    _sep_on, _audio_src, _vsep_backend, _vsep_model, _vsep_ihash = _vocal_sep_step(
        args, cfg, input_path, outdir, base,
    )

    try:
        from .transcribe import transcribe_video
        transcribe_video(
            input_path, outdir, base=base, model_name=cfg.model,
            chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
            vad_threshold=getattr(args, "vad_threshold", None),
            use_vad=getattr(args, "vad", False),
            adaptive_vad=getattr(args, "adaptive_vad", False),
            device=cfg.device, compute_type=cfg.compute_type,
            # T2 fields: only non-default when separation was actually used.
            audio_source=_audio_src,
            separate_vocals=_sep_on,
            vocal_sep_backend=_vsep_backend,
            vocal_sep_model=_vsep_model,
            vocal_sep_input_hash=_vsep_ihash,
        )
        segs_path = os.path.join(outdir, f"{base}.segments_en.json")
        # ADR-012: compute the independent silence reference ONCE and share it
        # with both the hallucination filter (merge) and the gap audit.
        # Spec 19 Invariant #4: this reference ALWAYS consults the original
        # input_path (never a cleaned audio_source) — no change here.
        silences: list[tuple[float, float]] | None = None
        try:
            prof = analyze_audio(input_path)
            silences = prof.silence_intervals if prof.ok else None
        except Exception:  # noqa: BLE001
            silences = None
        if cfg.merge_enabled and not args.no_merge:
            from .merge import apply_merge
            raw_path = os.path.join(outdir, f"{base}.segments_raw.json")
            apply_merge(
                segs_path, raw_path=raw_path,
                max_dur=cfg.merge_max_dur, max_gap=cfg.merge_max_gap,
                split_enabled=not getattr(args, "no_split", False),
                split_max_chars=cfg.merge_max_chars,
                snap_drift=not getattr(args, "no_drift_snap", False),
                silence_intervals=silences,
            )
            print(f"[merge] merged + split -> {segs_path} (raw kept at {raw_path})")
        # B: coverage self-audit + automatic gap recovery (skip with --no-audit)
        if not getattr(args, "no_audit", False):
            from .fill_gaps import fill_gaps
            from .io_utils import load_json, save_json
            segs = load_json(segs_path)
            recovered = fill_gaps(
                input_path, segs, lang=cfg.lang,
                model_name=cfg.model,
                use_vad=False,  # ADR-016 (T2a): recovery is always bare
                silence_intervals=silences,
                device=cfg.device, compute_type=cfg.compute_type,
                # T2 / Spec 19 §(B): recovery decodes from the SAME source as
                # the main pass — either vocals.wav (if used) or original video.
                audio_source=_audio_src,
            )
            if recovered is not segs:
                save_json(segs_path, recovered, indent=0)
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] transcription failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_translate(args: argparse.Namespace) -> int:
    cfg = resolve_config(
        {"proxy": args.proxy, "src": args.src, "tgt": args.tgt, "engine": args.engine,
         "glossary": getattr(args, "glossary", None),
         "source": getattr(args, "source", None)},
        cwd=os.getcwd(),
    )
    segments, out = args.segments, args.out

    if cfg.engine == "agent":
        from .translate import prepare_translate_task
        out_name = os.path.basename(out)
        if out_name.endswith(".zh_segments.json"):
            base_name = out_name[:-len(".zh_segments.json")]
        else:
            base_name = os.path.splitext(out_name)[0]
        task_path = os.path.join(os.path.dirname(out), f"{base_name}.translate_task.json")
        glossary_text = None
        if cfg.glossary:
            from .glossary import load_glossary
            glossary_text = load_glossary(cfg.glossary)
        prepare_translate_task(segments, task_path, persona=cfg.persona,
                                glossary=glossary_text, source=cfg.source,
                                full_transcript=cfg.full_transcript)
        base = _derive_base(segments)
        outdir = str(Path(out).parent)
        print(_AGENT_TRANSLATE_INSTRUCTIONS.format(
            task=task_path, segments=segments, out=out, outdir=outdir, base=base))
        return EXIT_AWAITING_AGENT

    # google engine (headless fallback) — the ONLY path that needs a proxy
    try:
        proxy = _translate_proxy(args)
    except ValueError:
        return EXIT_PROXY
    try:
        from .translate import translate_segments
        translate_segments(
            segments, out, pending_path=args.pending,
            proxy=proxy, src=cfg.src, tgt=cfg.tgt,
        )
        return EXIT_OK
    except ValueError:  # SOCKS proxy (already reported)
        return EXIT_PROXY
    except Exception as e:  # noqa: BLE001
        print(f"[error] translation failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_generate(args: argparse.Namespace) -> int:
    base = args.base or _derive_base(args.segments)
    gap = getattr(args, "gap", 0.0) or 0.0
    min_dur = getattr(args, "min_dur", 0.0) or 0.0
    offset = getattr(args, "offset", 0.0) or 0.0
    tail = getattr(args, "tail", 0.0) or 0.0
    flat = getattr(args, "flat", False)
    prune_old = getattr(args, "prune_old", False)

    # V11: agent translation is written batch-by-batch keyed by segment index;
    # one skipped line shifts every later translation while the English track
    # stays correct — invisible in logs, obvious to a viewer. Catch it before
    # rendering. Warning only; --no-align-check silences it.
    if not getattr(args, "no_align_check", False):
        try:
            import json as _json
            from .verify_align import report as _align_report
            with open(args.segments, encoding="utf-8") as _f:
                _segs = _json.load(_f)
            with open(args.zh, encoding="utf-8") as _f:
                _zh = _json.load(_f)
            if not _align_report(_segs, _zh):
                print("[align] verify the flagged range before shipping "
                      "(--no-align-check to silence)", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[align] check skipped ({e})", file=sys.stderr)

    try:
        from .generate import generate_subtitles
        generate_subtitles(args.segments, args.zh, args.outdir, base=base,
                           gap=gap, min_dur=min_dur, offset=offset, tail=tail,
                           flat=flat, prune_old=prune_old)
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] generate failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline: transcribe -> translate -> generate.

    With ``--engine agent`` (default), stops after transcribe + task emission and
    returns EXIT_AWAITING_AGENT (6); the calling agent translates and runs
    ``generate``. With ``--engine google`` it runs end-to-end.
    """
    skip = set(args.skip or [])
    input_path = args.input
    outdir = args.outdir or _default_outdir(input_path)
    base = args.base or _default_base(input_path)
    segments = os.path.join(outdir, f"{base}.segments_en.json")
    zh = os.path.join(outdir, f"{base}.zh_segments.json")
    pending = os.path.join(outdir, f"{base}.agent_pending.json")
    task = os.path.join(outdir, f"{base}.translate_task.json")
    cfg = resolve_config(
        {"model": args.model, "chunk": args.chunk, "lang": args.lang,
         "proxy": args.proxy, "src": args.src, "tgt": args.tgt,
         "engine": args.engine, "merge_max_chars": getattr(args, "merge_max_chars", None),
         "glossary": getattr(args, "glossary", None),
         "source": getattr(args, "source", None),
         "device": getattr(args, "device", None),
         "compute_type": getattr(args, "compute_type", None)},
        cwd=os.getcwd(),
    )

    if "transcribe" not in skip:
        rc = cmd_transcribe(argparse.Namespace(
            input=input_path, outdir=outdir, base=base,
            model=cfg.model, chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
            proxy=args.proxy, no_proxy=args.no_proxy, no_merge=args.no_merge,
            no_split=getattr(args, "no_split", False),
            merge_max_chars=cfg.merge_max_chars,
            vad_threshold=getattr(args, "vad_threshold", None),
            vad=getattr(args, "vad", False),
            adaptive_vad=getattr(args, "adaptive_vad", False),
            no_audit=getattr(args, "no_audit", False),
            no_drift_snap=getattr(args, "no_drift_snap", False),
            device=cfg.device, compute_type=cfg.compute_type,
            # T2 / ADR-017: forward the vocal-separation flags verbatim
            separate_vocals=getattr(args, "separate_vocals", False),
            demucs_model=getattr(args, "demucs_model", None),
        ))
        if rc != EXIT_OK:
            return rc

    if cfg.engine == "agent" and "translate" not in skip:
        from .translate import prepare_translate_task
        glossary_text = None
        if cfg.glossary:
            from .glossary import load_glossary
            glossary_text = load_glossary(cfg.glossary)
        prepare_translate_task(segments, task, persona=cfg.persona,
                                glossary=glossary_text, source=cfg.source,
                                full_transcript=cfg.full_transcript)
        print(_RUN_AWAITING_AGENT_INSTRUCTIONS.format(
            task=task, segments=segments, zh=zh, outdir=outdir, base=base))
        return EXIT_AWAITING_AGENT

    if "translate" not in skip:
        rc = cmd_translate(argparse.Namespace(
            segments=segments, out=zh, pending=pending,
            proxy=args.proxy, no_proxy=args.no_proxy,
            src=cfg.src, tgt=cfg.tgt, engine="google",
        ))
        if rc != EXIT_OK:
            return rc

    if "generate" not in skip:
        rc = cmd_generate(argparse.Namespace(
            segments=segments, zh=zh, outdir=outdir, base=base,
            gap=getattr(args, "gap", 0.0) or 0.0,
            min_dur=getattr(args, "min_dur", 1.0),
            offset=getattr(args, "offset", 0.0) or 0.0,
            tail=getattr(args, "tail", 0.3),
            flat=getattr(args, "flat", False),
            prune_old=getattr(args, "prune_old", False),
        ))
        if rc != EXIT_OK:
            return rc
    return EXIT_OK


def cmd_resegment(args: argparse.Namespace) -> int:
    """Re-transcribe given time windows with a forced language and splice
    the clean segments back into the existing segments_en.json.

    Used to fix mis-detected language spans (e.g. Japanese lines in an
    otherwise-English trailer that Whisper heard as English gibberish) without
    re-running the whole video. The re-transcribed segments are tagged with a
    ``lang`` field so the translation step can branch per segment.
    """
    dep = _require_ffmpeg()
    if dep is not None:
        return dep
    segs_path = args.segments
    if not os.path.exists(segs_path):
        print(f"[error] segments file not found: {segs_path}", file=sys.stderr)
        return EXIT_RUNTIME
    segments = load_json(segs_path)
    windows: list[tuple[float, float]] = []
    for w in args.windows:
        try:
            s, e = w.split("-")
            windows.append((float(s), float(e)))
        except ValueError:
            print(f"[error] bad window '{w}', expected 'start-end' (seconds)",
                  file=sys.stderr)
            return EXIT_RUNTIME
    windows.sort()

    # T2 / ADR-017: resegment respects --separate-vocals. Unlike `transcribe` /
    # `run`, resegment never *performs* the separation itself (it's a quick
    # manual-fix command), but if a vocals.wav cache was produced during a
    # previous --separate-vocals run in the SAME output dir as segments.json,
    # resegment will reuse it as the audio source so the re-decoded windows
    # are consistent with the rest of the timeline.
    cfg = resolve_config(
        {"separate_vocals": getattr(args, "separate_vocals", None),
         "demucs_model": getattr(args, "demucs_model", None)},
        cwd=os.getcwd(),
    )
    audio_source: str | None = None
    if cfg.separate_vocals:
        from .vocal_sep import (
            demucs_available, separate_fingerprint, vocals_wav_path,
        )
        if demucs_available():
            outdir = os.path.dirname(segs_path) or "."
            base = os.path.splitext(os.path.basename(segs_path))[0]
            # segments.json is named "{base}.segments_en.json" — strip that suffix
            if base.endswith(".segments_en"):
                base = base[: -len(".segments_en")]
            dm = cfg.demucs_model or "htdemucs"
            fp = separate_fingerprint(args.video, "demucs", dm)
            candidate = vocals_wav_path(outdir, base, fp)
            if os.path.isfile(candidate):
                audio_source = candidate
                print(f"[resegment] using cached vocals.wav ({fp[:8]}…)")
            else:
                print(
                    "[warn] --separate-vocals on resegment but no cached "
                    "vocals.wav found. Run 'transcribe --separate-vocals' first "
                    "to produce it. Falling back to original video audio."
                )
        else:
            print(
                "[warn] --separate-vocals requested but 'demucs' package not "
                "installed; falling back to original video audio."
            )

    from .transcribe import transcribe_window
    new_segs: list[dict[str, Any]] = []
    for (ws, we) in windows:
        print(f"[resegment] window {ws:.1f}-{we:.1f}s lang={args.lang} ...",
              flush=True)
        window_segs = transcribe_window(
            args.video, ws, we, lang=args.lang,
            use_vad=getattr(args, "vad", False),
            model_name=args.model, threads=args.threads,
            device=getattr(args, "device", None),
            compute_type=getattr(args, "compute_type", None),
            audio_source=audio_source,  # T2: vocals.wav if available
        )
        for seg in window_segs:
            seg = dict(seg)
            seg["lang"] = args.lang
            new_segs.append(seg)
        print(f"    -> {len(window_segs)} clean segment(s)", flush=True)

    # drop originals overlapping any window, then merge + sort by start
    kept = [
        seg for seg in segments
        if not any(seg["start"] < we and seg["end"] > ws for (ws, we) in windows)
    ]
    merged = kept + new_segs
    merged.sort(key=lambda s: s["start"])
    save_json(segs_path, merged, indent=0)
    print(f"[resegment] done: {len(segments)} -> {len(merged)} segments "
          f"({len(new_segs)} re-transcribed as '{args.lang}') -> {segs_path}")
    return EXIT_OK


def cmd_backfill(args: argparse.Namespace) -> int:
    """Backfill agent_pending segments via the agent engine.

    Two modes:
      - prepare (default): read pending, emit <base>.backfill_task.json (indices
        preserved from pending), return EXIT_AWAITING_AGENT (6).
      - merge (--agent-zh PATH): merge agent-filled zh into zh_segments.json and
        run generate, return EXIT_OK.
    """
    if not args.pending or not os.path.exists(args.pending):
        print(f"[backfill] --pending <path> required (got {args.pending!r})",
              file=sys.stderr)
        return EXIT_ARGS
    cfg = resolve_config(cwd=os.getcwd())
    out = args.out
    outdir = args.outdir or os.path.dirname(out) or "."
    base = args.base or _derive_base(out)

    # merge mode: agent already filled the task
    if args.agent_zh:
        from .translate import merge_agent_zh
        merge_agent_zh(out, args.agent_zh)
        if args.segments:
            return cmd_generate(argparse.Namespace(
                segments=args.segments, zh=out, outdir=outdir, base=base))
        print(f"[backfill] merged {args.agent_zh} into {out} (skip generate: no --segments)")
        return EXIT_OK

    pending = load_json(args.pending)
    if not pending:
        print("[backfill] nothing to do (pending empty)")
        return EXIT_OK

    # prepare mode: pending items already carry their original `index`
    tmp_segs = os.path.join(outdir, "_backfill_segments.json")
    save_json(tmp_segs, pending, indent=0)
    task_path = os.path.join(outdir, f"{base}.backfill_task.json")
    from .translate import prepare_translate_task
    prepare_translate_task(tmp_segs, task_path, persona=cfg.persona,
                           index_key="index")
    segs_hint = args.segments or "<segments_en.json>"
    print(_BACKFILL_INSTRUCTIONS.format(
        task=task_path, pending=args.pending, out=out,
        segments=segs_hint, outdir=outdir, base=base))
    return EXIT_AWAITING_AGENT


_AGENT_TRANSLATE_INSTRUCTIONS = """\
[AWAITING_AGENT] translation task written to:
  {task}
AGENT ACTION REQUIRED:
  1. Read the task file. FIRST read `full_transcript` (whole scene) + `source`
     + `guidelines`, THEN translate each batch's `to_translate` items to
     Chinese following the `persona`.
  2. Save as {out} — a JSON object mapping str(index) -> zh, covering every
     index in to_translate[*].index.
  3. Run: video-translate generate --segments {segments} --zh {out} --outdir {outdir} --base {base}
"""

_RUN_AWAITING_AGENT_INSTRUCTIONS = """\
[AWAITING_AGENT] transcribe complete. Translation task written to:
  {task}
AGENT ACTION REQUIRED:
  1. Read the task file. FIRST read `full_transcript` (whole scene) + `source`
     + `guidelines`, THEN translate each `to_translate` item per the `persona`.
  2. Save as {zh} — JSON {{str(index): zh}} covering every to_translate index.
  3. Run: video-translate generate --segments {segments} --zh {zh} --outdir {outdir} --base {base}
"""

_BACKFILL_INSTRUCTIONS = """\
[AWAITING_AGENT] backfill task written to:
  {task}
  (indices are the ORIGINAL zh_segments indices — fill those keys)
AGENT ACTION REQUIRED:
  1. Read the task file; translate each `to_translate` item per the `persona`.
  2. Save your translations as a JSON {{str(index): zh}} object.
  3. Run: video-translate backfill --pending {pending} --out {out} \\
             --agent-zh <your_translations.json> --segments {segments} --outdir {outdir} --base {base}
"""


# --------------------------- verify gate (Spec 18) ---------------------------

# suffixes whose stem is the video base (used to locate the generate_opts sidecar)
_BASE_STRIP_SUFFIXES = (
    ".segments_en.json", ".segments_raw.json", ".translate_task.json",
    ".zh_segments.json", ".backfill_task.json", ".agent_pending.json",
)


def _derive_base(segments_path: str) -> str:
    name = os.path.basename(segments_path)
    for suf in _BASE_STRIP_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return os.path.splitext(name)[0]


def _find_generate_opts(segments_path: str) -> dict | None:
    """Locate the display-window sidecar written by `generate` (Spec 18)."""
    seg_dir = os.path.dirname(segments_path)
    base = _derive_base(segments_path)
    candidates = [
        os.path.join(seg_dir, base, base + ".generate_opts.json"),
        os.path.join(seg_dir, base + ".generate_opts.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return load_json(c)
            except Exception:  # noqa: BLE001
                return None
    return None


def cmd_verify(args: argparse.Namespace) -> int:
    """Unified self-check gate: acoustic / content / presentation lanes (Spec 18).

    Returns 0 normally; with --strict, returns a non-zero code if any lane flags
    an issue (so CI can fail on warnings).
    """
    segments_path = args.segments
    zh_path = getattr(args, "zh", None)
    video = getattr(args, "video", None)
    strict = getattr(args, "strict", False)
    noise = getattr(args, "noise", "-30dB")
    d = getattr(args, "d", 0.3)
    opts_path = getattr(args, "opts", None)
    semantic = not getattr(args, "no_semantic", False)  # ADR-016/V14: ON by default
    semantic_out = getattr(args, "semantic_out", None)

    segments = load_json(segments_path)

    # ---- Lane 1: acoustic -------------------------------------------------
    silences: list[tuple[float, float]] = []
    audio_ok = False
    if video:
        prof = analyze_audio(video, noise=noise, d=d)
        silences = prof.silence_intervals
        audio_ok = prof.ok
        if prof.ok:
            print(f"[verify:acoustic] {len(silences)} silence gap(s) from "
                  f"silencedetect (independent reference)")
        else:
            print("[verify:acoustic] audio profile unavailable — lane skipped")
    else:
        print("[verify:acoustic] skipped: pass --video to enable (needs "
              "silencedetect reference)")

    offset = 0.0
    if opts_path and os.path.exists(opts_path):
        opts = load_json(opts_path)
    else:
        opts = _find_generate_opts(segments_path) or {}
    offset = float(opts.get("offset", 0.0) or 0.0)
    acoustic_issues = verify_acoustic(segments, silences, offset=offset) if silences else []

    # ADR-016 (T2b): uncovered-audio detection — audio present but no cue.
    uncovered: list[tuple[float, float]] = []
    if video and silences:
        try:
            from .ffmpeg_utils import probe_duration
            dur = probe_duration(video)
            uncovered = find_uncovered_speech(segments, silences, dur)
        except Exception:  # noqa: BLE001
            uncovered = []

    # ---- Lane 2: content (reuses validate_zh + verify_align) ---------------
    content_flags = 0
    mixed: list[dict[str, Any]] = []
    if zh_path:
        ok_zh, missing = validate_zh(segments_path, zh_path)
        if not ok_zh:
            content_flags += 1
        zh = {int(k): v for k, v in load_json(zh_path).items()}
        align_ok = align_report(segments, zh)
        if not align_ok:
            content_flags += 1
        # ADR-016/V14: deterministic 中英混杂 check — lower-case latin words left
        # untranslated (e.g. "rivalry"), which coverage/align can't catch.
        for i, s in enumerate(segments):
            words = find_untranslated_latin_words(zh.get(i, ""))
            if words:
                mixed.append({"index": i, "words": words})
        if mixed:
            content_flags += 1
    else:
        print("[verify:content] skipped: pass --zh to enable")

    # ---- Lane 3: presentation ---------------------------------------------
    first_start = None
    words0 = segments[0].get("words") if segments else None
    if words0:
        first_start = words0[0]["start"]
    elif segments:
        first_start = segments[0].get("start")
    presentation_issues = verify_presentation(opts, first_start, silences, offset=offset)

    # ---- report -----------------------------------------------------------
    any_flag = (bool(acoustic_issues) or bool(uncovered) or
                content_flags > 0 or bool(presentation_issues))
    print(f"\n=== verify report ===")
    print(f"  acoustic : {len(acoustic_issues)} issue(s)"
          + ("" if silences else " (no reference)"))
    for it in acoustic_issues:
        print(f"    - [{it['type']}] cue #{it['index']} "
              f"{it.get('start'):.2f}->{it.get('end'):.2f}s")
    if uncovered:
        print(f"  uncovered: {len(uncovered)} audio-present-but-no-cue window(s)")
        for (s, e) in uncovered:
            print(f"    - [{UNCOVERED_AUDIO}] {s:.2f}->{e:.2f}s")
    print(f"  content  : {'ok' if (zh_path and content_flags == 0) else 'skipped/flagged'}"
          f" ({content_flags} flag(s))")
    for it in mixed:
        print(f"    - [untranslated-latin] cue #{it['index']} {it['words']}")
    print(f"  presentation: {len(presentation_issues)} issue(s)")
    for it in presentation_issues:
        detail = it.get("detail") or f"start={it.get('start'):.2f}s"
        print(f"    - [{it['type']}] {detail}")

    # ---- Lane 2b: semantic reread task (agent-side; CLI never calls an LLM) --
    if semantic and zh_path:
        from .verify import build_semantic_reread_task
        zh = {int(k): v for k, v in load_json(zh_path).items()}
        task = build_semantic_reread_task(segments, zh)
        out = semantic_out or os.path.join(
            os.path.dirname(segments_path),
            _derive_base(segments_path) + ".semantic_reread_task.json",
        )
        save_json(out, task, indent=2)
        print(f"\n  semantic  : reread task written -> {out}")
        print(f"              ({len(task['pairs'])} pairs) — agent rereads "
              f"each (en,zh) and flags omit/add/wrong")
    elif semantic and not zh_path:
        print("\n  semantic  : skipped (needs --zh)")

    if not any_flag:
        print("  => clean (no lane flagged)")
    if strict and any_flag:
        return EXIT_RUNTIME
    return EXIT_OK


# --------------------------- parser ---------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video-translate",
        description="Video -> bilingual (zh/en) subtitles: faster-whisper + agent/Google translation.",
    )
    p.add_argument("--version", action="version", version=f"video-translate {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("transcribe", help="Transcribe video -> segments_en.json (chunked, resumable)")
    t.add_argument("input", help="path to the video/audio file")
    t.add_argument("--outdir", default=None, help="output dir (default: video's own directory)")
    t.add_argument("--base", default=None, help="output basename (default: video filename stem)")
    t.add_argument("--model", default="large-v3")
    t.add_argument("--chunk", type=float, default=240.0)
    t.add_argument("--threads", type=int, default=None)
    t.add_argument("--device", default=None, choices=["auto", "cpu", "cuda"],
                   help="compute device (default auto: CUDA if available, else cpu)")
    t.add_argument("--compute-type", default=None,
                   choices=["auto", "float16", "int8", "int8_float16"],
                   help="quantization (default auto: int8_float16 on cuda, int8 on cpu)")
    t.add_argument("--lang", default=None, help="source language (default: auto-detect)")
    t.add_argument("--proxy", default=None)
    t.add_argument("--no-proxy", action="store_true", help="force direct connection (no proxy)")
    t.add_argument("--no-merge", action="store_true", help="skip segment-merge stage (Stage 2)")
    t.add_argument("--no-split", action="store_true", help="skip cue splitting (Stage 2b); keep merged cues as-is")
    t.add_argument("--merge-max-chars", type=int, default=None, help="max chars per cue before splitting (default 42)")
    t.add_argument("--no-drift-snap", action="store_true",
                   help="keep Whisper's raw word timestamps even when a lone "
                        "word sits seconds ahead of its own sentence (disables "
                        "the drift-orphan fix)")
    t.add_argument("--vad-threshold", type=float, default=None,
                   help="Silero VAD speech threshold (default 0.35). Lower = "
                        "catch quieter/music-underscored lines at the cost of "
                        "more noise; changing it invalidates the chunk cache")
    t.add_argument("--vad", action="store_true",
                   help="enable Silero VAD before decoding (default: OFF — "
                        "full raw decode; use VAD only for very clean "
                        "single-speaker audio)")
    t.add_argument("--adaptive-vad", action="store_true",
                   help="(ADR-015) route VAD per chunk from each chunk's local "
                        "audio profile: clean chunks use VAD (anchor to silence), "
                        "noisy/continuous chunks run bare (avoid dropping speech "
                        "under laughter/cheer/music). Supersedes a global --vad.")
    t.add_argument("--separate-vocals", action="store_true",
                   help="(T2 / ADR-017) run Demucs vocal/accompaniment separation "
                        "BEFORE transcription, feeding Whisper only the cleaned "
                        "vocals track. Cures strong-BGM hallucinations. demucs is a "
                        "core dependency (installed by default via `pip install -e .`; "
                        "Windows/Linux get the CUDA torch wheel, macOS the CPU wheel). "
                        "GPU recommended. Timeline (start/end timestamps) is kept "
                        "1:1 with the original video — this flag only swaps the "
                        "decode input source, never rewrites cue boundaries.")
    t.add_argument("--demucs-model", default=None,
                   help="(T2) Demucs model to use when --separate-vocals is on "
                        "(default 'htdemucs'). Advanced: 'htdemucs_ft' for slightly "
                        "higher quality at ~2x the runtime.")
    t.add_argument("--no-audit", action="store_true",
                   help="skip the coverage self-audit + gap recovery step after "
                        "transcription (audit runs by default)")
    t.set_defaults(func=cmd_transcribe)

    tr = sub.add_parser("translate", help="Translate segments_en.json -> zh_segments.json")
    tr.add_argument("--segments", required=True)
    tr.add_argument("--out", required=True)
    tr.add_argument("--pending", default=None)
    tr.add_argument("--proxy", default=None)
    tr.add_argument("--no-proxy", action="store_true")
    tr.add_argument("--src", default="en")
    tr.add_argument("--tgt", default="zh-CN")
    tr.add_argument("--engine", default=None, choices=["agent", "google"],
                    help="agent (default) emits a task for the calling agent; google is headless MT")
    tr.add_argument("--glossary", default=None,
                    help="path to glossary file (txt/json) injected into the translation persona")
    tr.add_argument("--source", default=None,
                    help="video provenance/背景 hint fed to the translator, e.g. "
                         "'电影《天国王朝》鲍德温四世与萨拉丁会面片段'")
    tr.set_defaults(func=cmd_translate)

    g = sub.add_parser("generate", help="Generate the four subtitle files")
    g.add_argument("--segments", required=True)
    g.add_argument("--zh", required=True)
    g.add_argument("--outdir", required=True)
    g.add_argument("--base", default=None)
    g.add_argument("--gap", type=float, default=0.2,
                   help="min gap (s) between cues; trims trailing silence (default 0.2)")
    g.add_argument("--min-dur", type=float, default=1.0,
                   help="min display duration (s) per cue; extends short cues "
                        "(display-only, start never moves). 0 disables (default 1.0)")
    g.add_argument("--offset", type=float, default=0.0,
                   help="shift every cue's DISPLAY window by N seconds "
                        "(positive = later). Corrects Whisper's systematic "
                        "word-timestamp drift when subtitles feel early (default 0)")
    g.add_argument("--tail", type=float, default=0.3,
                   help="extend each cue's DISPLAY end by N seconds so lines "
                        "don't clear mid-sentence; the --gap clamp still wins "
                        "(default 0.3, 0 disables)")
    g.add_argument("--flat", action="store_true",
                   help="legacy: write outputs flat into --outdir (no per-video "
                        "subfolder, no version suffix)")
    g.add_argument("--prune-old", action="store_true",
                   help="keep only the 2 newest versioned outputs in the subfolder")
    g.add_argument("--no-align-check", action="store_true",
                   help="skip the zh/en index-drift audit run before rendering")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("run", help="Full pipeline: transcribe -> translate -> generate")
    r.add_argument("input", help="path to the video/audio file")
    r.add_argument("--outdir", default=None, help="output dir (default: video's own directory)")
    r.add_argument("--base", default=None, help="output basename (default: video filename stem)")
    r.add_argument("--skip", nargs="*", choices=["transcribe", "translate", "generate"], default=[])
    r.add_argument("--model", default=None)
    r.add_argument("--chunk", type=float, default=None)
    r.add_argument("--lang", default=None, help="source language (default: auto-detect)")
    r.add_argument("--threads", type=int, default=None)
    r.add_argument("--device", default=None, choices=["auto", "cpu", "cuda"],
                   help="compute device (default auto: CUDA if available, else cpu)")
    r.add_argument("--compute-type", default=None,
                   choices=["auto", "float16", "int8", "int8_float16"],
                   help="quantization (default auto: int8_float16 on cuda, int8 on cpu)")
    r.add_argument("--proxy", default=None)
    r.add_argument("--no-proxy", action="store_true")
    r.add_argument("--no-merge", action="store_true")
    r.add_argument("--no-split", action="store_true", help="skip cue splitting")
    r.add_argument("--merge-max-chars", type=int, default=None, help="max chars per cue before splitting (default 42)")
    r.add_argument("--no-drift-snap", action="store_true",
                   help="disable the drift-orphan fix (keep raw word timestamps)")
    r.add_argument("--vad-threshold", type=float, default=None,
                   help="Silero VAD speech threshold (default 0.35); lower = "
                        "catch quieter lines, invalidates the chunk cache")
    r.add_argument("--vad", action="store_true",
                   help="enable Silero VAD before decoding (default: OFF — "
                        "full raw decode; use VAD only for very clean "
                        "single-speaker audio)")
    r.add_argument("--adaptive-vad", action="store_true",
                   help="(ADR-015) route VAD per chunk from each chunk's local "
                        "audio profile: clean chunks use VAD (anchor to silence), "
                        "noisy/continuous chunks run bare (avoid dropping speech "
                        "under laughter/cheer/music). Supersedes a global --vad.")
    r.add_argument("--separate-vocals", action="store_true",
                   help="(T2 / ADR-017) run Demucs vocal/accompaniment separation "
                        "BEFORE transcription. See 'transcribe --separate-vocals'.")
    r.add_argument("--demucs-model", default=None,
                   help="(T2) advanced: override the Demucs model name (default htdemucs)")
    r.add_argument("--no-audit", action="store_true",
                   help="skip the coverage self-audit + gap recovery step after "
                        "transcription (audit runs by default)")
    r.add_argument("--src", default="en")
    r.add_argument("--tgt", default="zh-CN")
    r.add_argument("--engine", default=None, choices=["agent", "google"],
                    help="agent (default) stops after task; google runs end-to-end")
    r.add_argument("--gap", type=float, default=0.2, help="min gap (s) between cues (default 0.2)")
    r.add_argument("--min-dur", type=float, default=1.0,
                   help="min display duration (s) per cue; 0 disables (default 1.0)")
    r.add_argument("--offset", type=float, default=0.0,
                   help="shift every cue's DISPLAY window by N seconds "
                        "(positive = later); corrects word-timestamp drift (default 0)")
    r.add_argument("--tail", type=float, default=0.3,
                   help="extend each cue's DISPLAY end by N seconds (default 0.3)")
    r.add_argument("--glossary", default=None, help="path to glossary file (txt/json)")
    r.add_argument("--source", default=None,
                   help="video provenance/背景 hint fed to the translator, e.g. "
                        "'电影《天国王朝》鲍德温四世与萨拉丁会面片段'")
    r.add_argument("--flat", action="store_true",
                   help="legacy: write final outputs flat into --outdir (no per-video subfolder)")
    r.add_argument("--prune-old", action="store_true",
                   help="keep only the 2 newest versioned outputs in the subfolder")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resegment",
                        help="Re-transcribe time windows with a forced language and splice into segments_en.json")
    rs.add_argument("--segments", required=True, help="path to segments_en.json to patch")
    rs.add_argument("--video", required=True, help="source video (for re-transcription)")
    rs.add_argument("--windows", required=True, nargs="+",
                    help="time windows to re-transcribe, each 'start-end' in seconds, "
                         "e.g. 12.0-18.5 41.0-45.0")
    rs.add_argument("--lang", required=True,
                    help="forced language for the windows (e.g. ja, en, zh)")
    rs.add_argument("--model", default="large-v3")
    rs.add_argument("--threads", type=int, default=None)
    rs.add_argument("--device", default=None, choices=["auto", "cpu", "cuda"],
                    help="compute device (default auto: CUDA if available, else cpu)")
    rs.add_argument("--compute-type", default=None,
                    choices=["auto", "float16", "int8", "int8_float16"],
                    help="quantization (default auto)")
    rs.add_argument("--vad", action="store_true",
                    help="enable VAD for the re-transcription windows")
    rs.add_argument("--separate-vocals", action="store_true",
                    help="(T2) re-transcribe windows from a previously-produced "
                         "vocals.wav. If no cached vocals.wav exists, the user "
                         "must run 'transcribe --separate-vocals' first.")
    rs.add_argument("--demucs-model", default=None,
                    help="(T2) Demucs model name (default htdemucs); must match "
                         "the model used to produce the cached vocals.wav")
    rs.set_defaults(func=cmd_resegment)

    s = sub.add_parser("setup", help="Check/download the HF model (reuse if cached)")
    s.add_argument("--model", default="large-v3")
    s.add_argument("--device", default=None, choices=["auto", "cpu", "cuda"],
                   help="compute device (default auto: CUDA if available, else cpu)")
    s.add_argument("--compute-type", default=None,
                   choices=["auto", "float16", "int8", "int8_float16"],
                   help="quantization (default auto)")
    s.add_argument("--proxy", default=None)
    s.add_argument("--no-proxy", action="store_true")
    s.set_defaults(func=cmd_setup)

    d = sub.add_parser("doctor", help="Environment self-check")
    d.add_argument("--strict", action="store_true",
                   help="return exit code 7 if any check (incl. Google endpoint) fails")
    d.add_argument("--video", default=None,
                   help="optional video path: also compute an audio profile and a "
                        "VAD routing recommendation (ADR-012)")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("verify", help="Self-check gate: acoustic/content/presentation lanes (Spec 18)")
    v.add_argument("--segments", required=True, help="path to segments_en.json")
    v.add_argument("--zh", default=None, help="path to zh_segments.json (enables content lane)")
    v.add_argument("--video", default=None,
                   help="video path; enables the acoustic lane (silencedetect reference)")
    v.add_argument("--opts", default=None,
                   help="path to a generate_opts.json (display-window params); "
                        "auto-located next to the segments if omitted")
    v.add_argument("--noise", default="-30dB", help="silencedetect noise gate (default -30dB)")
    v.add_argument("--d", type=float, default=0.3, help="silencedetect min silence dur (s)")
    v.add_argument("--strict", action="store_true",
                   help="return non-zero if any lane flags an issue (CI)")
    v.add_argument("--no-semantic", action="store_true",
                   help="skip the agent-side semantic reread task (ON by default; "
                        "disable to save agent tokens when the reread is not needed). "
                        "The task itself costs no LLM tokens — the *reread* does.")
    v.add_argument("--semantic-out", default=None,
                   help="path for the semantic reread task JSON (default: next "
                        "to --segments as <base>.semantic_reread_task.json)")
    v.set_defaults(func=cmd_verify)

    b = sub.add_parser("backfill", help="Backfill agent_pending via the agent engine")
    b.add_argument("--pending", required=True, help="path to <base>.agent_pending.json")
    b.add_argument("--out", required=True, help="path to <base>.zh_segments.json (merge target)")
    b.add_argument("--segments", default=None, help="segments_en.json (for generate in merge mode)")
    b.add_argument("--outdir", default=None)
    b.add_argument("--base", default=None)
    b.add_argument("--agent-zh", default=None,
                   help="agent-filled zh JSON (triggers merge + generate)")
    b.set_defaults(func=cmd_backfill)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    init_toolchain()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
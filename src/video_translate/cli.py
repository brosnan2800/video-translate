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
from .verify import verify_acoustic, verify_presentation
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
    """Heuristic: is a faster-whisper model present in the HF cache?"""
    hub = os.path.join(_hf_cache_dir(), "hub")
    if not os.path.isdir(hub):
        return False
    needle = model_name.replace("/", "--").lower()
    return any(needle in d.lower() for d in os.listdir(hub))


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
    print(f"video-translate {__version__} — environment check\n")
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

    print(f"\n  device        : cpu (forced; CTranslate2 has no AMD/Metal support)")
    print(f"  compute_type  : int8")
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
    cfg = resolve_config(cwd=os.getcwd())
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

    # ADR-012: audio profile + automatic VAD routing recommendation.
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
        WhisperModel(model, device="cpu", compute_type="int8")  # triggers download
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


def cmd_transcribe(args: argparse.Namespace) -> int:
    dep = _require_ffmpeg()
    if dep is not None:
        return dep
    input_path = args.input
    outdir = args.outdir or _default_outdir(input_path)
    base = args.base or _default_base(input_path)
    cfg = resolve_config(
        {"model": args.model, "chunk": args.chunk, "lang": args.lang,
         "merge_max_chars": getattr(args, "merge_max_chars", None)},
        cwd=os.getcwd(),
    )
    try:
        from .transcribe import transcribe_video
        transcribe_video(
            input_path, outdir, base=base, model_name=cfg.model,
            chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
            vad_threshold=getattr(args, "vad_threshold", None),
            use_vad=getattr(args, "vad", False),
        )
        segs_path = os.path.join(outdir, f"{base}.segments_en.json")
        # ADR-012: compute the independent silence reference ONCE and share it
        # with both the hallucination filter (merge) and the gap audit.
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
                use_vad=getattr(args, "vad", False),
                silence_intervals=silences,
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
         "source": getattr(args, "source", None)},
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
            no_audit=getattr(args, "no_audit", False),
            no_drift_snap=getattr(args, "no_drift_snap", False),
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

    from .transcribe import transcribe_window
    new_segs: list[dict[str, Any]] = []
    for (ws, we) in windows:
        print(f"[resegment] window {ws:.1f}-{we:.1f}s lang={args.lang} ...",
              flush=True)
        window_segs = transcribe_window(
            args.video, ws, we, lang=args.lang,
            use_vad=getattr(args, "vad", False),
            model_name=args.model, threads=args.threads,
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
    semantic = getattr(args, "semantic", False)
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

    # ---- Lane 2: content (reuses validate_zh + verify_align) ---------------
    content_flags = 0
    if zh_path:
        ok_zh, missing = validate_zh(segments_path, zh_path)
        if not ok_zh:
            content_flags += 1
        zh = {int(k): v for k, v in load_json(zh_path).items()}
        align_ok = align_report(segments, zh)
        if not align_ok:
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
    any_flag = bool(acoustic_issues) or content_flags > 0 or bool(presentation_issues)
    print(f"\n=== verify report ===")
    print(f"  acoustic : {len(acoustic_issues)} issue(s)"
          + ("" if silences else " (no reference)"))
    for it in acoustic_issues:
        print(f"    - [{it['type']}] cue #{it['index']} "
              f"{it.get('start'):.2f}->{it.get('end'):.2f}s")
    print(f"  content  : {'ok' if (zh_path and content_flags == 0) else 'skipped/flagged'}"
          f" ({content_flags} flag(s))")
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
    rs.add_argument("--vad", action="store_true",
                    help="enable VAD for the re-transcription windows")
    rs.set_defaults(func=cmd_resegment)

    s = sub.add_parser("setup", help="Check/download the HF model (reuse if cached)")
    s.add_argument("--model", default="large-v3")
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
    v.add_argument("--semantic", action="store_true",
                   help="emit an agent-side semantic reread task (en/zh pairs + "
                        "context) for the calling agent to flag omit/add/wrong; "
                        "CLI itself never calls an LLM (ADR-005)")
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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

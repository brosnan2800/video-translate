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
    print(f"  proxy         : auto-detect (--no-proxy for direct)")

    # V3: probe Google Translate endpoint reachability via the resolved proxy,
    # so a 7-minute transcribe won't fail first at the translate step.
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
    proxy = _resolve_proxy(args)
    try:
        setup_http_proxy(proxy)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY
    try:
        from .transcribe import transcribe_video
        transcribe_video(
            input_path, outdir, base=base, model_name=cfg.model,
            chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
        )
        if cfg.merge_enabled and not args.no_merge:
            from .merge import apply_merge
            segs_path = os.path.join(outdir, f"{base}.segments_en.json")
            raw_path = os.path.join(outdir, f"{base}.segments_raw.json")
            apply_merge(
                segs_path, raw_path=raw_path,
                max_dur=cfg.merge_max_dur, max_gap=cfg.merge_max_gap,
                split_enabled=not getattr(args, "no_split", False),
                split_max_chars=cfg.merge_max_chars,
            )
            print(f"[merge] merged + split -> {segs_path} (raw kept at {raw_path})")
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] transcription failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_translate(args: argparse.Namespace) -> int:
    cfg = resolve_config(
        {"proxy": args.proxy, "src": args.src, "tgt": args.tgt, "engine": args.engine},
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
                                glossary=glossary_text)
        base = _derive_base(segments)
        outdir = str(Path(out).parent)
        print(_AGENT_TRANSLATE_INSTRUCTIONS.format(
            task=task_path, segments=segments, out=out, outdir=outdir, base=base))
        return EXIT_AWAITING_AGENT

    # google engine (headless fallback)
    proxy = _resolve_proxy(args)
    try:
        from .translate import translate_segments
        translate_segments(
            segments, out, pending_path=args.pending,
            proxy=proxy, src=cfg.src, tgt=cfg.tgt,
        )
        return EXIT_OK
    except ValueError as e:  # SOCKS proxy
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY
    except Exception as e:  # noqa: BLE001
        print(f"[error] translation failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_generate(args: argparse.Namespace) -> int:
    base = args.base or _derive_base(args.segments)
    gap = getattr(args, "gap", 0.0) or 0.0
    try:
        from .generate import generate_subtitles
        generate_subtitles(args.segments, args.zh, args.outdir, base=base, gap=gap)
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
         "glossary": getattr(args, "glossary", None)},
        cwd=os.getcwd(),
    )

    if "transcribe" not in skip:
        rc = cmd_transcribe(argparse.Namespace(
            input=input_path, outdir=outdir, base=base,
            model=cfg.model, chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
            proxy=args.proxy, no_proxy=args.no_proxy, no_merge=args.no_merge,
            no_split=getattr(args, "no_split", False),
            merge_max_chars=cfg.merge_max_chars,
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
                                glossary=glossary_text)
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
        ))
        if rc != EXIT_OK:
            return rc
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
  1. Read the task file; for each batch translate the `to_translate` items to
     Chinese following the `persona`.
  2. Save as {out} — a JSON object mapping str(index) -> zh, covering every
     index in to_translate[*].index.
  3. Run: video-translate generate --segments {segments} --zh {out} --outdir {outdir} --base {base}
"""

_RUN_AWAITING_AGENT_INSTRUCTIONS = """\
[AWAITING_AGENT] transcribe complete. Translation task written to:
  {task}
AGENT ACTION REQUIRED:
  1. Read the task file; translate each `to_translate` item per the `persona`.
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
    tr.set_defaults(func=cmd_translate)

    g = sub.add_parser("generate", help="Generate the four subtitle files")
    g.add_argument("--segments", required=True)
    g.add_argument("--zh", required=True)
    g.add_argument("--outdir", required=True)
    g.add_argument("--base", default=None)
    g.add_argument("--gap", type=float, default=0.2,
                   help="min gap (s) between cues; trims trailing silence (default 0.2)")
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
    r.add_argument("--src", default="en")
    r.add_argument("--tgt", default="zh-CN")
    r.add_argument("--engine", default=None, choices=["agent", "google"],
                    help="agent (default) stops after task; google runs end-to-end")
    r.add_argument("--gap", type=float, default=0.2, help="min gap (s) between cues (default 0.2)")
    r.add_argument("--glossary", default=None, help="path to glossary file (txt/json)")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("setup", help="Check/download the HF model (reuse if cached)")
    s.add_argument("--model", default="large-v3")
    s.add_argument("--proxy", default=None)
    s.add_argument("--no-proxy", action="store_true")
    s.set_defaults(func=cmd_setup)

    d = sub.add_parser("doctor", help="Environment self-check")
    d.add_argument("--strict", action="store_true",
                   help="return exit code 7 if any check (incl. Google endpoint) fails")
    d.set_defaults(func=cmd_doctor)

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

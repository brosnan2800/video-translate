"""Command-line interface for video-translate.

Subcommands: transcribe / translate / generate / run / setup / doctor.

Exit codes:
    0  success
    1  runtime error
    2  argument error (argparse default)
    3  missing dependency (ffmpeg / HF model)
    4  proxy error (e.g. SOCKS proxy given)
    5  transcription killed (SIGKILL); some chunks completed, safe to re-run
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Sequence

from . import __version__
from .config import DEFAULT_HF_CACHE, resolve_config

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_ARGS = 2
EXIT_MISSING_DEP = 3
EXIT_PROXY = 4
EXIT_KILLED = 5


# --------------------------- doctor / setup helpers ---------------------------

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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report environment readiness. Never fails hard; returns 0."""
    print(f"video-translate {__version__} — environment check\n")
    checks = [
        ("ffmpeg", _has("ffmpeg")),
        ("ffprobe", _has("ffprobe")),
        (f"HF cache dir ({_hf_cache_dir()})", os.path.isdir(_hf_cache_dir())),
        ("large-v3 model cached (reuse, no re-download)", _model_cached("large-v3")),
    ]
    for name, ok in checks:
        print(f"  [{'OK ' if ok else 'MISS'}] {name}")

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
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """Ensure the HF model is present; download it if missing (reuse if present)."""
    model = args.model
    if _model_cached(model):
        print(f"[setup] {model} already cached in {_hf_cache_dir()} — reusing, no download.")
        return EXIT_OK
    print(f"[setup] {model} not found; downloading into {_hf_cache_dir()} (~3GB for large-v3)...")
    try:
        from .proxy import setup_http_proxy
        cfg = resolve_config({"proxy": args.proxy})
        setup_http_proxy(cfg.proxy)
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
    cfg = resolve_config({"model": args.model, "chunk": args.chunk, "lang": args.lang})
    try:
        from .proxy import setup_http_proxy
        setup_http_proxy(cfg.proxy)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY
    try:
        from .transcribe import transcribe_video
        transcribe_video(
            args.input, args.outdir, base=args.base, model_name=cfg.model,
            chunk=cfg.chunk, threads=args.threads, lang=cfg.lang,
        )
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] transcription failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_translate(args: argparse.Namespace) -> int:
    cfg = resolve_config({"proxy": args.proxy, "src": args.src, "tgt": args.tgt})
    try:
        from .translate import translate_segments
        translate_segments(
            args.segments, args.out, pending_path=args.pending,
            proxy=cfg.proxy, src=cfg.src, tgt=cfg.tgt,
        )
        return EXIT_OK
    except ValueError as e:  # SOCKS proxy
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY
    except Exception as e:  # noqa: BLE001
        print(f"[error] translation failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_generate(args: argparse.Namespace) -> int:
    try:
        from .generate import generate_subtitles
        generate_subtitles(args.segments, args.zh, args.outdir, base=args.base)
        return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"[error] generate failed: {e}", file=sys.stderr)
        return EXIT_RUNTIME


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline: transcribe -> translate -> generate."""
    skip = set(args.skip or [])
    outdir = args.outdir
    base = args.base
    segments = os.path.join(outdir, f"{base}.segments_en.json")
    zh = os.path.join(outdir, f"{base}.zh_segments.json")
    pending = os.path.join(outdir, f"{base}.agent_pending.json")

    if "transcribe" not in skip:
        args.model = getattr(args, "model", "large-v3")
        args.chunk = getattr(args, "chunk", 240.0)
        args.lang = getattr(args, "lang", "en")
        args.threads = getattr(args, "threads", None)
        rc = cmd_transcribe(argparse.Namespace(
            input=args.input, outdir=outdir, base=base,
            model=args.model, chunk=args.chunk, threads=args.threads, lang=args.lang,
        ))
        if rc != EXIT_OK:
            return rc

    if "translate" not in skip:
        rc = cmd_translate(argparse.Namespace(
            segments=segments, out=zh, pending=pending,
            proxy=args.proxy, src=args.src, tgt=args.tgt,
        ))
        if rc != EXIT_OK:
            return rc

    if "generate" not in skip:
        rc = cmd_generate(argparse.Namespace(
            segments=segments, zh=zh, outdir=outdir, base=base,
        ))
        if rc != EXIT_OK:
            return rc
    return EXIT_OK


# --------------------------- parser ---------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video-translate",
        description="Video -> bilingual (zh/en) subtitles: faster-whisper + Google Translate.",
    )
    p.add_argument("--version", action="version", version=f"video-translate {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("transcribe", help="Transcribe video -> segments_en.json (chunked, resumable)")
    t.add_argument("--input", required=True)
    t.add_argument("--outdir", required=True)
    t.add_argument("--base", default="apollo_story")
    t.add_argument("--model", default="large-v3")
    t.add_argument("--chunk", type=float, default=240.0)
    t.add_argument("--threads", type=int, default=None)
    t.add_argument("--lang", default="en")
    t.set_defaults(func=cmd_transcribe)

    tr = sub.add_parser("translate", help="Translate segments_en.json -> zh_segments.json")
    tr.add_argument("--segments", required=True)
    tr.add_argument("--out", required=True)
    tr.add_argument("--pending", default=None)
    tr.add_argument("--proxy", default=None)
    tr.add_argument("--src", default="en")
    tr.add_argument("--tgt", default="zh-CN")
    tr.set_defaults(func=cmd_translate)

    g = sub.add_parser("generate", help="Generate the four subtitle files")
    g.add_argument("--segments", required=True)
    g.add_argument("--zh", required=True)
    g.add_argument("--outdir", required=True)
    g.add_argument("--base", default="apollo_story")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("run", help="Full pipeline: transcribe -> translate -> generate")
    r.add_argument("--input", required=True)
    r.add_argument("--outdir", required=True)
    r.add_argument("--base", default="apollo_story")
    r.add_argument("--skip", nargs="*", choices=["transcribe", "translate", "generate"], default=[])
    r.add_argument("--proxy", default=None)
    r.add_argument("--src", default="en")
    r.add_argument("--tgt", default="zh-CN")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("setup", help="Check/download the HF model (reuse if cached)")
    s.add_argument("--model", default="large-v3")
    s.add_argument("--proxy", default=None)
    s.set_defaults(func=cmd_setup)

    d = sub.add_parser("doctor", help="Environment self-check")
    d.set_defaults(func=cmd_doctor)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

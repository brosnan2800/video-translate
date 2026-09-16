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
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    DEFAULT_HF_CACHE,
    DEFAULT_PERSONA,
    VALID_PROMPTS,
    resolve_config,
)
from .io_utils import load_json, save_json
from .proxy import detect_proxy, setup_http_proxy
from .artifacts import artifact_path, workdir
from .audio_profile import analyze_audio, probe_volume_window
from .ffmpeg_utils import probe_duration
from .toolchain import init_toolchain, resolve_command_entry
from .verify import (
    ADJACENT_OVERLAP, UNCOVERED_AUDIO, classify_uncovered_windows,
    find_adjacent_overlaps, find_embedded_linebreaks, find_uncovered_speech,
    find_untranslated_latin_words, is_recovered_segment, verify_acoustic,
    verify_presentation,
)
from .translate import validate_zh
from .verify_align import report as align_report
from .capabilities import GateFail

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_ARGS = 2
EXIT_MISSING_DEP = 3
EXIT_PROXY = 4
EXIT_KILLED = 5
EXIT_AWAITING_AGENT = 6
EXIT_DOCTOR_FAIL = 7
EXIT_GATE_FAIL = 8  # control plane: an explicit decision could not be honored


# --------------------------- helpers ---------------------------

def _has(binary: str) -> bool:
    """True when the binary resolves to a real executable.

    Routes through the toolchain registry so this check agrees with whatever
    ``resolve_tool()`` later hands to subprocess. A path-only check here used to
    disagree with the persisted portable build — the source of "ffmpeg OK in
    doctor / ffmpeg missing in the next stage".
    """
    from .toolchain import tool_available

    return tool_available(binary)


def _hf_cache_dir() -> str:
    """HuggingFace cache dir, resolved through the toolchain registry.

    Reading HF_HOME at the call site can disagree with the value resolved at
    startup (unset env, a later .env load), which is what produced false
    "model missing — re-download" reports.
    """
    from .toolchain import model_dir

    return model_dir("hf_cache") or DEFAULT_HF_CACHE


# Milestone 3 / E3: a complete large-v3 model.bin is ~3.09 GB. A model.bin
# smaller than this lower bound is a truncated/corrupt download and must be
# treated as NOT cached so setup self-heals and the run aborts with a clear fix.
_MODEL_MIN_BYTES = 2 * 1024 ** 3  # 2 GiB


def _model_cached(model_name: str = "large-v3", *, min_bytes: int | None = None) -> bool:
    """Is a faster-whisper model present (in-repo OR HF cache), file-complete?

    Checks for model.bin AND that it meets ``min_bytes`` (E3: a truncated
    download smaller than the bound is NOT falsely reported as cached).
    When ``min_bytes`` is None, the module-level ``_MODEL_MIN_BYTES`` is used
    (read at call time, so tests can monkeypatch it down without 3 GB stubs).
    """
    if min_bytes is None:
        min_bytes = _MODEL_MIN_BYTES
    # 1) in-repo local model dir
    cand = os.path.join(_LOCAL_MODEL_DIR, model_name)
    mbin = os.path.join(cand, "model.bin")
    if os.path.isfile(mbin) and os.path.getsize(mbin) >= min_bytes:
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
                mbin = os.path.join(snap_root, snap, "model.bin")
                if os.path.isfile(mbin) and os.path.getsize(mbin) >= min_bytes:
                    return True
    return False


def _find_incomplete_model_bins(model_name: str = "large-v3") -> list[str]:
    """Return paths of model.bin that EXIST but are below ``_MODEL_MIN_BYTES``.

    Used by setup self-heal (E3): a truncated model.bin is deleted before a
    fresh download so the run never loads a corrupt snapshot.
    """
    found: list[str] = []
    cand = os.path.join(_LOCAL_MODEL_DIR, model_name, "model.bin")
    if os.path.isfile(cand) and os.path.getsize(cand) < _MODEL_MIN_BYTES:
        found.append(cand)
    hub = os.path.join(_hf_cache_dir(), "hub")
    if os.path.isdir(hub):
        needle = model_name.replace("/", "--").lower()
        for d in os.listdir(hub):
            if needle in d.lower():
                snap_root = os.path.join(hub, d, "snapshots")
                if not os.path.isdir(snap_root):
                    continue
                for snap in os.listdir(snap_root):
                    mbin = os.path.join(snap_root, snap, "model.bin")
                    if os.path.isfile(mbin) and os.path.getsize(mbin) < _MODEL_MIN_BYTES:
                        found.append(mbin)
    return found


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


# 注：此处曾另有一份 ``_derive_base``（只剥 3 种后缀）。Python 模块级后定义覆盖
# 前定义，文件后部那份（剥 ``_BASE_STRIP_SUFFIXES`` 全部 6 种）才是实际生效的，
# 本处从未被执行。2026-09-16 清理死代码，唯一实现见 ``_BASE_STRIP_SUFFIXES`` 附近
# （S5 单一来源）。


# --- Spec 25: CLI path / filename hygiene ---------------------------------------
# Encoding-damage fingerprints (platform-independent). None of these code points
# belongs in a human-readable filename on ANY OS, so flagging them is safe on
# macOS/Linux too — a mojibake name is just as broken there.
#
# Accident source (2026-09-04): videos/角斗士采访.mp4 passed through Windows
# PowerShell 5.1 (ACP=936) arrived as '瑙掓枟澹\ue0a6噰璁?mp4' — U+E0A6 sits in
# the BMP Private Use Area and the '.' became '?' (so Path.stem swallowed the
# extension). The damaged string flowed all the way into io_utils.save_json() ->
# os.replace(), raising OSError [WinError 123] with a stack trace that had
# nothing to do with the cause.
_BROKEN_ENCODING_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x001F),      # C0 control chars
    (0x007F, 0x009F),      # DEL + C1 control chars
    (0xD800, 0xDFFF),      # surrogates (unpaired / UTF-16 misdecode)
    (0xE000, 0xF8FF),      # BMP Private Use Area (GBK-misread-UTF-8 signature)
    (0xFFFD, 0xFFFD),      # REPLACEMENT CHARACTER
    (0xF0000, 0x10FFFD),   # Supplementary Private Use Area
)

# Platform-specific illegal filename characters. The Windows set must NEVER be
# applied on POSIX: ':' and '?' are perfectly legal on macOS/APFS.
_ILLEGAL_FILENAME_CHARS = {
    "nt": '<>:"|?*',
    "posix": "/\x00",
}

# A Windows drive spec ('C:') is a path ROOT, not a filename — its ':' must not
# be reported as an illegal filename character (--outdir C:\ is legitimate).
_DRIVE_SPEC_RE = re.compile(r"^[A-Za-z]:$")

# Path-valued arguments inspected at the entry boundary. Output-side args are
# included for encoding damage only — no existence assertion is made here.
_HYGIENE_PATH_ARGS = ("input", "segments", "zh", "video", "pending",
                      "outdir", "out")


def find_broken_encoding_chars(name: str) -> list[str]:
    """Return characters in `name` that look like an encoding accident.

    Spec 25 §3 layer 1. Platform-independent: these code points are damage
    signals no matter which OS we run on.
    """
    return [ch for ch in name
            if any(lo <= ord(ch) <= hi for lo, hi in _BROKEN_ENCODING_RANGES)]


def find_illegal_filename_chars(name: str, *,
                                platform: str | None = None) -> list[str]:
    """Return characters illegal for a filename on `platform` (default: this OS).

    Spec 25 §3 layer 2. The character set is platform-specific on purpose —
    ':' and '?' are legal on macOS, so the Windows set must not be applied
    there (hard "do not break Mac" constraint).
    """
    plat = platform or ("nt" if os.name == "nt" else "posix")
    illegal = _ILLEGAL_FILENAME_CHARS.get(plat, _ILLEGAL_FILENAME_CHARS["posix"])
    return [ch for ch in name if ch in illegal]


def _describe_bad_chars(chars: list[str]) -> str:
    """Render offending characters with their Unicode rationale (dedup, ordered)."""
    out: list[str] = []
    for ch in chars:
        cp = ord(ch)
        if cp == 0xFFFD:
            out.append("U+FFFD (替换字符)")
        elif cp < 0x20 or 0x7F <= cp <= 0x9F:
            out.append(f"U+{cp:04X} (控制字符)")
        elif 0xD800 <= cp <= 0xDFFF:
            out.append(f"U+{cp:04X} (未配对代理)")
        elif 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD:
            out.append(f"U+{cp:04X} (私用区)")
        else:
            out.append(repr(ch))
    seen: set[str] = set()
    return ", ".join(s for s in out if not (s in seen or seen.add(s)))


def _hygiene_message(label: str, value: str, bad: list[str], *,
                     broken_encoding: bool) -> str:
    detail = _describe_bad_chars(bad)
    if broken_encoding:
        head = f"参数 {label} 的文件名编码已损坏，无法继续：{value}"
        why = (f"  坏字符: {detail}\n"
               "  含义: 命令行参数在到达 Python 之前就已被重新编码。")
    else:
        head = f"参数 {label} 的文件名含本平台非法字符，无法继续：{value}"
        why = f"  坏字符: {detail}"
    fix = ("  Fix: 最常见原因是 Windows PowerShell 5.1 把 UTF-8 中文参数按 "
           "GBK(ACP 936) 解码。\n"
           "       ① 改用 ASCII 文件名；② 换 PowerShell 7（无 BOM 脚本按 UTF-8 读）；\n"
           "       ③ 或用 --base <ascii-name> 显式指定产物名。")
    return "\n".join([head, why, fix])


def _path_hygiene_error(args: argparse.Namespace) -> str | None:
    """Spec 25 §1: reject path arguments that are already broken at the boundary.

    Returns an actionable message, or None when every inspected path is clean.
    Called from ``main()`` before dispatching to any ``cmd_*``, so one check
    covers every subcommand (ADR-035 D4: assert at the stage boundary, fail
    loud instead of degrading silently).
    """
    targets: list[tuple[str, str]] = []
    for attr in _HYGIENE_PATH_ARGS:
        val = getattr(args, attr, None)
        if isinstance(val, str) and val:
            targets.append((f"--{attr}", val))

    # base: explicit when given, otherwise derived from input (Spec 11 semantics
    # of _default_base() are unchanged — we validate its RESULT).
    base = getattr(args, "base", None)
    input_path = getattr(args, "input", None)
    if not base and isinstance(input_path, str) and input_path:
        base = _default_base(input_path)
    if isinstance(base, str) and base:
        targets.append(("--base", base))

    for label, value in targets:
        # Only the basename is a filename; ':' in 'C:\' or '/' in a directory
        # path are separators, not illegal characters.
        name = os.path.basename(value.rstrip("/\\") or value)
        if not name or _DRIVE_SPEC_RE.match(name):
            continue  # path root (drive spec / '/') — nothing to validate
        bad = find_broken_encoding_chars(name)
        if bad:
            return _hygiene_message(label, value, bad, broken_encoding=True)
        illegal = find_illegal_filename_chars(name)
        if illegal:
            return _hygiene_message(label, value, illegal, broken_encoding=False)
    return None


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

    # ADR-029 / Spec 23: command-entry determinism. A bare system python on
    # PATH shadows the project venv and silently loses deps (e.g. whisperx),
    # so doctor surfaces how this CLI was launched and how to fix it.
    entry, interp = resolve_command_entry()
    if entry == "bare":
        print(f"  [WARN] entry       : BARE — interpreter NOT in project .venv")
        print(f"                     interpreter: {interp}")
        print(f"                     Fix: cd <repo> && uv run video-translate ... (Spec 23)")
    else:
        print(f"  [OK ] entry       : {entry} — project .venv ({interp})")

    checks = [
        ("ffmpeg", _has("ffmpeg")),
        ("ffprobe", _has("ffprobe")),
        (f"HF cache dir ({_hf_cache_dir()})", os.path.isdir(_hf_cache_dir())),
        ("large-v3 model cached (reuse, no re-download)", _model_cached("large-v3")),
    ]
    failed = False
    ffmpeg_missing = False
    for name, ok in checks:
        print(f"  [{'OK ' if ok else 'MISS'}] {name}")
        if not ok:
            failed = True
            if name in ("ffmpeg", "ffprobe"):
                ffmpeg_missing = True

    cfg = resolve_config(cwd=os.getcwd())
    from .transcribe import resolve_device
    dev, ct = resolve_device(cfg.device, cfg.compute_type)
    print(f"\n  device        : {dev} (configured '{cfg.device}'; "
          f"CUDA {'yes' if _cuda_available() else 'no'})")
    print(f"  compute_type  : {ct} (configured '{cfg.compute_type}')")
    print(f"  NVIDIA CUDA   : {'yes' if _cuda_available() else 'no (CPU-only path)'}")
    try:
        from .toolchain import get_toolchain_status
        _tc = get_toolchain_status()
        if _tc.cuda_dir:
            print(f"  cuda dir      : {_tc.cuda_dir}  (source: {_tc.cuda_source or 'unknown'})")
    except Exception:  # noqa: BLE001
        pass

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
    # T4 / ADR-028 / Spec 22: forced-acoustic-alignment status (GPU-only extra)
    nltk_missing = False
    try:
        from .align import whisperx_available
        if whisperx_available():
            # whisperx sentence-splits with nltk. Without those corpora the align
            # pass degrades to DTW timestamps while still reporting success — the
            # most expensive kind of silent no-op, so surface it explicitly.
            from .toolchain import nltk_data_ready
            if nltk_data_ready():
                print(f"  whisperx      : OK — forced alignment available "
                      f"(--align whisperx)")
            else:
                nltk_missing = True
                print(f"  whisperx      : installed, but NLTK corpora MISSING — "
                      f"alignment would silently degrade to DTW")
        else:
            _mac = sys.platform == "darwin"
            _hint = ("(macOS has no CUDA; alignment unsupported)"
                     if _mac else
                     "not installed — enable with: uv sync --extra gpu "
                     "(requires NVIDIA CUDA)")
            print(f"  whisperx      : unavailable {_hint}")
    except Exception:
        print(f"  whisperx      : probe failed — forced alignment unavailable")
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

    # ADR-012 / ADR-017 / ADR-032: audio profile + 统一三决策推荐 (doctor 与
    # 决策点/run 共用 profile_recommendation, 避免双份逻辑漂移)。落盘到
    # decisions.audio_profile, 供 cmd_run 复用并作为 P0→P1 决策点依据。
    video = getattr(args, "video", None)
    if video:
        try:
            from .audio_profile import analyze_audio, profile_recommendation
            prof = analyze_audio(video)
            if prof.ok:
                rec = profile_recommendation(
                    prof, default_style=cfg.style or "film",
                    duration=prof.duration,
                )
                print(f"\n  audio profile : mean={prof.mean_vol} dB, max={prof.max_vol} dB, "
                      f"{len(prof.silence_intervals)} silence gap(s)")
                print(f"  recommendation: style={rec.style} vad={rec.vad} "
                      f"adaptive_vad={rec.adaptive_vad} "
                      f"separate_vocals={rec.separate_vocals} "
                      f"vad_threshold={rec.vad_threshold}")
                print(f"                  {rec.rationale}")
                # 落盘快照: 供 cmd_run 复用 (避免重复画像) + 作为决策点依据。
                # ADR-035 M2: 同时落盘原始声学事实（duration/静音区间/电平）——
                # 这是 artifact "audio_profile" 的唯一生产点，transcribe/verify
                # 读它不重算。
                try:
                    from . import state as vt_state

                    vt_state.record_audio_profile(
                        _default_outdir(video), _default_base(video), rec)
                    vt_state.record_acoustics(
                        _default_outdir(video), _default_base(video), prof)
                except Exception:  # noqa: BLE001
                    pass
            else:
                print(f"\n  audio profile : unavailable (ffmpeg profile failed; default bare run)")
        except Exception as e:  # noqa: BLE001
            print(f"\n  audio profile : unavailable ({e}); default bare run")

    # Show the resolved FFmpeg bin dir (helps diagnose "ffmpeg MISS" cases) and,
    # when it is missing, point the user/Agent at the deterministic fix.
    # Rule 3: derive it from the path the toolchain actually resolved rather than
    # a raw env read — the variable can be stale or empty while a portable build
    # is in use (or set while something else actually wins).
    from .toolchain import get_toolchain_status

    ffmpeg_path = get_toolchain_status().ffmpeg_path
    ffmpeg_dir = str(Path(ffmpeg_path).parent) if ffmpeg_path else ""
    if ffmpeg_dir:
        print(f"\n  ffmpeg dir    : {ffmpeg_dir}")
    if ffmpeg_missing:
        print("\n  [FIX] ffmpeg/ffprobe missing. Run the deterministic auto-download:")
        print("        video-translate setup --ffmpeg")
        print("        (downloads a portable build into tools/ffmpeg, no manual install)")
    if nltk_missing:
        print("\n  [FIX] NLTK alignment corpora missing. Run:")
        print("        video-translate setup --align")
        print("        (downloads punkt/punkt_tab into models/nltk_data, no C:\\ cache)")
    if not _model_cached("large-v3"):
        print("\n  [FIX] large-v3 model missing. Run:")
        print("        make setup     # or: video-translate setup")

    # ffmpeg/ffprobe 是核心流水线的硬依赖：转写缺它无法 extract_chunk 抽轨 /
    # probe_duration 取时长，缺失即必崩。故 ffmpeg/ffprobe 缺失默认硬失败
    # (exit 7)，不再仅依赖 --strict —— 避免 "doctor 全绿却 run 一转写就崩" 的误导。
    # 其余可选依赖 (whisperx/demucs/nltk/proxy) 保持宽松，仅 --strict 才拦。
    if ffmpeg_missing:
        print("\n  [GATE] ffmpeg/ffprobe is a hard dependency of the core "
              "pipeline; doctor fails by default (run `setup --ffmpeg`).")
        return EXIT_DOCTOR_FAIL
    if strict and failed:
        return EXIT_DOCTOR_FAIL
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """Ensure the HF model (and optionally portable FFmpeg) is present.

    With ``--ffmpeg`` it downloads a portable FFmpeg into ``tools/ffmpeg`` via
    ``ensure_ffmpeg`` (idempotent). The model download only runs when
    ``--no-model`` is not set.
    """
    proxy = _resolve_proxy(args)
    try:
        setup_http_proxy(proxy)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return EXIT_PROXY

    if getattr(args, "ffmpeg", False):
        from .toolchain import ensure_ffmpeg
        print("[setup] --ffmpeg requested: ensuring portable FFmpeg...")
        bin_dir = ensure_ffmpeg(proxy=proxy)
        if bin_dir is None:
            print("[error] ffmpeg auto-download failed (see messages above).",
                  file=sys.stderr)
            return EXIT_MISSING_DEP
        # Make the freshly-downloaded ffmpeg available for the rest of this run.
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["VT_FFMPEG_DIR"] = str(bin_dir)

    if getattr(args, "align", False):
        from .toolchain import ensure_nltk_data
        print("[setup] --align requested: ensuring NLTK alignment corpora...")
        nltk_dir = ensure_nltk_data()
        if nltk_dir is None:
            print("[error] NLTK corpora unavailable. whisperx (and therefore nltk) "
                  "ships with the [gpu] extra — install it with: "
                  "uv sync --extra gpu", file=sys.stderr)
            return EXIT_MISSING_DEP
        print(f"[setup] nltk data ready at {nltk_dir}")

    if getattr(args, "no_model", False):
        print("[setup] --no-model set; skipping model download.")
        return EXIT_OK

    model = args.model
    if _model_cached(model):
        print(f"[setup] {model} already present in {_LOCAL_MODEL_DIR} / HF cache "
              f"— reusing, no download.")
        return EXIT_OK
    # E3: self-heal truncated downloads before fetching a fresh copy.
    incomplete = _find_incomplete_model_bins(model)
    if incomplete:
        print(f"[setup] found {len(incomplete)} incomplete/corrupt model.bin — removing "
              f"before re-download:")
        for p in incomplete:
            try:
                os.remove(p)
                print(f"        removed {p}")
            except OSError as e:
                print(f"[warn] could not remove {p}: {e}", file=sys.stderr)
    print(f"[setup] {model} not found; downloading into {_LOCAL_MODEL_DIR} "
          f"(~3GB for large-v3, stays in-repo, no C:\\ users cache)...")
    try:
        from faster_whisper import WhisperModel
        from .transcribe import resolve_device
        dev, ct = resolve_device(getattr(args, "device", None),
                                 getattr(args, "compute_type", None))
        # Download into the project-local models/ dir so the weight never lands
        # in the user's HF cache (C:\Users\...\AppData) — drop-in ready, portable.
        WhisperModel(model, device=dev, compute_type=ct,
                     download_root=os.path.join(_LOCAL_MODEL_DIR, model))
        print(f"[setup] {model} ready at {os.path.join(_LOCAL_MODEL_DIR, model)}.")
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


def _resolve_silence_reference(
    input_path: str, outdir: str, base: str,
) -> list[tuple[float, float]] | None:
    """独立静音参照（``ffmpeg silencedetect``）——ADR-012 同一份喂 merge 与 fill_gaps。

    ADR-035 M2: silencedetect 全链只算一次（artifact ``audio_profile``，单一生产 =
    preflight）。优先读 state 落盘的声学事实；缺失（老目录 / 直调）才兜底重算并回写，
    绝不作为常规路径。空区间列表是「探测成功但全片无静音」的真实结果，不是缓存缺失。

    Spec 19 Invariant #4: 参照永远取自原始输入（绝不使用清洗后的 audio_source）。

    本函数留在 cli 层（控制面）：state 的读取/回写属控制面，① 层门面
    ``asr.run_asr`` 只接收已解析好的参照（ADR-038 D5 第二步 A 块）。
    """
    from . import state as vt_state

    silences: list[tuple[float, float]] | None = None
    try:
        ac = vt_state.get_acoustics(outdir, base)
        if ac is not None and ac.get("silence_intervals") is not None:
            silences = [tuple(iv) for iv in ac["silence_intervals"]]
    except Exception:  # noqa: BLE001 - state 是增强，永不阻断
        silences = None
    if silences is None:
        try:
            prof = analyze_audio(input_path)
            silences = prof.silence_intervals if prof.ok else None
            if prof is not None and prof.ok:
                try:
                    vt_state.record_acoustics(outdir, base, prof)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            silences = None
    return silences


# T2 人声分离 + 裁决一意图闸已下沉到 ① 层门面（ADR-038 D5 第二步 A 块）：
#   ``asr._vocal_sep_step``（编排）/ ``asr._gate_vsep``（显式意图硬停）。
# cli 不再持有第二份实现（S5 单一来源）。


def cmd_transcribe(args: argparse.Namespace) -> int:
    """转写薄壳（ADR-038 D5 第二步 A 块）。

    ① 层编排已收归 ``asr.run_asr``；本函数只做四件事：

      1. **入口前置**：ffmpeg/ffprobe 是硬依赖（缺失即 exit 3，不进入编排）
      2. **参数归一化**：``args`` + ``Config`` → ``AsrRequest`` / ``TranscriberConfig``
         （``asr.py`` 刻意不接收 Namespace，避免反向耦合 cli 入口壳）
      3. **静音参照解析**：state 优先（控制面，ADR-035 M2）
      4. **控制面记账**：audio_source 落盘 + transcribe stage 落 state + 异常→退出码
    """
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
         "demucs_model": getattr(args, "demucs_model", None),
         "align": getattr(args, "align", None)},
        cwd=os.getcwd(),
    )
    cfg.model = _resolve_model_path(cfg.model)

    from .asr import AsrRequest, TranscriberConfig, run_asr

    allow_degrade = getattr(args, "allow_degrade", False)
    request = AsrRequest(
        input_path=input_path, outdir=outdir, base=base,
        config=TranscriberConfig(
            model=cfg.model, chunk=cfg.chunk, threads=args.threads,
            lang=cfg.lang,
            vad_threshold=getattr(args, "vad_threshold", None),
            use_vad=getattr(args, "vad", False),
            adaptive_vad=getattr(args, "adaptive_vad", False),
            device=cfg.device, compute_type=cfg.compute_type,
            # T2 / ADR-017: 显式 flag 或配置任一为真即视为请求分离
            separate_vocals=bool(getattr(args, "separate_vocals", False)
                                 or cfg.separate_vocals),
            vocal_sep_model=(getattr(args, "demucs_model", None)
                             or cfg.demucs_model or "htdemucs"),
            # T4 (ADR-028 / Spec 22): forced-acoustic-alignment backend.
            align_backend=cfg.align,
            align_allow_degrade=allow_degrade,
        ),
        # 后处理参数与开关（搬迁前散读 args.* / cfg.*，现集中归一化）
        merge_max_dur=cfg.merge_max_dur,
        merge_max_gap=cfg.merge_max_gap,
        merge_max_chars=cfg.merge_max_chars,
        merge=cfg.merge_enabled and not args.no_merge,
        split=not getattr(args, "no_split", False),
        snap_drift=not getattr(args, "no_drift_snap", False),
        audit=not getattr(args, "no_audit", False),
        review=not getattr(args, "no_review", False),
        g3=not getattr(args, "no_g3", False),
        allow_degrade=allow_degrade,
    )

    # ADR-012 / Spec 19 Invariant #4: 独立静音参照取自原始输入（绝不用清洗后的音轨），
    # 同一份喂 merge 与 fill_gaps。state 的读取/回写属控制面，留在本层。
    silences = _resolve_silence_reference(input_path, outdir, base)

    try:
        outcome = run_asr(request, silence_intervals=silences, progress=print)
        # ADR-035 M2: T2 分离产物关联显式落盘（artifact "audio_source"），
        # 下游 resegment / verify / fill_gaps 不再靠 separate_fingerprint 反推。
        if outcome.audio_source:
            try:
                from . import state as vt_state
                vt_state.record_audio_source(
                    outdir, base, vocals_wav=outcome.audio_source,
                    vsep_backend=outcome.vsep_backend,
                    vsep_model=outcome.vsep_model,
                    vsep_input_hash=outcome.vsep_input_hash)
            except Exception:  # noqa: BLE001 - state 是增强，永不阻断
                pass
        _record_transcribe_stage(outcome.segments_path, video=input_path)
        return EXIT_OK
    except GateFail:
        # 裁决一：显式意图硬停（如 --separate-vocals 缺 demucs）必须冒泡到
        # cli.main → exit 8，不得被下面的兜底吞成 EXIT_RUNTIME。
        # （搬迁前 _vocal_sep_step 在 try 之外，同等效果；此处显式放行。）
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        # E3: a corrupt/truncated model snapshot can pass the existence check but
        # fail at load time. Surface a deterministic fix and bail with EXIT_MISSING_DEP
        # rather than a raw traceback.
        if "model" in msg and ("load" in msg or "corrupt" in msg or "not a zip" in msg
                               or "unexpected" in msg or "checksum" in msg):
            print("[error] model load failed (cache may be corrupt).", file=sys.stderr)
            print("        fix: video-translate setup   # re-downloads large-v3", file=sys.stderr)
            return EXIT_MISSING_DEP
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
        prepare_translate_task(segments, task_path,
                                persona=cfg.persona if cfg.persona != DEFAULT_PERSONA else None,
                                glossary=glossary_text, source=cfg.source,
                                full_transcript=cfg.full_transcript, style=cfg.style)
        base = _derive_base(segments)
        outdir = str(Path(out).parent)
        print(_AGENT_TRANSLATE_INSTRUCTIONS.format(
            task=task_path, segments=segments, out=out, outdir=outdir, base=base))
        _print_pipeline_next(outdir, base)
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


def _enforce_generate_gate(
    segments_path: str,
    zh_path: str,
    *,
    base: str,
    outdir: str,
    allow_degrade: bool = False,
) -> None:
    """control plane §2.1 静默点 4: generate 前置强制校验。

    只依赖 segments + zh 两个文件本身（防绕过核心——就算删掉 state 文件闸门照
    常生效），state 仅增强（陈旧检测）：

      1. zh 覆盖率 100%（每个 en segment index 都有 zh 项）——漏行/漏 index 阻断；
      2. en/zh 段数匹配（对齐收紧段边界 → merge 改变段数 → 旧翻译按 index 错行）；
      3. verify_align 位移检测命中 → 阻断（原 cli.py:697-709 的 warning 升级）；
      4. state 存在时比对 segments_sha：segments 在翻译后被改变 = 翻译陈旧 → 阻断。

    逃生门：--allow-degrade / --no-align-check 显式放行（记录在 state，不留静默）。
    """
    import json as _json

    # ---- 1 + 2: 覆盖率 100% + 段数匹配（纯文件校验，不依赖 state） ----
    with open(segments_path, encoding="utf-8") as _f:
        _segs = _json.load(_f)
    with open(zh_path, encoding="utf-8") as _f:
        _zh = _json.load(_f)
    n_segs = len(_segs)
    missing = [i for i in range(n_segs) if str(i) not in _zh]
    if missing:
        raise GateFail(
            f"generate blocked: zh 覆盖不完整，缺失 {len(missing)} 个 index "
            f"({missing[:10]}…)。翻译必须 100% 覆盖全部 {n_segs} 个 en segment。",
            "用 `video-translate translate --segments <base>.segments_en.json "
            "--out <base>.zh_segments.json` 补齐缺失行后重跑。",
        )
    # 段数匹配：zh 的 key 数必须 >= en 段数（多风格允许 style 后缀 key 分开存）
    if len(_zh) != n_segs:
        raise GateFail(
            f"generate blocked: zh 有 {len(_zh)} 项但 en 有 {n_segs} 段——翻译与 "
            f"当前 segments 不匹配（对齐/重转写后段数可能变化）。",
            "重新翻译（对齐后段边界变化必须重译，SRT 才能按 index 对齐），"
            "或 --no-align-check 显式放行（记录在 state）。",
        )

    # ---- 3: verify_align 位移检测 ----
    if not allow_degrade:
        from .verify_align import report as _align_report
        if not _align_report(_segs, _zh):
            raise GateFail(
                "generate blocked: verify_align 检测到 zh/en 索引漂移"
                "（行错位）。",
                "核对被标记的 range；确认 zh 是按当前 segments 的 index 顺序翻译。",
            )

    # ---- 4: segments_sha 陈旧检测（state 增强，不是闸门前提） ----
    try:
        from .state import load as _state_load
        from .state import segment_sha as _sha
        st = _state_load(outdir, base)
        if st:
            trans_sha = st.get("stages", {}).get("transcribe", {}).get("segments_sha")
            current_sha = _sha(segments_path)
            if trans_sha and trans_sha != current_sha:
                raise GateFail(
                    "generate blocked: segments 在转写后被改动，翻译对应的不是"
                    "当前 segments（陈旧）。",
                    "重新翻译后再 generate（或显式放行）。",
                )
    except GateFail:
        raise
    except Exception:  # noqa: BLE001 - state 损坏不应阻断纯文件闸门
        pass


def _record_transcribe_stage(segs_path: str, video: str | None = None) -> None:
    """Transcription 完成落盘（control plane 静默点 1/3）：state 链位置 + 段指纹。

    ``transcribe.segments_sha`` is the anchor the generate stale-translation
    gate compares against. Best-effort: a state failure never blocks the
    pipeline (gates only depend on the artifact files themselves).
    """
    try:
        from . import state as vt_state
        outdir = os.path.dirname(os.path.dirname(os.path.abspath(segs_path))) or "."
        base = _derive_base(segs_path)
        st = vt_state.ensure_state(outdir, base, video=video)
        vt_state.set_stage(st, "translate")
        vt_state.record_stage(st, "transcribe", status="ok",
                              segments_sha=vt_state.segment_sha(segs_path),
                              n_segments=len(load_json(segs_path)))
        vt_state.save(outdir, base, st)
    except Exception:  # noqa: BLE001 - state is an enhancement, never a gate
        pass


def _record_run_decisions(args: argparse.Namespace, segments_path: str) -> None:
    """Record run-level decisions with origin grading (裁决一 / 静默点 1).

    Explicit flags become ``origin="explicit"``; defaults stay visible as
    ``origin="default"`` instead of being transient doctor prints.
    """
    try:
        from . import state as vt_state
        outdir = os.path.dirname(os.path.dirname(os.path.abspath(segments_path))) or "."
        base = _derive_base(segments_path)
        st = vt_state.ensure_state(outdir, base)

        def dec(key: str, value: object, default: object) -> None:
            vt_state.record_decision(
                st, key, value,
                origin="explicit" if value != default else "default")

        dec("engine", getattr(args, "engine", None), None)
        dec("style", getattr(args, "style", None), None)
        dec("align", getattr(args, "align", None), None)
        dec("separate_vocals", bool(getattr(args, "separate_vocals", False)), False)
        dec("vad", bool(getattr(args, "vad", False)), False)
        dec("adaptive_vad", bool(getattr(args, "adaptive_vad", False)), False)
        vt_state.save(outdir, base, st)
    except Exception:  # noqa: BLE001 - state is an enhancement, never a gate
        pass


def _ensure_audio_profile(outdir: str, base: str, input_path: str,
                          cfg: Any) -> dict[str, Any]:
    """P0 画像兜底 (ADR-032 / ADR-035 M2): analyze once, persist once.

    No-op when a snapshot already exists in state. Shared by
    ``_resolve_routing`` (run) and ``cmd_pipeline`` (decision point) — this is
    the single producer of the audio-profile / acoustics snapshot, so
    silencedetect 与 duration 全链只算一次（T10 铁律：消灭重算）。
    """
    from . import state as vt_state
    from .audio_profile import analyze_audio, profile_recommendation

    prof_snap = vt_state.get_audio_profile(outdir, base)
    if prof_snap is not None:
        return prof_snap
    try:
        prof = analyze_audio(input_path)
    except Exception:  # noqa: BLE001
        prof = None
    rec = profile_recommendation(
        prof, default_style=getattr(cfg, "style", None) or "film",
        duration=prof.duration if prof else None,
    )
    vt_state.record_audio_profile(outdir, base, rec)
    # ADR-035 M2: 兜底画像时同样落盘声学事实（此路径也是生产者之一）。
    vt_state.record_acoustics(outdir, base, prof)
    return rec.to_dict()


def _resolve_routing(
    args: argparse.Namespace,
    outdir: str,
    base: str,
    input_path: str,
    cfg: Any,
) -> tuple[dict[str, Any] | None, str]:
    """P0->P1 自动路由 (ADR-032): 画像兜底 + 三决策合并 + 落盘。

    合并优先级: CLI 显式 flag > 已落盘 routing > 画像推荐兜底。

    * 无 ``audio_profile`` 快照时自动 ``analyze_audio`` 并落盘——绝不裸跑无画像。
    * 最终三决策以 origin 分级 (``explicit``=用户/CLI 拍板, ``profile``=画像推荐)
      落盘到 ``decisions.routing``，使 P0→P1 交接可审计。

    ``--require-profile`` (可选硬闸): 要求 P0→P1 必须经人工决策点，即已存在
    ``origin=explicit`` 的 routing，否则返回 ``(None, "explicit")``，调用方据此
    退出 ``EXIT_GATE_FAIL``——防止 Agent 失守时裸跳过决策点。

    Returns:
        ``(final, origin)``；硬闸未通过时 ``final`` 为 ``None``。
    """
    from . import state as vt_state

    # 可选硬闸: 必须经人工决策点 (origin=explicit 的 routing)。
    if getattr(args, "require_profile", False):
        st = vt_state.load(outdir, base)
        entry = st.get("decisions", {}).get(vt_state.ROUTING_KEY)
        if not isinstance(entry, dict) or entry.get("origin") != "explicit":
            print("[gate] --require-profile: P0→P1 必须经由人工决策点 "
                  "(decisions.routing origin=explicit)，当前缺失。")
            print("       请先运行 `doctor --video <video>` 完成画像，并让 Agent 在决策点 "
                  "选择/确认路由后 run。")
            return None, "explicit"

    # 1. 画像兜底: 无快照则自动补画像并落盘 (绝不裸跑无画像参数)。
    prof_snap = _ensure_audio_profile(outdir, base, input_path, cfg)

    # 2. 已落盘 routing (用户/Agent 决策点的产物)。
    routing = vt_state.get_routing(outdir, base)

    # 3. 合并: CLI 显式 flag > routing > 画像推荐兜底。
    #    store_true 的 flag 只有显式传了才为 True；default=None 的字段非 None 即显式。
    def cbool(name: str) -> bool:
        return bool(getattr(args, name, False))

    def cset(name: str) -> bool:
        return getattr(args, name, None) is not None

    explicit: list[str] = []

    if cset("style"):
        style = args.style
        explicit.append("style")
    elif routing:
        style = routing["style"]
    else:
        style = prof_snap.get("style", getattr(cfg, "style", None) or "film")

    if cbool("vad"):
        vad = True
        explicit.append("vad")
    elif routing:
        vad = bool(routing["vad"])
    else:
        vad = bool(prof_snap.get("vad", False))

    if cbool("adaptive_vad"):
        adaptive_vad = True
        explicit.append("adaptive_vad")
    elif routing:
        adaptive_vad = bool(routing["adaptive_vad"])
    else:
        adaptive_vad = bool(prof_snap.get("adaptive_vad", False))

    if cbool("separate_vocals"):
        separate_vocals = True
        explicit.append("separate_vocals")
    elif routing:
        separate_vocals = bool(routing["separate_vocals"])
    else:
        separate_vocals = bool(prof_snap.get("separate_vocals", False))

    if cset("vad_threshold"):
        vad_threshold = args.vad_threshold
        explicit.append("vad_threshold")
    elif routing and routing.get("vad_threshold") is not None:
        vad_threshold = routing["vad_threshold"]
    else:
        vad_threshold = prof_snap.get("vad_threshold")

    # 4. origin 分级: 有显式 flag 即 explicit; 否则沿用 routing 的 origin; 否则 profile。
    if explicit:
        origin = "explicit"
    elif routing:
        st = vt_state.load(outdir, base)
        entry = st.get("decisions", {}).get(vt_state.ROUTING_KEY)
        origin = entry.get("origin", "profile") if isinstance(entry, dict) else "profile"
    else:
        origin = "profile"

    final = {
        "style": style,
        "vad": vad,
        "adaptive_vad": adaptive_vad,
        "separate_vocals": separate_vocals,
        "vad_threshold": vad_threshold,
    }
    # 落盘最终三决策 (可追溯)。显式转换确保类型稳定 (final 的 value 是混合类型)。
    vt_state.record_routing(
        outdir, base,
        style=str(style), vad=bool(vad), adaptive_vad=bool(adaptive_vad),
        separate_vocals=bool(separate_vocals),
        vad_threshold=(float(vad_threshold) if vad_threshold is not None else None),
        origin=origin,
    )
    return final, origin


def _record_generate_stage(segments_path: str, outdir: str,
                           base: str, style: object = None) -> None:
    """Generate 完成落盘：generate ok + translate 指纹锚（generate 所校验的段）。"""
    try:
        from . import state as vt_state
        st = vt_state.ensure_state(outdir, base)
        vt_state.set_stage(st, "verify")
        vt_state.record_stage(st, "generate", status="ok", style=style)
        vt_state.record_stage(st, "translate", status="ok",
                              segments_sha=vt_state.segment_sha(segments_path))
        vt_state.save(outdir, base, st)
    except Exception:  # noqa: BLE001 - state is an enhancement, never a gate
        pass


def _print_pipeline_next(outdir: str, base: str,
                         video: str | None = None) -> None:
    """Append the NEXT block at every collaboration stop (run/generate tails)."""
    from .pipeline import build_ctx, render_next, resolve_position
    print(render_next(resolve_position(build_ctx(outdir, base, video=video))))


def cmd_generate(args: argparse.Namespace) -> int:
    base = args.base or _derive_base(args.segments)
    gap = getattr(args, "gap", 0.0) or 0.0
    min_dur = getattr(args, "min_dur", 0.0) or 0.0
    offset = getattr(args, "offset", 0.0) or 0.0
    tail = getattr(args, "tail", 0.0) or 0.0
    flat = getattr(args, "flat", False)
    prune_old = getattr(args, "prune_old", False)
    display_merge = getattr(args, "display_merge", False)
    dm_gap = getattr(args, "display_merge_gap", None)
    dm_max_dur = getattr(args, "display_merge_max_dur", None)
    dm_max_chars = getattr(args, "display_merge_max_chars", None)
    dm_max_zh = getattr(args, "display_merge_max_zh", None)
    dm_short_dur = getattr(args, "display_merge_short_dur", None)
    dm_short_words = getattr(args, "display_merge_short_words", None)

    # control plane 静默点 4: enforce 前置校验（覆盖 100% + 段数匹配 + 位移 +
    # sha 陈旧）。--no-align-check / --allow-degrade 是显式逃生门。
    allow_degrade = (
        getattr(args, "allow_degrade", False)
        or getattr(args, "no_align_check", False)
    )
    outdir = args.outdir or Path(args.segments).parent
    try:
        _enforce_generate_gate(
            args.segments, args.zh, base=base, outdir=str(outdir),
            allow_degrade=allow_degrade,
        )
    except GateFail:
        raise  # cli.main 统一 catch → exit 8

    try:
        from .generate import generate_subtitles
        generate_subtitles(args.segments, args.zh, args.outdir, base=base,
                           gap=gap, min_dur=min_dur, offset=offset, tail=tail,
                           flat=flat, prune_old=prune_old, style=args.style,
                           display_merge=display_merge,
                           dm_gap=dm_gap, dm_max_dur=dm_max_dur,
                           dm_max_chars=dm_max_chars, dm_max_zh=dm_max_zh,
                           dm_short_dur=dm_short_dur,
                           dm_short_words=dm_short_words)
        _record_generate_stage(args.segments, str(outdir), base,
                               style=getattr(args, "style", None))
        _print_pipeline_next(str(outdir), base)
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
    # ADR-031 D8: a fresh `run` resets the verify retry counter (local re-runs
    # like resegment/generate must NOT reset — only a full pipeline run counts).
    from .state import reset_verify_attempts
    reset_verify_attempts(outdir, base)
    segments = os.path.join(workdir(outdir, base), f"{base}.segments_en.json")
    zh = os.path.join(workdir(outdir, base), f"{base}.zh_segments.json")
    pending = os.path.join(workdir(outdir, base), f"{base}.agent_pending.json")
    task = os.path.join(workdir(outdir, base), f"{base}.translate_task.json")
    cfg = resolve_config(
        {"model": args.model, "chunk": args.chunk, "lang": args.lang,
         "proxy": args.proxy, "src": args.src, "tgt": args.tgt,
         "engine": args.engine, "merge_max_chars": getattr(args, "merge_max_chars", None),
         "glossary": getattr(args, "glossary", None),
         "source": getattr(args, "source", None),
         "style": getattr(args, "style", None),
         "device": getattr(args, "device", None),
         "compute_type": getattr(args, "compute_type", None),
         "align": getattr(args, "align", None)},
        cwd=os.getcwd(),
    )

    # ADR-032: P0->P1 自动路由——画像兜底 (无快照自动补并落盘) + 三决策合并
    # (CLI flag > routing > 画像推荐)。即使 Agent 失守直接 run 也绝不裸跑无画像。
    final, origin = _resolve_routing(args, outdir, base, input_path, cfg)
    if final is None:
        return EXIT_GATE_FAIL
    args.vad = final["vad"]
    args.adaptive_vad = final["adaptive_vad"]
    args.separate_vocals = final["separate_vocals"]
    args.vad_threshold = final["vad_threshold"]
    args.style = final["style"]
    cfg.style = final["style"]
    print(f"[route] style={final['style']} vad={final['vad']} "
          f"adaptive_vad={final['adaptive_vad']} "
          f"separate_vocals={final['separate_vocals']} "
          f"vad_threshold={final['vad_threshold']} (origin={origin})")

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
            no_review=getattr(args, "no_review", False),
            no_g3=getattr(args, "no_g3", False),
            no_drift_snap=getattr(args, "no_drift_snap", False),
            device=cfg.device, compute_type=cfg.compute_type,
            # T2 / ADR-017: forward the vocal-separation flags verbatim
            separate_vocals=getattr(args, "separate_vocals", False),
            demucs_model=getattr(args, "demucs_model", None),
            # T4 (ADR-028 / Spec 22): forward alignment backend
            align=cfg.align,
            allow_degrade=getattr(args, "allow_degrade", False),
        ))
        if rc != EXIT_OK:
            return rc
    _record_run_decisions(args, segments)

    if cfg.engine == "agent" and "translate" not in skip:
        from .translate import prepare_translate_task
        glossary_text = None
        if cfg.glossary:
            from .glossary import load_glossary
            glossary_text = load_glossary(cfg.glossary)
        # Multi-style: emit one task per style (suffixed filenames), else single.
        persona_override = cfg.persona if cfg.persona != DEFAULT_PERSONA else None
        if "," in cfg.style:
            styles = [s.strip() for s in cfg.style.split(",") if s.strip()]
            written = prepare_translate_task(
                segments, None, persona=persona_override,
                glossary=glossary_text, source=cfg.source,
                full_transcript=cfg.full_transcript,
                styles=styles, outdir=outdir, base=base)
            for t in written:
                print(_RUN_AWAITING_AGENT_INSTRUCTIONS.format(
                    task=t, segments=segments,
                    zh=os.path.join(workdir(outdir, base), f"{base}.{_style_of(t)}.zh_segments.json"),
                    outdir=outdir, base=base))
            _print_pipeline_next(outdir, base, video=input_path)
            return EXIT_AWAITING_AGENT
        prepare_translate_task(segments, task, persona=persona_override,
                                glossary=glossary_text, source=cfg.source,
                                full_transcript=cfg.full_transcript, style=cfg.style)
        print(_RUN_AWAITING_AGENT_INSTRUCTIONS.format(
            task=task, segments=segments, zh=zh, outdir=outdir, base=base))
        _print_pipeline_next(outdir, base, video=input_path)
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
            display_merge=getattr(args, "display_merge", False),
            display_merge_gap=getattr(args, "display_merge_gap", None),
            display_merge_max_dur=getattr(args, "display_merge_max_dur", None),
            display_merge_max_chars=getattr(args, "display_merge_max_chars", None),
            display_merge_max_zh=getattr(args, "display_merge_max_zh", None),
            display_merge_short_dur=getattr(args, "display_merge_short_dur", None),
            display_merge_short_words=getattr(args, "display_merge_short_words", None),
        ))
        if rc != EXIT_OK:
            return rc
    return EXIT_OK


# --------------------------- T8 / ADR-033 / Spec 24 -------------------------
# `pipeline` — the idempotent single-entry advancer. Decision logic lives in
# pipeline.next_action (pure); this executor only maps actions onto the
# existing run/generate/verify primitives (never re-implements their gates).


def _pipeline_run_ns(args: argparse.Namespace, outdir: str, base: str,
                     extra: list[str] | None = None) -> argparse.Namespace:
    """Build a full `run` namespace from pipeline args (delegation, not copy).

    Re-parsing through ``build_parser`` inherits every run-flag default, so
    the delegated executor behaves byte-identically to a direct `run` call.
    """
    argv = ["run", args.input, "--outdir", outdir, "--base", base]
    for flag, attr in (("--style", "style"), ("--engine", "engine"),
                       ("--vad-threshold", "vad_threshold"),
                       ("--demucs-model", "demucs_model")):
        val = getattr(args, attr, None)
        if val is not None:
            argv += [flag, str(val)]
    for flag, attr in (("--vad", "vad"), ("--adaptive-vad", "adaptive_vad"),
                       ("--separate-vocals", "separate_vocals"),
                       ("--allow-degrade", "allow_degrade")):
        if getattr(args, attr, False):
            argv.append(flag)
    if extra:
        argv += extra
    return build_parser().parse_args(argv)


def cmd_pipeline(args: argparse.Namespace) -> int:
    """T8 / ADR-033 / Spec 24: idempotent single-entry advancer.

    Resolves the current position, executes exactly the next step, and stops
    at the next collaboration stop point. Re-running is always safe: routing,
    chunk caches and state fingerprints make every step resumable. The
    underlying run/generate/verify primitives keep their own gates and exit
    codes (this executor only dispatches and propagates).
    """
    from . import state as vt_state
    from .pipeline import build_ctx, next_action, resolve_position

    input_path = args.input
    outdir = args.outdir or _default_outdir(input_path)
    base = args.base or _default_base(input_path)
    cfg = resolve_config({"style": getattr(args, "style", None)},
                         cwd=os.getcwd())
    prompt_mode = getattr(args, "prompt", None) or cfg.prompt
    if prompt_mode not in VALID_PROMPTS:  # defensive; parser already limits
        prompt_mode = "always"

    ctx = build_ctx(outdir, base, video=input_path)
    pos = resolve_position(ctx)
    routing = vt_state.get_routing(outdir, base)
    # Explicit flags satisfy the decision point: the user already picked, and
    # cmd_run's _resolve_routing persists them with origin=explicit.
    has_explicit = (getattr(args, "style", None) is not None
                    or getattr(args, "vad", False)
                    or getattr(args, "adaptive_vad", False)
                    or getattr(args, "separate_vocals", False))
    if has_explicit and routing is None:
        routing = {"origin": "explicit"}
    action = next_action(pos, prompt_mode=prompt_mode, routing=routing)

    if action == "done":
        print("[NEXT] pipeline complete — bilingual SRT verified.")
        if pos.get("pending_agent"):
            print("  (semantic reread still pending: write "
                  f"{base}.semantic_reread_result.json)")
        return EXIT_OK

    if action == "stop_decision_point":
        # Preflight first (ADR-035 M2: analyze once, persist once) so the
        # rationale is available even if the user never proceeds.
        prof = _ensure_audio_profile(outdir, base, input_path, cfg)
        print("[decision point] translation style only (ADR-034: VAD / "
              "vocal separation are explicit-flag overrides; default bare run)")
        rationale = prof.get("rationale") if isinstance(prof, dict) else None
        if rationale:
            print(f"  profile: {rationale}")
        print("  pick one: film (default, 影视二创) / literal (忠实直译) / "
              "bilingual_study (双语精读)")
        print(f"  reply, then run: uv run video-translate pipeline "
              f"\"{input_path}\" --style <picked>")
        print("  no reply by timeout -> just re-run `pipeline` (auto-route, "
              "origin=profile)")
        print("[NEXT] stage=preflight  (STOP POINT — decision point: "
              "style only)")
        return EXIT_AWAITING_AGENT

    if action == "transcribe":
        extra = (["--require-profile"]
                 if prompt_mode == "require-profile" else None)
        return cmd_run(_pipeline_run_ns(args, outdir, base, extra=extra))

    if action == "stop_translate":
        task = os.path.join(workdir(outdir, base), f"{base}.translate_task.json")
        if not os.path.isfile(task):
            # Interrupted before task emission: self-heal by re-emitting the
            # task via `run --skip transcribe` instead of a dangling pointer.
            return cmd_run(_pipeline_run_ns(
                args, outdir, base, extra=["--skip", "transcribe"]))
        print(_RUN_AWAITING_AGENT_INSTRUCTIONS.format(
            task=task, segments=ctx["segments"], zh=ctx["zh"],
            outdir=outdir, base=base))
        _print_pipeline_next(outdir, base, video=input_path)
        return EXIT_AWAITING_AGENT

    if action == "generate":
        extra = []
        if getattr(args, "display_merge", False):
            extra.append("--display-merge")
            for flag, attr in (
                ("--display-merge-gap", "display_merge_gap"),
                ("--display-merge-max-dur", "display_merge_max_dur"),
                ("--display-merge-max-chars", "display_merge_max_chars"),
                ("--display-merge-max-zh", "display_merge_max_zh"),
                ("--display-merge-short-dur", "display_merge_short_dur"),
                ("--display-merge-short-words", "display_merge_short_words"),
            ):
                val = getattr(args, attr, None)
                if val is not None:
                    extra += [flag, str(val)]
        return cmd_generate(build_parser().parse_args([
            "generate", "--segments", ctx["segments"], "--zh", ctx["zh"],
            "--outdir", outdir, "--base", base] + extra))

    # action == "verify"
    return cmd_verify(build_parser().parse_args([
        "verify", "--segments", ctx["segments"], "--zh", ctx["zh"],
        "--video", input_path,
    ]))


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
            outdir = os.path.dirname(os.path.dirname(os.path.abspath(segs_path))) or "."
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
            # 裁决一: explicit --separate-vocals on resegment + demucs missing
            # => hard-stop (exit 8) unless --allow-degrade explicitly opts out.
            from .asr import _gate_vsep
            _gate_vsep(
                getattr(args, "allow_degrade", False),
                "--separate-vocals requested on resegment but the demucs "
                "package is not installed.",
                "Run `uv sync` (`pip install -e .` fallback), or drop "
                "--separate-vocals / explicitly bypass with --allow-degrade.",
            )

    from .transcribe import transcribe_window
    from .fill_gaps import _is_recovered_hallucination
    decoded: list[dict[str, Any]] = []
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
            decoded.append(seg)
        print(f"    -> {len(window_segs)} clean segment(s)", flush=True)

    # drop originals overlapping any window, then merge + sort by start
    kept = [
        seg for seg in segments
        if not any(seg["start"] < we and seg["end"] > ws for (ws, we) in windows)
    ]
    # ADR-031 D1: spliced re-decodes go through the SAME hallucination guard as
    # fill_gaps recoveries (ADR-021). kathy_meta_vlog: "We'll be right back."
    # (no_speech_prob=0.906) and "Wait." (0.851) escaped because resegment had
    # no guard. Interception is PRINTED — never silent.
    new_segs: list[dict[str, Any]] = []
    dropped = 0
    for seg in decoded:
        if _is_recovered_hallucination(seg, kept + new_segs):
            dropped += 1
            print(f"[resegment] hallucination guard DROPPED "
                  f"{float(seg.get('start') or 0):.2f}-"
                  f"{float(seg.get('end') or 0):.2f}s {seg.get('text')!r} "
                  f"(no_speech_prob={seg.get('no_speech_prob')}, "
                  f"avg_logprob={seg.get('avg_logprob')}) — window energy was "
                  f"likely BGM/noise, not speech", flush=True)
            continue
        seg["origin"] = "resegment"  # ADR-031 D2: recovered-class visibility
        new_segs.append(seg)
    merged = kept + new_segs
    merged.sort(key=lambda s: s["start"])
    # Whisper ALL-CAPS artifact on loud speech; normalize the re-transcribed
    # (recovered) segments too so segments_en.json stays consistent.
    from .transcribe import _normalize_caps
    for _s in merged:
        if isinstance(_s, dict) and "text" in _s:
            _s["text"] = _normalize_caps(_s.get("text") or "")
    save_json(segs_path, merged, indent=0)
    # Control plane: resegment is a legitimate transcription-layer amendment —
    # refresh the segments_sha anchor so the generate stale-translation gate
    # compares against the NEW baseline (otherwise a legal fix would be
    # indistinguishable from "forgot to re-translate"). Agent still must
    # re-translate the amended segments; the chain stops at `translate`.
    _record_transcribe_stage(segs_path, video=args.video)
    print(f"[resegment] done: {len(segments)} -> {len(merged)} segments "
          f"({len(new_segs)} re-transcribed as '{args.lang}', "
          f"{dropped} hallucination-guarded) -> {segs_path}")
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
    os.makedirs(workdir(outdir, base), exist_ok=True)
    tmp_segs = os.path.join(workdir(outdir, base), "_backfill_segments.json")
    save_json(tmp_segs, pending, indent=0)
    task_path = os.path.join(workdir(outdir, base), f"{base}.backfill_task.json")
    from .translate import prepare_translate_task
    prepare_translate_task(tmp_segs, task_path,
                           persona=cfg.persona if cfg.persona != DEFAULT_PERSONA else None,
                           index_key="index", style=cfg.style)
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


def _style_of(task_path: str) -> str:
    """Extract the style suffix from a `<base>.<style>.translate_task.json` path."""
    name = os.path.basename(task_path)
    for suf in (".translate_task.json", ".backfill_task.json"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    # name is now "<base>.<style>" or "<base>"
    if "." in name:
        return name.rsplit(".", 1)[1]
    return "film"


def _find_generate_opts(segments_path: str) -> dict | None:
    """Locate the display-window sidecar written by `generate` (Spec 18)."""
    seg_dir = os.path.dirname(segments_path)
    base = _derive_base(segments_path)
    # ADR-037: generate_opts 随 segments 落在 workdir，故唯一候选为 seg_dir 下。
    candidates = [os.path.join(seg_dir, base + ".generate_opts.json")]
    for c in candidates:
        if os.path.exists(c):
            try:
                return load_json(c)
            except Exception:  # noqa: BLE001
                return None
    return None


def _parse_reread_result(raw: object) -> list[tuple[int, str]]:
    """Parse ``<base>.semantic_reread_result.json`` into flagged (index, why).

    Tolerant of both shapes the agent may write:
      {"12": "wrong: dropped the second clause"}     (value = verdict)
      {"12: wrong: dropped the second clause": "…"}  (schema-illustration key)
    Entries without a recognizable verdict are flagged conservatively — a gate
    must fail loud, never silently clear an unreadable verdict.
    """
    if not isinstance(raw, dict):
        return [(-1, f"unparseable reread result: {raw!r}")]
    flags: list[tuple[int, str]] = []
    for k, v in raw.items():
        ks = str(k)
        head = ks.strip().split(":")[0].strip()
        idx = int(head) if head.isdigit() else -1
        blob = str(v) if head.isdigit() else f"{ks}: {v}"
        verdict = None
        for w in ("untranslated", "omit", "add", "wrong", "ok"):
            if re.search(rf"\b{w}\b", blob, re.IGNORECASE):
                verdict = w
                break
        if verdict != "ok":
            flags.append((idx, blob.strip() or "(no reason given)"))
    return flags


def _verify_state_hook(segments_path: str, status: str) -> None:
    """Best-effort state update after verify (control plane 静默点 9).

    Marks ``stages.verify.status`` = ok / flagged / pending_agent. NEVER a
    gate: a missing or corrupt state file is tolerated — gates depend only on
    the segments+zh files themselves (state.py contract).
    """
    try:
        from . import state as vt_state
        outdir = os.path.dirname(os.path.dirname(os.path.abspath(segments_path))) or "."
        base = _derive_base(segments_path)
        st = vt_state.ensure_state(outdir, base)
        vt_state.set_stage(st, "verify")
        # ADR-031 D8: record_stage overwrites the verify entry — preserve the
        # attempts counter that increment_verify_attempts set earlier.
        prev_attempts = st.get("stages", {}).get("verify", {}).get("attempts")
        vt_state.record_stage(st, "verify", status=status)
        if prev_attempts is not None:
            st.setdefault("stages", {}).setdefault("verify", {})["attempts"] = prev_attempts
        vt_state.save(outdir, base, st)
    except Exception:  # noqa: BLE001 - state is an enhancement, never a gate
        pass


def cmd_status(args: argparse.Namespace) -> int:
    """Show where the pipeline stands and the single next action (S2).

    Reads the per-base state file when present and falls back to on-disk
    artifacts for legacy directories. ``--json`` emits a machine-parseable
    position blob (agents read this instead of prose).
    """
    from .pipeline import build_ctx, discover_bases, render_next, resolve_position
    outdir = args.outdir
    bases = [args.base] if args.base else discover_bases(outdir)
    if not bases:
        if args.json:
            print(render_next({
                "base": None, "outdir": outdir, "current_stage": "preflight",
                "done": False, "pending_agent": False, "verify_status": None,
                "artifacts": {}, "next_action": {
                    "stage": "preflight",
                    "title": "环境自检 (doctor)",
                    "cli": "uv run video-translate doctor",
                    "stop_point": False, "why": "no pipeline artifacts yet",
                    "pending_agent": False, "blocked_by": []},
            }, as_json=True))
        else:
            print(f"[status] no pipeline artifacts under {outdir} — start "
                  "with `uv run video-translate run <video>` (doctor first "
                  "per AGENTS.md).")
        return EXIT_OK
    if args.base is None and len(bases) > 1:
        print(f"[status] multiple bases found ({', '.join(bases[:5])}…); "
              f"showing the most recent: {bases[0]} (pass --base to pick)",
              file=sys.stderr)
    ctx = build_ctx(outdir, bases[0], video=getattr(args, "video", None))
    pos = resolve_position(ctx)
    print(render_next(pos, as_json=args.json))
    return EXIT_OK


def _reread_all_ok(result_path: str) -> bool:
    """True when a reread result exists AND every verdict is ok."""
    if not os.path.exists(result_path):
        return False
    try:
        return not _parse_reread_result(load_json(result_path))
    except Exception:  # noqa: BLE001 - unreadable result is not "all ok"
        return False


def _find_vocals_wav(segments_path: str) -> str | None:
    """ADR-031 D7: locate the demucs vocals cache co-located with segments."""
    d = os.path.dirname(os.path.abspath(segments_path)) or "."
    base = _derive_base(segments_path)
    for p in sorted(Path(d).glob(f"{base}.*.vocals.wav")):
        return str(p)
    return None


def _classify_uncovered_advisory(
    segments_path: str,
    uncovered: list[tuple[float, float]],
) -> dict[tuple[float, float], dict[str, str]] | None:
    """ADR-031 D7: classify uncovered windows by vocal-track energy (advisory).

    Probes the demucs vocals cache (when present) per window and attaches a
    bgm/speech/ambiguous/unknown verdict, so the next-round decision
    (resegment vs adjudicate-as-BGM) no longer depends on the agent
    improvising volumedetect calls. Purely advisory: uncovered windows are
    already RED on their own.
    """
    vocals = _find_vocals_wav(segments_path)
    if not vocals:
        return None
    try:
        volumes: dict[tuple[float, float], tuple[float | None, float | None]] = {}
        for (s, e) in uncovered:
            try:
                volumes[(s, e)] = probe_volume_window(vocals, s, e)
            except Exception:  # noqa: BLE001 - single-window probe failure
                volumes[(s, e)] = (None, None)
        return classify_uncovered_windows(uncovered, volumes)
    except Exception:  # noqa: BLE001 - classification is advisory, never a gate
        return None


def cmd_verify(args: argparse.Namespace) -> int:
    """Unified self-check gate: acoustic / content / presentation lanes (Spec 18).

    Control plane (§2.1 静默点 5–9):
      - Missing --zh / --video is a usage error (EXIT_ARGS): verify must run
        every lane — a partial self-check must never read as a pass.
      - Strict is the DEFAULT: any lane flag -> EXIT_GATE_FAIL (8). Pass
        --no-strict for report-only mode; the legacy --strict flag is accepted
        as a no-op for backward compatibility.
      - An audio-profile failure or an uncovered-probe failure is a RED
        acoustic lane, never a silent skip.
      - The semantic reread result is consumed when present (non-ok verdicts
        fail the gate); when missing the task is (re)hung and the state file
        is marked ``pending_agent`` — generated SRTs stay untouched.
    """
    segments_path = args.segments
    zh_path = getattr(args, "zh", None)
    video = getattr(args, "video", None)
    # 静默点 5: strict is the default; --no-strict is the explicit escape hatch.
    strict = not getattr(args, "no_strict", False)
    noise = getattr(args, "noise", "-30dB")
    d = getattr(args, "d", 0.3)
    opts_path = getattr(args, "opts", None)
    semantic = not getattr(args, "no_semantic", False)  # ADR-016/V14: ON by default
    semantic_out = getattr(args, "semantic_out", None)

    if not zh_path or not video:
        missing = "--zh" if not zh_path else "--video"
        print(f"[verify] refusing to run: {missing} is required — verify must "
              "execute every lane (acoustic/content/presentation); a partial "
              "self-check would read as a pass.", file=sys.stderr)
        return EXIT_ARGS

    # ---- ADR-031 D8: retry counting + circuit-breaker -------------------------
    from .state import MAX_VERIFY_ATTEMPTS, increment_verify_attempts
    outdir = os.path.dirname(os.path.dirname(os.path.abspath(segments_path))) or "."
    base = _derive_base(segments_path)
    attempts = increment_verify_attempts(outdir, base)
    print(f"[verify] attempt {attempts}/{MAX_VERIFY_ATTEMPTS}", flush=True)
    if attempts > MAX_VERIFY_ATTEMPTS:
        # circuit-breaker: force report-only mode on the 3rd+ attempt
        strict = False
        print("[verify] retry limit reached — switching to report-only mode "
              "(problems listed below; please review manually).", flush=True)

    segments = load_json(segments_path)

    # ADR-041: verify 不再读取 ASR 的置信度字段（低置信道已移除）——裁判只看
    # FFmpeg 独立参照与 cue 的客观几何，不看被测模型的内心活动。
    # （`segments_raw.json` 的置信度仍由①层 review 信号 A 经 _raw_indices 使用。）

    # ---- Lane 1: acoustic -------------------------------------------------
    # 静默点 7: a profile failure is a RED lane — "couldn't check" must never
    # masquerade as "checked, all clear".
    # ADR-035 M2: 声学事实读 preflight 落盘的 state（单一生产，全链只算一次）。
    # 仅当探测参数与生产时一致才复用（noise/d 是 artifact 身份的一部分），
    # 缺失或参数不一致才兜底重算（verify 是消费者，不回写 state）。
    silences: list[tuple[float, float]] = []
    prof_error: str | None = None
    prof = None
    ac_hit = False
    try:
        from . import state as vt_state
        ac = vt_state.get_acoustics(outdir, base)
    except Exception:  # noqa: BLE001 - state 是增强，永不阻断
        ac = None
    if (ac is not None and ac.get("silence_intervals") is not None
            and ac.get("noise") == noise and ac.get("d") == d):
        from .audio_profile import AudioProfile
        prof = AudioProfile(
            mean_vol=ac.get("mean_db"), max_vol=ac.get("max_db"),
            silence_intervals=[tuple(iv) for iv in ac["silence_intervals"]],
            duration=ac.get("duration"), ok=True,
        )
        ac_hit = True
    if prof is None:
        try:
            prof = analyze_audio(video, noise=noise, d=d)
        except Exception as exc:  # noqa: BLE001 - profile probe crashed
            prof = None
            prof_error = f"audio profile probe failed: {exc}"
    if prof is not None:
        if prof.ok:
            silences = prof.silence_intervals
            src = ("cached from preflight profile (ADR-035 单一生产)"
                   if ac_hit else "silencedetect (independent reference)")
            print(f"[verify:acoustic] {len(silences)} silence gap(s) from {src}")
        else:
            prof_error = ("audio profile unavailable (ffmpeg failed or no "
                          "usable signal)")

    offset = 0.0
    if opts_path and os.path.exists(opts_path):
        opts = load_json(opts_path)
    else:
        opts = _find_generate_opts(segments_path) or {}
    offset = float(opts.get("offset", 0.0) or 0.0)
    acoustic_issues = verify_acoustic(segments, silences, offset=offset) if silences else []
    # ADR-031 D4/D5（D3 已由 ADR-041 移除）: adjacent-overlap / prefix-collision
    # inspection runs unconditionally — it does not depend on the silencedetect
    # reference (kathy_meta_vlog: the "Is he" prefix riding on "busy." escaped
    # every silence-based check). 这是 cue 的客观几何，非模型自证。
    acoustic_issues = acoustic_issues + find_adjacent_overlaps(segments)

    # ADR-016 (T2b): uncovered-audio detection — audio present but no cue.
    # 静默点 8: a probe exception is RED with the reason, never a swallowed [].
    uncovered: list[tuple[float, float]] = []
    uncovered_error: str | None = None
    if silences:
        try:
            dur = probe_duration(video)
            uncovered = find_uncovered_speech(segments, silences, dur)
        except Exception as exc:  # noqa: BLE001 - failure must stay visible
            uncovered_error = f"uncovered-audio probe failed: {exc}"

    # ---- Lane 2: content (reuses validate_zh + verify_align) ---------------
    content_flags = 0
    mixed: list[dict[str, Any]] = []
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
    # ADR-040: 内容层巡检「文本内含换行」。write 边界已压平，但 zh / segments
    # 是可被 Agent / 人工直接编辑的产物，会绕过清洗——故对产物本身再查一遍。
    linebreaks: list[dict[str, Any]] = []
    for i, s in enumerate(segments):
        fields = [name for name, val in (("text", s.get("text")),
                                         ("zh", zh.get(i, "")))
                  if find_embedded_linebreaks(val)]
        if fields:
            linebreaks.append({"index": i, "fields": fields})
    if linebreaks:
        content_flags += 1

    # ---- Lane 3: presentation ---------------------------------------------
    first_start = None
    words0 = segments[0].get("words") if segments else None
    if words0:
        first_start = words0[0]["start"]
    elif segments:
        first_start = segments[0].get("start")
    presentation_issues = verify_presentation(opts, first_start, silences, offset=offset)

    # ---- report -----------------------------------------------------------
    any_flag = (bool(acoustic_issues) or bool(uncovered)
                or prof_error is not None or uncovered_error is not None
                or content_flags > 0 or bool(presentation_issues))
    print(f"\n=== verify report ===")
    print(f"  acoustic : {len(acoustic_issues)} issue(s)"
          + ("" if (prof_error is None and uncovered_error is None)
             else " (probe FAILED — lane is RED)"))
    for it in acoustic_issues:
        st, en = it.get("start"), it.get("end")
        span = (f" {st:.2f}->{en:.2f}s"
                if isinstance(st, (int, float)) and isinstance(en, (int, float))
                else "")
        extra = ""
        if it["type"] == ADJACENT_OVERLAP:
            extra = (f" (rides on cue #{it.get('index_a')}, overlap "
                     f"{it.get('overlap')}s"
                     + (", word-collision" if it.get("word_collision") else "")
                     + (f" — {it['hint']}" if it.get("hint") else "") + ")")
        print(f"    - [{it['type']}] cue #{it.get('index')}{span}{extra}")
    if prof_error:
        print(f"    - [profile-error] {prof_error}")
    if uncovered_error:
        print(f"    - [probe-error] {uncovered_error}")
    if uncovered:
        print(f"  uncovered: {len(uncovered)} audio-present-but-no-cue window(s)")
        classifications = _classify_uncovered_advisory(segments_path, uncovered)
        for (s, e) in uncovered:
            line = f"    - [{UNCOVERED_AUDIO}] {s:.2f}->{e:.2f}s"
            if classifications and (s, e) in classifications:
                c = classifications[(s, e)]
                line += f"  [{c['verdict']}] {c['suggestion']}"
            print(line)
    recovered_idx = [i for i, s in enumerate(segments) if is_recovered_segment(s)]
    if recovered_idx:
        tags = ", #".join(str(i) for i in recovered_idx)
        print(f"  recovered : {len(recovered_idx)} fill_gaps/resegmented "
              f"segment(s) -> #{tags} (high hallucination suspicion — see "
              f"reread task hints)")
    print(f"  content  : {'ok' if content_flags == 0 else 'flagged'}"
          f" ({content_flags} flag(s))")
    for it in mixed:
        print(f"    - [untranslated-latin] cue #{it['index']} {it['words']}")
    for it in linebreaks:
        print(f"    - [embedded-linebreak] cue #{it['index']} "
              f"field(s)={'/'.join(it['fields'])}")
    print(f"  presentation: {len(presentation_issues)} issue(s)")
    for it in presentation_issues:
        detail = it.get("detail") or f"start={it.get('start'):.2f}s"
        print(f"    - [{it['type']}] {detail}")

    # ---- Lane 2b: semantic reread (agent-side; CLI never calls an LLM) ------
    # 静默点 9: consume the reread RESULT when present — non-ok verdicts fail
    # the gate. When missing, (re)hang the task and mark state pending_agent;
    # generated SRTs are NOT withdrawn, but the pipeline must not read as done.
    pending_agent = False
    if zh_path:
        outdir = os.path.dirname(os.path.abspath(segments_path)) or "."
        base = _derive_base(segments_path)
        result_path = os.path.join(
            outdir, base + ".semantic_reread_result.json")
        reread_flags: list[tuple[int, str]] | None = None
        if os.path.exists(result_path):
            reread_flags = _parse_reread_result(load_json(result_path))
            if reread_flags:
                any_flag = True
                print(f"  semantic  : reread RESULT flags {len(reread_flags)} "
                      f"segment(s) -> RED")
                for idx, why in reread_flags:
                    print(f"    - [reread] cue #{idx if idx >= 0 else '?'} {why}")
            else:
                print("  semantic  : reread result all ok")
        if semantic:
            from .verify import build_semantic_reread_task
            zh2 = {int(k): v for k, v in load_json(zh_path).items()}
            task = build_semantic_reread_task(segments, zh2)
            out = semantic_out or os.path.join(
                os.path.dirname(segments_path),
                base + ".semantic_reread_task.json",
            )
            save_json(out, task, indent=2)
            print(f"  semantic  : reread task written -> {out}")
            print(f"              ({len(task['pairs'])} pairs) — agent rereads "
                  f"each (en,zh) and flags omit/add/wrong")
        if reread_flags is None and not _reread_all_ok(result_path):
            # No result file at all: hang (or re-hang) the task + mark pending.
            pending_agent = True
            if not semantic:
                from .verify import build_semantic_reread_task
                zh2 = {int(k): v for k, v in load_json(zh_path).items()}
                task = build_semantic_reread_task(segments, zh2)
                out = semantic_out or os.path.join(
                    os.path.dirname(segments_path),
                    base + ".semantic_reread_task.json",
                )
                save_json(out, task, indent=2)
            print("  semantic  : reread result MISSING -> task hung, status "
                  "marked pending_agent (agent reads neighbors, flags "
                  "omit/add/wrong, writes <base>.semantic_reread_result.json)")
    if pending_agent:
        _verify_state_hook(segments_path, "pending_agent")
    elif zh_path and not reread_flags:
        _verify_state_hook(segments_path, "ok" if not any_flag else "flagged")
    elif zh_path:
        _verify_state_hook(segments_path, "flagged")

    if not any_flag:
        print("  => clean (no lane flagged)")
    if strict and any_flag:
        print("\n[verify] gate FAILED (exit 8): fix the flagged lane(s) above "
              "and re-run; use --no-strict for a report-only pass.",
              file=sys.stderr)
        return EXIT_GATE_FAIL
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
    t.add_argument("--no-review", action="store_true",
                   help="(ADR-034 §6.2) skip the post-transcribe dual-signal "
                        "review + G1/G2 re-processing (review runs by default)")
    t.add_argument("--no-g3", action="store_true",
                   help="(ADR-034 §6.3) skip G3 local vocal separation "
                        "(strong-BGM recovery); G3 runs by default when "
                        "review is enabled")
    t.add_argument("--align", choices=["auto", "none", "whisperx"], default=None,
                   help="(T4 / ADR-028 / Spec 22) forced-acoustic word alignment "
                        "backend. 'auto' (default) = WhisperX wav2vec2 word-level "
                        "timestamp refinement on hosts that can run it (NVIDIA CUDA + "
                        "package), else none. 'whisperx' = force alignment (macOS / "
                        "not-installed hard-stops unless --allow-degrade). 'none' = "
                        "no alignment, byte-identical to historical output. "
                        "Override precedence: CLI > VT_ALIGN > [transcribe].align")
    t.add_argument("--allow-degrade", action="store_true",
                   help="(control plane 裁决一 escape hatch) explicitly permit "
                        "degradation of an EXPLICIT request --align whisperx or "
                        "--separate-vocals when the tool is unavailable, instead "
                        "of hard-stopping with exit 8. The reason is recorded in "
                        "<base>.vt_state.json.")
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
    tr.add_argument("--style", default=None,
                    choices=["film", "literal", "bilingual_study"],
                    help="translation style track: film (default, 影视二创) / "
                         "literal (忠实直译) / bilingual_study (双语精读注记)")
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
                   help="skip the zh/en index-drift audit run before rendering "
                        "(equivalent to --allow-degrade; the translate coverage "
                        "gate stays active)")
    g.add_argument("--allow-degrade", action="store_true",
                   help="(control plane escape hatch) explicitly bypass the "
                        "generate pre-flight gate (zh coverage / segment-count / "
                        "index-drift / staleness). Reason recorded in state; "
                        "never silent.")
    g.add_argument("--style", default=None,
                   choices=["film", "literal", "bilingual_study"],
                   help="style suffix for output filenames (e.g. base.film.bilingual.srt); "
                        "omit for the default single-track name")
    g.add_argument("--display-merge", action="store_true",
                   help="(Spec 26) merge adjacent SHORT cues with a small display "
                        "gap into one display cue (zh/en concatenated). Pure "
                        "presentation layer — segments/words untouched. Default OFF.")
    g.add_argument("--display-merge-gap", type=float, default=0.8,
                   help="(Spec 26) max display gap (s) between merged cues (default 0.8)")
    g.add_argument("--display-merge-max-dur", type=float, default=6.0,
                   help="(Spec 26) joined cue window upper bound in seconds (default 6.0)")
    g.add_argument("--display-merge-max-chars", type=int, default=84,
                   help="(Spec 26) English char budget per merged cue = 2 lines x 42 "
                        "(default 84)")
    g.add_argument("--display-merge-max-zh", type=int, default=40,
                   help="(Spec 26) Chinese char budget per merged cue = 2 lines x 20 "
                        "(default 40)")
    g.add_argument("--display-merge-short-dur", type=float, default=2.0,
                   help="(Spec 26) a cue is mergeable only if its dur <= this s (default 2.0)")
    g.add_argument("--display-merge-short-words", type=int, default=8,
                   help="(Spec 26) a cue is mergeable only if its word count <= this (default 8)")
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
    r.add_argument("--no-review", action="store_true",
                   help="(ADR-034 §6.2) skip the post-transcribe dual-signal "
                        "review + G1/G2 re-processing (review runs by default)")
    r.add_argument("--no-g3", action="store_true",
                   help="(ADR-034 §6.3) skip G3 local vocal separation "
                        "(strong-BGM recovery); G3 runs by default when "
                        "review is enabled")
    r.add_argument("--align", choices=["auto", "none", "whisperx"], default=None,
                   help="(T4 / ADR-028 / Spec 22) forced-acoustic word alignment "
                        "backend. See 'transcribe --align'. 'auto' (default) runs "
                        "whisperx on CUDA hosts; macOS / not-installed degrades to none.")
    r.add_argument("--allow-degrade", action="store_true",
                   help="(control plane 裁决一 escape hatch) explicitly permit "
                        "degradation of --align whisperx / --separate-vocals when "
                        "unavailable, instead of exit 8 (reason recorded in state).")
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
    r.add_argument("--display-merge", action="store_true",
                   help="(Spec 26) merge adjacent SHORT cues with a small display "
                        "gap into one display cue (zh/en concatenated). Pure "
                        "presentation layer — segments/words untouched. Default OFF.")
    r.add_argument("--display-merge-gap", type=float, default=0.8,
                   help="(Spec 26) max display gap (s) between merged cues (default 0.8)")
    r.add_argument("--display-merge-max-dur", type=float, default=6.0,
                   help="(Spec 26) joined cue window upper bound in seconds (default 6.0)")
    r.add_argument("--display-merge-max-chars", type=int, default=84,
                   help="(Spec 26) English char budget per merged cue = 2 lines x 42 "
                        "(default 84)")
    r.add_argument("--display-merge-max-zh", type=int, default=40,
                   help="(Spec 26) Chinese char budget per merged cue = 2 lines x 20 "
                        "(default 40)")
    r.add_argument("--display-merge-short-dur", type=float, default=2.0,
                   help="(Spec 26) a cue is mergeable only if its dur <= this s (default 2.0)")
    r.add_argument("--display-merge-short-words", type=int, default=8,
                   help="(Spec 26) a cue is mergeable only if its word count <= this (default 8)")
    r.add_argument("--glossary", default=None, help="path to glossary file (txt/json)")
    r.add_argument("--source", default=None,
                   help="video provenance/背景 hint fed to the translator, e.g. "
                        "'电影《天国王朝》鲍德温四世与萨拉丁会面片段'")
    r.add_argument("--style", default=None,
                   choices=["film", "literal", "bilingual_study"],
                   help="translation style track: film (default, 影视二创) / "
                        "literal (忠实直译) / bilingual_study (双语精读注记)")
    r.add_argument("--flat", action="store_true",
                   help="legacy: write final outputs flat into --outdir (no per-video subfolder)")
    r.add_argument("--prune-old", action="store_true",
                   help="keep only the 2 newest versioned outputs in the subfolder")
    r.add_argument("--require-profile", action="store_true",
                   help="(ADR-032 硬闸) P0→P1 必须经由人工决策点: 要求已存在 "
                        "origin=explicit 的 routing (decisions.routing)。缺失则 run "
                        "直接失败 (exit 8), 防止 Agent 失守时裸跳过 P0 画像与决策点。")
    r.set_defaults(func=cmd_run)

    pl = sub.add_parser(
        "pipeline",
        help="(T8 / ADR-033) idempotent single-entry advancer: resolve the "
             "current position, execute the next step, stop at the next "
             "collaboration stop point. Re-running is always safe.")
    pl.add_argument("input", help="path to the video/audio file")
    pl.add_argument("--outdir", default=None,
                    help="artifact dir (default: video's own directory)")
    pl.add_argument("--base", default=None,
                    help="output basename (default: video filename stem)")
    pl.add_argument("--prompt", choices=list(VALID_PROMPTS), default=None,
                    help="decision-point mode (default from config, always): "
                         "always = stop for the style pick; never = auto-route "
                         "by profile; require-profile = hard gate requiring an "
                         "explicit routing (exit 8 otherwise)")
    pl.add_argument("--style", default=None,
                    choices=["film", "literal", "bilingual_study"],
                    help="explicit style pick (persists origin=explicit; "
                         "satisfies the decision point)")
    pl.add_argument("--vad", action="store_true",
                    help="explicit VAD override (default bare run, ADR-034)")
    pl.add_argument("--adaptive-vad", action="store_true",
                    help="explicit per-chunk VAD routing override (ADR-015)")
    pl.add_argument("--separate-vocals", action="store_true",
                    help="explicit Demucs vocal separation override (T2)")
    pl.add_argument("--vad-threshold", type=float, default=None,
                    help="Silero VAD speech threshold override")
    pl.add_argument("--demucs-model", default=None,
                    help="Demucs model name override (default htdemucs)")
    pl.add_argument("--engine", default=None, choices=["agent", "google"],
                    help="agent (default) stops at the translate stop point; "
                         "google runs the translation headless")
    pl.add_argument("--allow-degrade", action="store_true",
                    help="forwarded to run: permit degradation of explicit "
                         "--align whisperx / --separate-vocals when unavailable")
    pl.set_defaults(func=cmd_pipeline)

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
    rs.add_argument("--allow-degrade", action="store_true",
                    help="(control plane 裁决一 escape hatch) explicitly permit "
                         "degradation of --separate-vocals when demucs is "
                         "unavailable, instead of exit 8 (reason recorded in state).")
    rs.set_defaults(func=cmd_resegment)

    s = sub.add_parser("setup", help="Check/download the HF model (reuse if cached)")
    s.add_argument("--model", default="large-v3")
    s.add_argument("--device", default=None, choices=["auto", "cpu", "cuda"],
                   help="compute device (default auto: CUDA if available, else cpu)")
    s.add_argument("--compute-type", default=None,
                   choices=["auto", "float16", "int8", "int8_float16"],
                   help="quantization (default auto)")
    s.add_argument("--ffmpeg", action="store_true",
                   help="also download a portable FFmpeg into tools/ffmpeg (idempotent)")
    s.add_argument("--align", action="store_true",
                   help="also download the NLTK corpora the whisperx alignment "
                        "backend needs into models/nltk_data (idempotent)")
    s.add_argument("--no-model", action="store_true",
                   help="skip model download (e.g. when only fetching FFmpeg)")
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

    s = sub.add_parser("status", help="Show pipeline position and the next "
                                        "action (control plane state machine)")
    s.add_argument("--base", default=None,
                   help="pipeline base name (default: auto-discover the most "
                        "recent <base>.segments_en.json under --outdir)")
    s.add_argument("--outdir", default="videos",
                   help="artifact directory (default: videos)")
    s.add_argument("--video", default=None,
                   help="source video path, when known")
    s.add_argument("--json", action="store_true",
                   help="machine-parseable position JSON (agents)")
    s.set_defaults(func=cmd_status)

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
                   help="(deprecated no-op) strict is now the DEFAULT; kept "
                        "for backward compatibility")
    v.add_argument("--no-strict", dest="no_strict", action="store_true",
                   help="report-only mode: print findings, always exit 0")
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
    b.add_argument("--style", default=None,
                   choices=["film", "literal", "bilingual_study"],
                   help="translation style track for the backfill task persona")
    b.set_defaults(func=cmd_backfill)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    init_toolchain()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Spec 25 §1: boundary check. A path argument already mangled by the host
    # shell must fail HERE with an actionable message — not deep inside
    # io_utils.save_json() as an unrelated OSError (WinError 123).
    hygiene = _path_hygiene_error(args)
    if hygiene:
        print(f"[args] {hygiene}", file=sys.stderr)
        return EXIT_ARGS
    try:
        return args.func(args)
    except GateFail as e:
        # Control plane (§1.1 裁决一): an explicit request could not be
        # honored. Print the deterministic repair guidance and hard-stop —
        # never silently degrade an explicit intent.
        print(f"[gate-fail] {e.message}", file=sys.stderr)
        if e.guidance:
            print(f"  Fix: {e.guidance}", file=sys.stderr)
        return EXIT_GATE_FAIL


if __name__ == "__main__":
    sys.exit(main())

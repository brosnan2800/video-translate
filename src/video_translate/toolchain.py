"""Toolchain discovery, environment isolation, and runtime path injection.

Supports .env and platform-specific environment files (.env.win, .env.mac, .env.linux,
.env.local) to configure external tools (FFmpeg, CUDA libraries, model cache) cleanly
isolated from code and across platforms.

Toolchain setup priority:
  CLI args / runtime os.environ > .env.local > .env.<platform> > .env > system PATH / defaults
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .proxy import detect_proxy, setup_http_proxy

# Match ${VAR} or $VAR for simple variable expansion
_VAR_EXPAND_RE = re.compile(r"\$(?:\{([A-Za-z0-9_]+)\}|([A-Za-z0-9_]+))")


def parse_dotenv_content(content: str, current_env: dict[str, str] | None = None) -> dict[str, str]:
    """Parse key-value pairs from .env format string with variable expansion.

    Supports comments (#), export statements, single/double quotes, and ${VAR} expansion.
    """
    env_vars: dict[str, str] = dict(current_env or {})
    result: dict[str, str] = {}

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        if not key:
            continue

        # Handle quotes
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            quote_char = val[0]
            val = val[1:-1]
            if quote_char == '"':
                val = (
                    val.replace(r"\n", "\n")
                    .replace(r"\t", "\t")
                    .replace(r"\"", '"')
                    .replace(r"\\", "\\")
                )
        else:
            # Inline comment for unquoted value
            if " #" in val:
                val = val.split(" #", 1)[0].strip()

        # Variable expansion ${VAR} or $VAR
        def _replace_var(match: re.Match[str]) -> str:
            vname = match.group(1) or match.group(2)
            return result.get(vname, env_vars.get(vname, os.environ.get(vname, "")))

        val = _VAR_EXPAND_RE.sub(_replace_var, val)
        result[key] = val
        env_vars[key] = val

    return result


def parse_dotenv_file(filepath: str | Path, current_env: dict[str, str] | None = None) -> dict[str, str]:
    """Parse a .env file if it exists, returning extracted dict."""
    p = Path(filepath)
    if not p.is_file():
        return {}
    try:
        content = p.read_text(encoding="utf-8")
        return parse_dotenv_content(content, current_env=current_env)
    except Exception:
        return {}


def get_platform_env_filename() -> str:
    """Return platform-specific .env filename suffix."""
    if sys.platform == "win32":
        return ".env.win"
    if sys.platform == "darwin":
        return ".env.mac"
    return ".env.linux"


def resolve_env_files(root_dir: str | Path | None = None) -> list[Path]:
    """Return ordered list of existing .env files from base to specific."""
    root = Path(root_dir or os.getcwd())
    candidates = [
        root / ".env",
        root / get_platform_env_filename(),
        root / ".env.local",
    ]
    return [c for c in candidates if c.is_file()]


def load_env(
    root_dir: str | Path | None = None,
    *,
    override: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Load .env files hierarchy into os.environ.

    Returns (merged_env_dict, list_of_loaded_file_paths).
    """
    files = resolve_env_files(root_dir)
    merged: dict[str, str] = {}
    for f in files:
        parsed = parse_dotenv_file(f, current_env=merged)
        merged.update(parsed)

    for k, v in merged.items():
        if override or k not in os.environ:
            os.environ[k] = v

    return merged, [str(f) for f in files]


@dataclass
class ToolchainStatus:
    """Diagnostic snapshot of toolchain and runtime isolation."""

    loaded_files: list[str] = field(default_factory=list)
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    # Every external tool binary is resolved ONCE here (no ad-hoc shutil.which
    # at call sites). Adding a tool: register it in _TOOL_REGISTRY below.
    demucs_path: str | None = None
    nvidia_smi_path: str | None = None
    # Dependency / model directories, resolved ONCE from .env / VT_* (no ad-hoc
    # os.environ.get at call sites). Adding a dependency: register in _DEP_REGISTRY.
    hf_cache_dir: str | None = None
    whisper_model_dir: str | None = None
    htdemucs_cache_dir: str | None = None
    nltk_data_dir: str | None = None
    cuda_dir: str | None = None
    cuda_source: str | None = None  # E4: "venv-torch" | "env" | "system" | None
    cuda_available: bool = False
    cuda_info: str = ""
    device: str = "cpu"
    compute_type: str = "int8"
    dll_handles: list[Any] = field(default_factory=list, repr=False)
    initialized: bool = False


_GLOBAL_TOOLCHAIN: ToolchainStatus | None = None


# ---------------------------------------------------------------------------
# Central tool / dependency registry — the SINGLE source of truth
# ---------------------------------------------------------------------------
# Every external tool binary and dependency directory MUST be resolved here,
# ONCE, at init_toolchain() — never ad-hoc (shutil.which / os.environ.get) at a
# call site. See AGENTS.md "工具链解析（持久化）" red line and TOOLCHAIN.md
# "Adding a new tool". To add a tool/dependency:
#   1. add its field to ToolchainStatus
#   2. register it here (binary -> "<name>_path" / dependency -> dir field)
#   3. call it via resolve_tool(name) / model_dir(kind)
_DEFAULT_HF_CACHE = os.path.expanduser(os.path.join("~", ".cache", "huggingface"))

# name (as invoked on the CLI) -> ToolchainStatus attribute holding its abs path
_TOOL_REGISTRY: dict[str, str] = {
    "ffmpeg": "ffmpeg_path",
    "ffprobe": "ffprobe_path",
    "demucs": "demucs_path",
    "nvidia-smi": "nvidia_smi_path",
}

# dependency kind -> ToolchainStatus attribute holding its directory
_DEP_REGISTRY: dict[str, str] = {
    "hf": "hf_cache_dir",
    "whisper": "whisper_model_dir",
    "htdemucs": "htdemucs_cache_dir",
    "nltk": "nltk_data_dir",
}


def resolve_tool(name: str) -> str:
    """Return the absolute path of an external tool binary from the persisted
    toolchain config (resolved once at startup).

    This is the ONLY sanctioned way to locate a tool binary. It never lets a
    caller do its own ``shutil.which`` — so a binary found at one pipeline stage
    is never "lost" at a later stage (no per-call PATH/CWD search). Falls back
    to ``shutil.which`` then the bare name only if the config has no entry.
    """
    status = get_toolchain_status()
    attr = _TOOL_REGISTRY.get(name)
    if attr:
        val = getattr(status, attr)
        if val:
            return val
    return shutil.which(name) or name


def tool_available(name: str) -> bool:
    """True when ``name`` resolves to a real binary (not the bare fallback name)."""
    return resolve_tool(name) != name


def model_dir(kind: str) -> str | None:
    """Return the cached directory for a dependency/model (resolved once).

    ``kind`` ∈ {"hf", "whisper", "htdemucs", "nltk"}. Returns ``None`` for an
    unknown kind — callers must not invent dirs ad-hoc.
    """
    status = get_toolchain_status()
    attr = _DEP_REGISTRY.get(kind)
    if attr:
        return getattr(status, attr)
    return None


def prepend_to_path(dir_path: str | Path) -> None:
    """Prepend a directory to os.environ['PATH'] if directory exists."""
    p = str(Path(dir_path).resolve())
    if not os.path.isdir(p):
        return
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep) if current_path else []
    # Avoid duplicate prepends
    if p not in parts:
        os.environ["PATH"] = p + (os.pathsep + current_path if current_path else "")


def _check_cuda_support() -> tuple[bool, str]:
    """Probe whether NVIDIA CUDA is present and usable."""
    has_smi = shutil.which("nvidia-smi") is not None
    try:
        import ctranslate2
        cuda_count = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            return True, f"CTranslate2 detected {cuda_count} CUDA device(s)"
    except Exception as e:
        if has_smi:
            return False, f"nvidia-smi present but CTranslate2 CUDA init failed: {e}"
    if has_smi:
        return True, "nvidia-smi present"
    try:
        import torch
        if torch.cuda.is_available():
            return True, f"PyTorch detected {torch.cuda.device_count()} CUDA device(s)"
    except Exception:
        pass
    return False, "No NVIDIA GPU / CUDA runtime detected (CPU fallback)"


def _dir_has_cuda_dlls(path: str | Path) -> bool:
    """True only when `path` actually holds CUDA runtime DLLs.

    E4 guard: probing used to accept any directory that merely existed, so an
    unrelated ``CUDA_PATH`` (e.g. ``F:\\Program Files``) was injected into PATH
    and reported to `doctor` as the CUDA source. Require the DLLs themselves.
    """
    try:
        names = {p.name.lower() for p in Path(path).iterdir() if p.is_file()}
    except OSError:
        return False
    return any(
        name.startswith(("cublas64_", "cudart64_", "cudnn64_", "cufft64_", "nvrtc64_"))
        for name in names
    )


def _resolve_cuda_dir(merged: dict[str, str]) -> tuple[str | None, str | None]:
    """Resolve the CUDA / PyTorch lib directory (Milestone 3 / E4).

    Order (deterministic, no scattered caches):
      ① VT_CUDA_DIR / VT_TORCH_LIB_DIR — explicit override: what the user
         spelled out wins, we never second-guess a deliberate setting.
      ② venv torch/lib — auto-detected; ships the CUDA runtime that exactly
         matches this venv's torch build, so a fresh machine needs no config.
      ③ CUDA_PATH — system CUDA toolkit, last resort.

    Every candidate must actually contain CUDA DLLs — a directory that merely
    exists is not a CUDA directory.

    Returns (dir, source_label). source_label ∈ {"env", "venv-torch", "system"}
    so doctor can show where the directory came from; ``None`` means CPU fallback.
    """
    def _validated(raw: str | None, label: str) -> tuple[str, str] | None:
        if not raw:
            return None
        path = Path(raw)
        if not path.is_dir():
            return None
        # A CUDA_PATH points at the toolkit root; its DLLs live in bin/.
        cand = path / "bin" if (path / "bin").is_dir() else path
        if _dir_has_cuda_dlls(cand):
            return str(cand), label
        if _dir_has_cuda_dlls(path):
            return str(path), label
        return None

    # ① explicit override
    for key in ("VT_CUDA_DIR", "VT_TORCH_LIB_DIR"):
        hit = _validated(merged.get(key) or os.environ.get(key), "env")
        if hit:
            return hit

    # ② venv torch/lib — derived from the torch package installed in this venv.
    try:
        import importlib.util

        spec = importlib.util.find_spec("torch")
        if spec and spec.submodule_search_locations:
            cand = Path(spec.submodule_search_locations[0]) / "lib"
            hit = _validated(str(cand), "venv-torch")
            if hit:
                return hit
    except Exception:  # noqa: BLE001
        pass

    # ③ system CUDA toolkit
    hit = _validated(merged.get("CUDA_PATH") or os.environ.get("CUDA_PATH"), "system")
    if hit:
        return hit

    return None, None


def init_toolchain(
    root_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> ToolchainStatus:
    """Initialize and inject toolchain paths (FFmpeg, CUDA libs) into runtime environment.

    Idempotent: caches result after first run unless force=True.
    """
    global _GLOBAL_TOOLCHAIN
    if _GLOBAL_TOOLCHAIN is not None and _GLOBAL_TOOLCHAIN.initialized and not force:
        return _GLOBAL_TOOLCHAIN

    status = ToolchainStatus()

    # 1. Load .env hierarchy.
    # Anchor to the repo root (not cwd) so the toolchain config is a *fixed*
    # file regardless of where the command is launched from — a plain `uv run`
    # from a subdir still binds tools/ffmpeg/bin via .env(.local).
    merged, loaded_files = load_env(root_dir or project_root(), override=force)
    status.loaded_files = loaded_files

    # 2. Inject FFmpeg directory if specified
    ffmpeg_dir = (
        merged.get("VT_FFMPEG_DIR")
        or os.environ.get("VT_FFMPEG_DIR")
        or merged.get("FFMPEG_DIR")
        or os.environ.get("FFMPEG_DIR")
    )
    if ffmpeg_dir:
        prepend_to_path(ffmpeg_dir)

    # 3. Resolve CUDA / PyTorch library directory.
    # Milestone 3 / E4 resolution order (highest determinism, no scattered caches):
    #   ① venv torch/lib  — auto-detect the torch install shipped with this venv
    #   ② VT_CUDA_DIR / VT_TORCH_LIB_DIR — explicit override (still respected)
    #   ③ CUDA_PATH — system CUDA toolkit
    #   ④ none — CPU fallback
    cuda_dir, cuda_source = _resolve_cuda_dir(merged)
    if cuda_dir and os.path.isdir(cuda_dir):
        status.cuda_dir = cuda_dir
        status.cuda_source = cuda_source
        prepend_to_path(cuda_dir)
        # On Windows Python 3.8+, os.add_dll_directory is required for ctypes/C extensions
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            try:
                handle = os.add_dll_directory(cuda_dir)
                status.dll_handles.append(handle)
            except Exception:
                pass

    # 4. Probe binaries (resolved ONCE; every caller reuses these via resolve_tool)
    status.ffmpeg_path = shutil.which("ffmpeg")
    status.ffprobe_path = shutil.which("ffprobe")
    status.demucs_path = shutil.which("demucs")
    status.nvidia_smi_path = shutil.which("nvidia-smi")

    # 5. Resolve dependency / model directories (ONCE; callers use model_dir).
    # Precedence mirrors the legacy env reads so behavior is unchanged — we only
    # centralize WHERE the value comes from (no scattered os.environ.get).
    status.hf_cache_dir = (
        os.environ.get("VT_HF_CACHE_DIR")
        or os.environ.get("HF_HOME")
        or _DEFAULT_HF_CACHE
    )
    _models = project_root() / "models"
    status.whisper_model_dir = str(_models)
    status.htdemucs_cache_dir = str(_models / "torch")
    status.nltk_data_dir = str(nltk_data_dir())

    # 5. Probe CUDA
    status.cuda_available, status.cuda_info = _check_cuda_support()

    # 6. Resolve default device / compute type
    cfg_dev = os.environ.get("VT_DEVICE", "auto").lower()
    cfg_ct = os.environ.get("VT_COMPUTE_TYPE", "auto").lower()
    if cfg_dev == "auto":
        status.device = "cuda" if status.cuda_available else "cpu"
    else:
        status.device = cfg_dev

    if cfg_ct == "auto":
        status.compute_type = "int8_float16" if status.device == "cuda" else "int8"
    else:
        status.compute_type = cfg_ct

    status.initialized = True
    _GLOBAL_TOOLCHAIN = status
    return status


def get_toolchain_status() -> ToolchainStatus:
    """Return active toolchain status (initializes if not already done)."""
    global _GLOBAL_TOOLCHAIN
    if _GLOBAL_TOOLCHAIN is None:
        return init_toolchain()
    return _GLOBAL_TOOLCHAIN


# ---------------------------------------------------------------------------
# Portable FFmpeg auto-download (Milestone 3, E2)
# ---------------------------------------------------------------------------
# Per-platform portable FFmpeg build. We never reuse a package across platforms
# (Windows zip / Linux static tar.xz / macOS zip differ in layout and binaries).
_FFMPEG_SOURCES: dict[str, dict[str, str]] = {
    "win32": {
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "kind": "zip",
        "subdir": "ffmpeg-*-essentials_build/bin",
    },
    "linux": {
        # John Van Sickle static build (self-contained, no system deps).
        "url": "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-6.1.1-amd64-static.tar.xz",
        "kind": "tar.xz",
        "subdir": "ffmpeg-6.1.1-amd64-static",
    },
    "darwin": {
        "url": "https://evermeet.cx/ffmpeg/getrelease/zip",
        "kind": "zip",
        "subdir": "",  # evermeet zip contains ffmpeg/ffprobe at top level
    },
}


def _ffmpeg_dest_root(dest: str | Path) -> Path:
    return Path(dest)


def _find_exe_in(dir_path: Path, name: str) -> Path | None:
    """Find ``name`` (optionally with .exe) within ``dir_path`` (recursive, shallow)."""
    candidates = [name, f"{name}.exe"]
    for cand in candidates:
        direct = dir_path / cand
        if direct.is_file():
            return direct
    # shallow recursive search (one level) to locate inside extracted subdir
    for sub in sorted(dir_path.rglob(name)) + sorted(dir_path.rglob(f"{name}.exe")):
        if sub.is_file():
            return sub
    return None


def ensure_ffmpeg(dest: str | Path = "tools/ffmpeg", *, proxy: str | None = None) -> str | None:
    """Download and extract a portable FFmpeg for the current platform.

    Idempotent: if ``{dest}/bin/ffmpeg[.exe]`` already exists, returns its parent
    dir without re-downloading. On success writes ``VT_FFMPEG_DIR`` into
    ``.env.local`` (gitignored) so ``init_toolchain`` picks it up on next run.

    Returns:
        The bin directory containing ffmpeg/ffprobe, or ``None`` on failure.

    Cross-platform note: only the current platform's source is ever used; the
    other entries exist purely for documentation/clarity and are never fetched.
    """
    bin_dir = _ffmpeg_dest_root(dest) / "bin"
    existing = _find_exe_in(bin_dir, "ffmpeg")
    if existing is not None:
        print(f"[ensure_ffmpeg] already present at {existing}; skipping download.")
        return str(bin_dir)

    plat = sys.platform
    if plat not in _FFMPEG_SOURCES:
        print(f"[ensure_ffmpeg] unsupported platform {plat!r}; please install ffmpeg manually.",
              file=sys.stderr)
        return None

    src = _FFMPEG_SOURCES[plat]
    print(f"[ensure_ffmpeg] downloading {src['url']} (platform={plat})...")
    _ffmpeg_dest_root(dest).mkdir(parents=True, exist_ok=True)
    try:
        setup_http_proxy(proxy)
        tmp_path, _hdr = urllib.request.urlretrieve(src["url"])  # nosec B310 (fixed allowlist)
    except Exception as e:  # noqa: BLE001
        print(f"[ensure_ffmpeg] download failed: {e}", file=sys.stderr)
        return None

    try:
        extract_root = _ffmpeg_dest_root(dest)
        if src["kind"] == "zip":
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(extract_root)
        else:  # tar.xz
            with tarfile.open(tmp_path, "r:xz") as tf:
                tf.extractall(extract_root)
    except Exception as e:  # noqa: BLE001
        print(f"[ensure_ffmpeg] extract failed: {e}", file=sys.stderr)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Locate the bin dir inside the extracted tree.
    bin_dir.mkdir(parents=True, exist_ok=True)
    sub = src.get("subdir", "")
    if sub:
        # subdir may contain a glob (e.g. "ffmpeg-*-essentials_build/bin").
        matches = sorted(extract_root.glob(sub))
        search_base = matches[0] if matches else extract_root
    else:
        search_base = extract_root
    ffmpeg_exe = _find_exe_in(search_base, "ffmpeg")
    ffprobe_exe = _find_exe_in(search_base, "ffprobe")
    if ffmpeg_exe is None or ffprobe_exe is None:
        print("[ensure_ffmpeg] ffmpeg/ffprobe not found in extracted archive.",
              file=sys.stderr)
        return None
    # Move binaries up into {dest}/bin for a stable, known path.
    shutil.move(str(ffmpeg_exe), str(bin_dir / ffmpeg_exe.name))
    shutil.move(str(ffprobe_exe), str(bin_dir / ffprobe_exe.name))

    # Persist VT_FFMPEG_DIR into .env.local (gitignored) so init_toolchain binds it.
    try:
        env_local = Path(".env.local")
        lines = env_local.read_text(encoding="utf-8").splitlines() if env_local.exists() else []
        kept = [ln for ln in lines if not ln.startswith("VT_FFMPEG_DIR=")]
        kept.append(f"VT_FFMPEG_DIR={bin_dir.resolve()}")
        env_local.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass  # non-fatal: caller can still export the path itself

    print(f"[ensure_ffmpeg] ready at {bin_dir}")
    return str(bin_dir)


# --- alignment corpora (R4/R5: project-local, auto-provisioned, gitignored) ---
# whisperx sentence-splits with nltk before aligning, so `punkt` / `punkt_tab`
# must resolve. Without them `whisperx.align` raises a LookupError and the align
# pass silently degrades to DTW timestamps — it looks like alignment ran, but
# every timestamp stays exactly as faster-whisper produced it. Same treatment as
# the Whisper weights (E3) and ffmpeg (E2): provisioned by `setup` into
# <repo>/models/, never committed, never fetched ad hoc by hand or agent.
_NLTK_RESOURCES = {
    "punkt": "tokenizers/punkt",
    "punkt_tab": "tokenizers/punkt_tab",
    "averaged_perceptron_tagger": "taggers/averaged_perceptron_tagger",
}


def nltk_data_dir() -> Path:
    """Project-local NLTK data dir (<repo>/models/nltk_data).

    Derived from this file's location (src/video_translate/toolchain.py) so it
    resolves regardless of the current working directory.
    """
    return Path(__file__).resolve().parent.parent.parent / "models" / "nltk_data"


def _nltk_has(nltk_mod, resource: str) -> bool:
    try:
        nltk_mod.data.find(resource)
        return True
    except LookupError:
        return False


def register_nltk_path() -> str | None:
    """Put the project-local NLTK dir on nltk's search path (idempotent).

    Returns the path, or None when nltk is not installed. Every check and every
    use of the corpora must go through this first — otherwise `doctor` reports
    "corpora missing" while the align pass happily finds them (the path is only
    registered as a side effect of running alignment).
    """
    try:
        import nltk
    except Exception:  # noqa: BLE001 - nltk only ships with the gpu extra
        return None
    target = str(nltk_data_dir())
    if target not in nltk.data.path:
        nltk.data.path.insert(0, target)
    return target


def nltk_data_ready() -> bool | None:
    """True when every needed corpus resolves; None when nltk is not installed.

    `None` (not False) distinguishes "alignment extra absent" from "corpora
    missing" so doctor can print the right hint.
    """
    if register_nltk_path() is None:
        return None
    import nltk
    return all(_nltk_has(nltk, res) for res in _NLTK_RESOURCES.values())


def ensure_nltk_data(dest: str | Path | None = None) -> str | None:
    """Download the NLTK corpora the alignment backend needs into <repo>/models/.

    Idempotent: returns as soon as every corpus resolves. Never raises — a
    failure here must not break `setup`; the align pass degrades gracefully.
    """
    try:
        import nltk
    except Exception:  # noqa: BLE001
        return None

    target = Path(dest) if dest else nltk_data_dir()
    if str(target) not in nltk.data.path:
        nltk.data.path.insert(0, str(target))

    missing = [pkg for pkg, res in _NLTK_RESOURCES.items()
               if not _nltk_has(nltk, res)]
    if not missing:
        print(f"[ensure_nltk_data] already present at {target}; skipping download.")
        return str(target)

    # nltk refuses to fetch through a proxy unless explicitly opted in (SSRF guard).
    os.environ.setdefault("NLTK_ALLOW_PROXIED_URLOPEN", "1")
    target.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for pkg in missing:
        print(f"[ensure_nltk_data] downloading {pkg} -> {target} ...")
        try:
            if nltk.download(pkg, download_dir=str(target)):
                fetched += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ensure_nltk_data] WARNING: {pkg} failed: {exc}",
                  file=sys.stderr)
    return str(target) if fetched == len(missing) else None


# ---------------------------------------------------------------------------
# Command-entry determinism (ADR-029 / Spec 23)
# ---------------------------------------------------------------------------
# The project's runtime environment always lives at <repo>/.venv, managed by uv.
# Every command must be launched through `uv run` (or an activated .venv) so the
# interpreter is pinned to the project — never a stray system python that may
# shadow the venv on PATH. `doctor` surfaces the entry source so a bare-python
# launch is caught before it silently degrades (e.g. missing whisperx).


def project_root() -> Path:
    """Repo root, derived from this module's location (src/video_translate/).

    Independent of the current working directory, matching ``nltk_data_dir()``.
    """
    return Path(__file__).resolve().parent.parent.parent


def project_venv_dir(root_dir: str | Path | None = None) -> Path:
    """The project virtualenv directory (<repo>/.venv), regardless of CWD."""
    root = Path(root_dir) if root_dir else project_root()
    return root / ".venv"


def resolve_command_entry(root_dir: str | Path | None = None) -> tuple[str, str]:
    """Determine how the CLI was launched (ADR-029 / Spec 23).

    Returns ``(entry, interpreter)`` where entry ∈ {"uv-run", "venv", "bare"}:
      - ``uv-run``  ``VIRTUAL_ENV`` points at this repo's ``.venv`` — the
                     canonical launch (`uv run` and `.venv activate` both set it).
      - ``venv``    ``sys.executable`` lives inside this repo's ``.venv`` but no
                     ``VIRTUAL_ENV`` marker was seen.
      - ``bare``    the interpreter is NOT the project venv (a system python or
                     another project's env shadowing PATH). This is the drift
                     the entry check exists to catch.

    ``interpreter`` is the resolved ``sys.executable`` for the doctor line.
    """
    venv = project_venv_dir(root_dir).resolve()
    interp = Path(sys.executable).resolve()
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env and os.path.normcase(str(venv)) == os.path.normcase(
        str(Path(venv_env).resolve())
    ):
        return "uv-run", sys.executable
    interp_norm = os.path.normcase(str(interp))
    venv_norm = os.path.normcase(str(venv))
    if interp_norm == venv_norm or interp_norm.startswith(venv_norm + os.sep):
        return "venv", sys.executable
    return "bare", sys.executable

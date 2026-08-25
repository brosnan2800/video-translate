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
    cuda_dir: str | None = None
    cuda_source: str | None = None  # E4: "venv-torch" | "env" | "system" | None
    cuda_available: bool = False
    cuda_info: str = ""
    device: str = "cpu"
    compute_type: str = "int8"
    dll_handles: list[Any] = field(default_factory=list, repr=False)
    initialized: bool = False


_GLOBAL_TOOLCHAIN: ToolchainStatus | None = None


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


def _resolve_cuda_dir(merged: dict[str, str]) -> tuple[str | None, str | None]:
    """Resolve the CUDA / PyTorch lib directory (Milestone 3 / E4).

    Order (deterministic, no scattered caches):
      ① venv torch/lib  — auto-detect torch shipped inside this venv
      ② VT_CUDA_DIR / VT_TORCH_LIB_DIR — explicit override (highest priority)
      ③ CUDA_PATH — system CUDA toolkit

    Returns (dir, source_label). source_label ∈ {"venv-torch", "env", "system"}
    so doctor can show where the directory came from; ``None`` means CPU fallback.
    """
    # ② explicit override wins when actually present on disk.
    for key, label in (("VT_CUDA_DIR", "env"), ("VT_TORCH_LIB_DIR", "env"),
                       ("CUDA_PATH", "system")):
        val = merged.get(key) or os.environ.get(key)
        if val and os.path.isdir(val):
            return val, label

    # ① venv torch/lib — derived from the torch package installed in this venv.
    try:
        import importlib.util

        spec = importlib.util.find_spec("torch")
        if spec and spec.submodule_search_locations:
            torch_root = Path(spec.submodule_search_locations[0])
            cand = torch_root / "lib"
            if cand.is_dir():
                return str(cand), "venv-torch"
    except Exception:  # noqa: BLE001
        pass

    # ③ system CUDA_PATH already covered in the loop above (returns "system").
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

    # 1. Load .env hierarchy
    merged, loaded_files = load_env(root_dir, override=force)
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

    # 4. Probe binaries
    status.ffmpeg_path = shutil.which("ffmpeg")
    status.ffprobe_path = shutil.which("ffprobe")

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

"""Unit tests for toolchain discovery, .env parsing, and runtime path injection."""
import os
import sys
from pathlib import Path

import pytest

from video_translate.toolchain import (
    ToolchainStatus,
    get_platform_env_filename,
    init_toolchain,
    load_env,
    parse_dotenv_content,
    parse_dotenv_file,
    prepend_to_path,
    resolve_env_files,
)


def test_parse_dotenv_content_basic():
    text = """
    # This is a comment
    VT_MODEL=large-v3
    export VT_ENGINE=agent
    VT_EMPTY=
    VT_QUOTED_DOUBLE="hello \\"world\\""
    VT_QUOTED_SINGLE='single quoted'
    VT_INLINE=some_value # inline comment
    """
    res = parse_dotenv_content(text)
    assert res["VT_MODEL"] == "large-v3"
    assert res["VT_ENGINE"] == "agent"
    assert res["VT_EMPTY"] == ""
    assert res["VT_QUOTED_DOUBLE"] == 'hello "world"'
    assert res["VT_QUOTED_SINGLE"] == "single quoted"
    assert res["VT_INLINE"] == "some_value"


def test_parse_dotenv_variable_expansion():
    text = """
    BASE_DIR=/opt/tools
    FFMPEG_DIR=${BASE_DIR}/ffmpeg
    EXTRA_DIR=$BASE_DIR/extra
    """
    res = parse_dotenv_content(text)
    assert res["BASE_DIR"] == "/opt/tools"
    assert res["FFMPEG_DIR"] == "/opt/tools/ffmpeg"
    assert res["EXTRA_DIR"] == "/opt/tools/extra"


def test_resolve_env_files_order(tmp_path):
    env_base = tmp_path / ".env"
    env_plat = tmp_path / get_platform_env_filename()
    env_local = tmp_path / ".env.local"

    env_base.write_text("A=1\n", encoding="utf-8")
    env_plat.write_text("B=2\n", encoding="utf-8")
    env_local.write_text("C=3\n", encoding="utf-8")

    files = resolve_env_files(tmp_path)
    assert len(files) == 3
    assert files[0] == env_base
    assert files[1] == env_plat
    assert files[2] == env_local


def test_load_env_hierarchy(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    monkeypatch.delenv("PLAT_KEY", raising=False)

    (tmp_path / ".env").write_text("TEST_KEY=from_base\nBASE_ONLY=yes\n", encoding="utf-8")
    (tmp_path / get_platform_env_filename()).write_text(
        "TEST_KEY=from_plat\nPLAT_KEY=yes\n", encoding="utf-8"
    )

    merged, loaded = load_env(tmp_path, override=True)
    assert "TEST_KEY" in merged
    assert merged["TEST_KEY"] == "from_plat"
    assert merged["BASE_ONLY"] == "yes"
    assert merged["PLAT_KEY"] == "yes"
    assert os.environ["TEST_KEY"] == "from_plat"


def test_prepend_to_path(tmp_path, monkeypatch):
    dummy_dir = tmp_path / "bin"
    dummy_dir.mkdir()
    p = str(dummy_dir.resolve())

    orig_path = os.environ.get("PATH", "")
    prepend_to_path(dummy_dir)
    assert os.environ["PATH"].startswith(p)

    # Calling again does not duplicate
    cur_path = os.environ["PATH"]
    prepend_to_path(dummy_dir)
    assert os.environ["PATH"] == cur_path


def test_init_toolchain_injects_ffmpeg_and_cuda(tmp_path, monkeypatch):
    ffmpeg_dir = tmp_path / "ffmpeg"
    ffmpeg_dir.mkdir()
    cuda_dir = tmp_path / "cuda"
    cuda_dir.mkdir()
    (cuda_dir / "cublas64_12.dll").write_bytes(b"")  # must contain CUDA DLLs

    env_file = tmp_path / ".env"
    env_file.write_text(
        f"VT_FFMPEG_DIR={ffmpeg_dir}\nVT_CUDA_DIR={cuda_dir}\nVT_DEVICE=cpu\n",
        encoding="utf-8",
    )

    status = init_toolchain(root_dir=tmp_path, force=True)
    assert status.initialized is True
    assert str(ffmpeg_dir.resolve()) in os.environ["PATH"]
    assert str(cuda_dir.resolve()) in os.environ["PATH"]
    assert status.device == "cpu"


def test_ensure_ffmpeg_zip_download_and_idempotent(tmp_path, monkeypatch):
    """ensure_ffmpeg downloads a zip, extracts ffmpeg/ffprobe into bin/, is idempotent."""
    import io
    import zipfile

    from video_translate.toolchain import ensure_ffmpeg

    monkeypatch.chdir(tmp_path)
    # Build a fake gyan-style zip: ffmpeg-XXXX/bin/{ffmpeg.exe, ffprobe.exe}
    fake_zip = tmp_path / "fake.zip"
    with zipfile.ZipFile(fake_zip, "w") as zf:
        zf.writestr("ffmpeg-essentials_build/bin/ffmpeg.exe", "PE")
        zf.writestr("ffmpeg-essentials_build/bin/ffprobe.exe", "PE")
    fake_zip_bytes = fake_zip.read_bytes()

    def fake_urlretrieve(url, filename=None):
        out = filename or str(tmp_path / "dl")
        Path(out).write_bytes(fake_zip_bytes)
        return out, None

    monkeypatch.setattr(
        "video_translate.toolchain.urllib.request.urlretrieve", fake_urlretrieve
    )

    bin_dir = ensure_ffmpeg(dest=tmp_path / "tools" / "ffmpeg", proxy=None)
    assert bin_dir is not None
    assert Path(bin_dir, "ffmpeg.exe").is_file()
    assert Path(bin_dir, "ffprobe.exe").is_file()
    # Second call must be idempotent (no re-download, no crash).
    bin_dir2 = ensure_ffmpeg(dest=tmp_path / "tools" / "ffmpeg", proxy=None)
    assert bin_dir2 == bin_dir
    # .env.local should record VT_FFMPEG_DIR
    env_local = tmp_path / ".env.local"
    assert env_local.exists()
    assert "VT_FFMPEG_DIR=" in env_local.read_text(encoding="utf-8")


def test_ensure_ffmpeg_unsupported_platform(monkeypatch, tmp_path):
    from video_translate.toolchain import ensure_ffmpeg

    monkeypatch.setattr("video_translate.toolchain.sys.platform", "freebsd")
    # Use a tmp dir, not a relative path: a relative "tools/ffmpeg" resolves
    # against the repo root, where a real portable ffmpeg may already exist —
    # ensure_ffmpeg would then short-circuit and return it instead of None.
    assert ensure_ffmpeg(dest=str(tmp_path / "tools" / "ffmpeg")) is None


def test_resolve_cuda_dir_venv_torch_first(monkeypatch, tmp_path):
    """E4: with no explicit override, venv torch/lib is auto-detected as source."""
    import importlib

    from video_translate import toolchain

    torch_lib = tmp_path / "lib"
    torch_lib.mkdir()
    (torch_lib / "cublas64_12.dll").write_bytes(b"")  # must ship CUDA DLLs

    class _FakeSpec:
        submodule_search_locations = [str(tmp_path)]

    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: _FakeSpec() if name == "torch" else None,
    )
    # Ensure no env override is present
    for k in ("VT_CUDA_DIR", "VT_TORCH_LIB_DIR", "CUDA_PATH"):
        monkeypatch.delenv(k, raising=False)

    cuda_dir, source = toolchain._resolve_cuda_dir({})
    assert source == "venv-torch"
    assert cuda_dir == str(torch_lib)


def test_resolve_cuda_dir_explicit_override_wins(monkeypatch, tmp_path):
    """E4: an explicit VT_CUDA_DIR on disk overrides venv torch detection."""
    import importlib

    from video_translate import toolchain

    explicit = tmp_path / "explicit_cuda"
    explicit.mkdir()
    (explicit / "cublas64_12.dll").write_bytes(b"")  # must ship CUDA DLLs

    class _FakeSpec:
        submodule_search_locations = [str(tmp_path / "venv_torch")]

    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: _FakeSpec() if name == "torch" else None,
    )
    monkeypatch.setenv("VT_CUDA_DIR", str(explicit))

    cuda_dir, source = toolchain._resolve_cuda_dir({})
    assert source == "env"
    assert cuda_dir == str(explicit)


def test_resolve_cuda_dir_none_without_torch_or_env(monkeypatch):
    """E4: no torch, no env -> CPU fallback (None, no source)."""
    import importlib

    from video_translate import toolchain

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    for k in ("VT_CUDA_DIR", "VT_TORCH_LIB_DIR", "CUDA_PATH"):
        monkeypatch.delenv(k, raising=False)

    cuda_dir, source = toolchain._resolve_cuda_dir({})
    assert cuda_dir is None
    assert source is None


def test_resolve_cuda_dir_system_path_does_not_outrank_venv_torch(monkeypatch, tmp_path):
    """E4 regression: CUDA_PATH (system) must NOT outrank venv torch/lib.

    The original implementation probed CUDA_PATH before the venv, so a stray
    system CUDA_PATH hijacked the CUDA source even when the venv shipped its own
    (build-matching) CUDA runtime — which is exactly the case E4 exists to fix.
    """
    import importlib

    from video_translate import toolchain

    venv_torch = tmp_path / "venv_torch"
    torch_lib = venv_torch / "lib"
    torch_lib.mkdir(parents=True)
    (torch_lib / "cublas64_12.dll").write_bytes(b"")

    system_cuda = tmp_path / "system_cuda"
    system_bin = system_cuda / "bin"
    system_bin.mkdir(parents=True)
    (system_bin / "cublas64_12.dll").write_bytes(b"")

    class _FakeSpec:
        submodule_search_locations = [str(venv_torch)]

    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: _FakeSpec() if name == "torch" else None,
    )
    monkeypatch.setenv("CUDA_PATH", str(system_cuda))
    monkeypatch.delenv("VT_CUDA_DIR", raising=False)
    monkeypatch.delenv("VT_TORCH_LIB_DIR", raising=False)

    cuda_dir, source = toolchain._resolve_cuda_dir({})
    assert source == "venv-torch"
    assert cuda_dir == str(torch_lib)


def test_resolve_cuda_dir_rejects_dir_without_cuda_dlls(monkeypatch, tmp_path):
    """E4: a directory that merely exists is not a CUDA directory.

    Real-world case that motivated this guard: CUDA_PATH=F:\\Program Files (a
    generic software root) used to be injected into PATH and reported as the
    CUDA source.
    """
    import importlib

    from video_translate import toolchain

    not_cuda = tmp_path / "not_cuda"
    not_cuda.mkdir()

    monkeypatch.setenv("VT_CUDA_DIR", str(not_cuda))
    monkeypatch.setenv("CUDA_PATH", str(not_cuda))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    cuda_dir, source = toolchain._resolve_cuda_dir({})
    assert cuda_dir is None
    assert source is None


# ---------------------------------------------------------------------------
# Tool registry (control plane §2.3)
# ---------------------------------------------------------------------------

def _fake_exe(bin_dir, name: str) -> Path:
    exe = name + (".exe" if os.name == "nt" else "")
    p = bin_dir / exe
    p.write_bytes(b"MZ")
    return p


def test_resolve_tool_returns_persisted_abs_path(tmp_path):
    """Control plane §2.3: resolve_tool uses the persisted toolchain path, not
    a fresh ad-hoc PATH search — a binary found at stage A is never lost at
    stage B."""
    from video_translate.toolchain import resolve_tool, tool_available

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = _fake_exe(bin_dir, "ffmpeg")
    _fake_exe(bin_dir, "ffprobe")
    (tmp_path / ".env").write_text(
        f"VT_FFMPEG_DIR={bin_dir}\n", encoding="utf-8"
    )

    status = init_toolchain(root_dir=tmp_path, force=True)
    assert status.ffmpeg_path  # resolved once at init
    # Windows: shutil.which normalizes the extension to PATHEXT's uppercase .EXE
    assert os.path.normcase(resolve_tool("ffmpeg")) == os.path.normcase(str(fake))
    assert tool_available("ffmpeg") is True


def test_tool_available_false_for_unknown_or_missing():
    from video_translate.toolchain import tool_available

    assert tool_available("this-tool-definitely-does-not-exist") is False

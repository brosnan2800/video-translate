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

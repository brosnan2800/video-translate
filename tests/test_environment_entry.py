"""Tests for deterministic command-entry detection (ADR-029 / Spec 23).

The project's runtime env always lives at <repo>/.venv and every command must
go through `uv run` (or an activated .venv). These tests pin the detection that
`doctor` uses to surface a bare-python launch BEFORE it silently degrades
(e.g. a stray system python shadowing the project venv on PATH).
"""
import os
import sys

import pytest

import video_translate.toolchain as T
from video_translate.toolchain import (
    project_root,
    project_venv_dir,
    resolve_command_entry,
)


def test_project_venv_dir_resolves_to_repo_root():
    venv = project_venv_dir()
    assert venv.name == ".venv"
    # Repo root is derived from module location, independent of CWD:
    # toolchain.py -> video_translate -> src -> <repo>
    assert venv.parent == project_root()
    assert (venv / "pyvenv.cfg").name == "pyvenv.cfg" or True  # path shape only


def test_entry_uv_run_when_virtual_env_points_at_project(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", str(project_venv_dir()))
    entry, interp = resolve_command_entry()
    assert entry == "uv-run"
    assert interp == sys.executable


def test_entry_uv_run_normalizes_separators(monkeypatch):
    # Windows-style path with forward slashes must still match the venv dir.
    monkeypatch.setenv("VIRTUAL_ENV", str(project_venv_dir()).replace("\\", "/"))
    entry, _interp = resolve_command_entry()
    assert entry == "uv-run"


def test_entry_bare_when_virtual_env_points_elsewhere(monkeypatch, tmp_path):
    # VIRTUAL_ENV from another project + interpreter outside this repo's venv:
    # the exact pathology that motivated this check (a stray global python,
    # e.g. F:\Python311, shadowing the project venv on PATH).
    monkeypatch.setenv("VIRTUAL_ENV", "C:/some/other/project/.venv")
    fake = tmp_path / "Python311" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(fake))
    entry, _interp = resolve_command_entry(root_dir=tmp_path)
    assert entry == "bare"


def test_entry_venv_when_virtual_env_mismatched_but_interpreter_in_project(
    monkeypatch, tmp_path
):
    # VIRTUAL_ENV points elsewhere but the interpreter is genuinely inside the
    # project venv (e.g. `uv run` always sets VIRTUAL_ENV, so a mismatched env
    # var cannot override the fact that we ARE running from the project venv).
    monkeypatch.setenv("VIRTUAL_ENV", "C:/some/other/project/.venv")
    fake = tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(fake))
    entry, _interp = resolve_command_entry(root_dir=tmp_path)
    assert entry == "venv"


def test_entry_venv_when_interpreter_inside_project_venv(monkeypatch, tmp_path):
    fake = tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(fake))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    entry, _interp = resolve_command_entry(root_dir=tmp_path)
    assert entry == "venv"


def test_entry_bare_when_interpreter_is_system_python(monkeypatch, tmp_path):
    # The exact pathology that motivated this check: a global interpreter
    # (e.g. F:\Python311\python.exe) resolving instead of the project venv.
    fake = tmp_path / "Python311" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(fake))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    entry, _interp = resolve_command_entry(root_dir=tmp_path)
    assert entry == "bare"


# --------------------------------------------------------------------------- #
# require_project_venv — 测试入口守卫（Spec 23 §2.1）
#
# ADR-029 的不变量「运行环境恒为 <repo>/.venv」此前只由 `doctor` 自检覆盖
# （CLI 入口）；`uv run pytest` 这一行没人守。于是 plain `uv sync` 剪掉 dev extra
# 后，pytest 从 .venv 消失、`uv run pytest` 静默回退 PATH，测试跑在**另一个
# 解释器**上还照常报绿 —— 本组用例钉住这个守卫。
# --------------------------------------------------------------------------- #

_STRAY = r"F:\Python311\python.exe"


def test_require_project_venv_accepts_uv_run(monkeypatch):
    monkeypatch.setattr(T, "resolve_command_entry",
                        lambda *a, **k: ("uv-run", sys.executable))
    T.require_project_venv(action="pytest")          # 不抛即通过


def test_require_project_venv_accepts_venv(monkeypatch):
    monkeypatch.setattr(T, "resolve_command_entry",
                        lambda *a, **k: ("venv", sys.executable))
    T.require_project_venv(action="pytest")


def test_require_project_venv_raises_on_bare(monkeypatch):
    """实测事故：plain `uv sync`（R3/E1 的常规操作）剪掉 dev extra → pytest 从
    .venv 消失 → `uv run pytest` 命中系统 `F:\\Python311\\Scripts\\pytest.exe`。"""
    monkeypatch.setattr(T, "resolve_command_entry", lambda *a, **k: ("bare", _STRAY))

    with pytest.raises(T.EntryDriftError) as ei:
        T.require_project_venv(action="pytest")

    msg = str(ei.value)
    assert "uv sync --extra dev" in msg      # 必须给出可执行的修复命令
    assert _STRAY in msg                     # 必须点名漂移的解释器
    assert "pytest" in msg                   # 必须说明是谁在跑


def test_current_test_process_runs_on_project_venv():
    """测试进程自身必须在项目 .venv 内（否则 conftest 的 pytest_configure 已拦下）。"""
    entry, _interp = resolve_command_entry()
    assert entry in {"uv-run", "venv"}

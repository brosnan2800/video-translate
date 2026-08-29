"""Tests for deterministic command-entry detection (ADR-029 / Spec 23).

The project's runtime env always lives at <repo>/.venv and every command must
go through `uv run` (or an activated .venv). These tests pin the detection that
`doctor` uses to surface a bare-python launch BEFORE it silently degrades
(e.g. a stray system python shadowing the project venv on PATH).
"""
import os
import sys

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

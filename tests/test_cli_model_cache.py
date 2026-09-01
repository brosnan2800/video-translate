"""E3: model cache completeness detection + self-heal path.

These tests never touch the network and never write large files to disk.
The lower-size bound is monkeypatched down so we can exercise the
"complete vs truncated" logic with tiny stub files (no 3 GB junk in Temp).
"""
import os

import pytest

from video_translate import cli


# Local-only threshold so tests can use KB-sized stub files instead of a real
# 3 GB model.bin — keeps the pytest Temp dir clean (Milestone 3 / E3 fix).
_TEST_MIN_BYTES = 100


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)


def _empty_hf_cache(monkeypatch, tmp_path) -> str:
    """Isolate the HF cache to an empty temp dir; returns that path.

    The toolchain resolves dependency directories ONCE at startup and caches
    them — that is the point of rule 3 (no CWD/env drift between stages). So
    once init has run, a bare ``setenv("HF_HOME")`` no longer reaches
    ``_hf_cache_dir()``. Set both: the env var for anything reading it directly,
    and the resolved value the code under test actually consumes.
    """
    hf = str(tmp_path / "hf")
    monkeypatch.setenv("HF_HOME", hf)
    monkeypatch.setattr(cli, "_hf_cache_dir", lambda: hf)
    return hf


def test_model_cached_true_for_complete_local(monkeypatch, tmp_path):
    """A complete local model.bin (>= min bound) is reported cached."""
    monkeypatch.setattr(cli, "_MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    model_dir = tmp_path / "models" / "large-v3"
    _write(str(model_dir / "model.bin"), _TEST_MIN_BYTES + 50)
    monkeypatch.setattr(cli, "_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    _empty_hf_cache(monkeypatch, tmp_path)
    assert cli._model_cached("large-v3") is True


def test_model_cached_false_for_truncated_local(monkeypatch, tmp_path):
    """A truncated local model.bin (< min bound) is NOT reported cached (E3)."""
    monkeypatch.setattr(cli, "_MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    model_dir = tmp_path / "models" / "large-v3"
    _write(str(model_dir / "model.bin"), _TEST_MIN_BYTES - 50)  # below bound
    monkeypatch.setattr(cli, "_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    _empty_hf_cache(monkeypatch, tmp_path)
    assert cli._model_cached("large-v3") is False


def test_find_incomplete_model_bins_locates_truncated(monkeypatch, tmp_path):
    """_find_incomplete_model_bins returns the truncated model.bin path."""
    monkeypatch.setattr(cli, "_MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    model_dir = tmp_path / "models" / "large-v3"
    _write(str(model_dir / "model.bin"), _TEST_MIN_BYTES - 50)
    monkeypatch.setattr(cli, "_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    _empty_hf_cache(monkeypatch, tmp_path)
    found = cli._find_incomplete_model_bins("large-v3")
    assert len(found) == 1
    assert found[0].endswith(os.path.join("large-v3", "model.bin"))


def test_find_incomplete_model_bins_empty_when_complete(monkeypatch, tmp_path):
    """A complete model yields no incomplete entries (nothing to self-heal)."""
    monkeypatch.setattr(cli, "_MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    model_dir = tmp_path / "models" / "large-v3"
    _write(str(model_dir / "model.bin"), _TEST_MIN_BYTES + 50)
    monkeypatch.setattr(cli, "_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    _empty_hf_cache(monkeypatch, tmp_path)
    assert cli._find_incomplete_model_bins("large-v3") == []


def test_model_cached_false_when_no_dir(monkeypatch, tmp_path):
    """With no local dir and empty HF cache, model is not cached."""
    monkeypatch.setattr(cli, "_MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    monkeypatch.setattr(cli, "_LOCAL_MODEL_DIR", str(tmp_path / "models"))
    _empty_hf_cache(monkeypatch, tmp_path)
    assert cli._model_cached("large-v3") is False

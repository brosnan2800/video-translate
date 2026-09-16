"""E3: model cache completeness detection + self-heal path (`model_cache` module).

These tests never touch the network and never write large files to disk.
The lower-size bound is monkeypatched down so we can exercise the
"complete vs truncated" logic with tiny stub files (no 3 GB junk in Temp).

（原名 test_cli_model_cache.py —— 这些函数已从 cli 下沉到 model_cache，
patch 目标随之改指后者；该模块是唯一来源，cli / transcribe / vocal_sep 都消费它。）
"""
import os

from video_translate import model_cache

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
    ``hf_cache_dir()``. Set both: the env var for anything reading it directly,
    and the resolved value the code under test actually consumes.
    """
    hf = str(tmp_path / "hf")
    monkeypatch.setenv("HF_HOME", hf)
    monkeypatch.setattr(model_cache, "hf_cache_dir", lambda: hf)
    return hf


def _local_only(monkeypatch, tmp_path) -> str:
    """Point the module at a temp ``models/`` dir with the tiny test bound."""
    monkeypatch.setattr(model_cache, "MODEL_MIN_BYTES", _TEST_MIN_BYTES)
    local = str(tmp_path / "models")
    monkeypatch.setattr(model_cache, "LOCAL_MODEL_DIR", local)
    return local


def test_model_cached_true_for_complete_local(monkeypatch, tmp_path):
    """A complete local model.bin (>= min bound) is reported cached."""
    local = _local_only(monkeypatch, tmp_path)
    _write(os.path.join(local, "large-v3", "model.bin"), _TEST_MIN_BYTES + 50)
    _empty_hf_cache(monkeypatch, tmp_path)
    assert model_cache.model_cached("large-v3") is True


def test_model_cached_false_for_truncated_local(monkeypatch, tmp_path):
    """A truncated local model.bin (< min bound) is NOT reported cached (E3)."""
    local = _local_only(monkeypatch, tmp_path)
    _write(os.path.join(local, "large-v3", "model.bin"), _TEST_MIN_BYTES - 50)
    _empty_hf_cache(monkeypatch, tmp_path)
    assert model_cache.model_cached("large-v3") is False


def test_find_incomplete_model_bins_locates_truncated(monkeypatch, tmp_path):
    """find_incomplete_model_bins returns the truncated model.bin path."""
    local = _local_only(monkeypatch, tmp_path)
    _write(os.path.join(local, "large-v3", "model.bin"), _TEST_MIN_BYTES - 50)
    _empty_hf_cache(monkeypatch, tmp_path)
    found = model_cache.find_incomplete_model_bins("large-v3")
    assert len(found) == 1
    assert found[0].endswith(os.path.join("large-v3", "model.bin"))


def test_find_incomplete_model_bins_empty_when_complete(monkeypatch, tmp_path):
    """A complete model yields no incomplete entries (nothing to self-heal)."""
    local = _local_only(monkeypatch, tmp_path)
    _write(os.path.join(local, "large-v3", "model.bin"), _TEST_MIN_BYTES + 50)
    _empty_hf_cache(monkeypatch, tmp_path)
    assert model_cache.find_incomplete_model_bins("large-v3") == []


def test_model_cached_false_when_no_dir(monkeypatch, tmp_path):
    """With no local dir and empty HF cache, model is not cached."""
    _local_only(monkeypatch, tmp_path)
    _empty_hf_cache(monkeypatch, tmp_path)
    assert model_cache.model_cached("large-v3") is False


def test_model_cached_finds_complete_hf_snapshot(monkeypatch, tmp_path):
    """HF hub 快照里完整的 model.bin 同样算「已缓存」（两条探测路径都要守）。"""
    _local_only(monkeypatch, tmp_path)  # 本地目录为空
    hf = _empty_hf_cache(monkeypatch, tmp_path)
    snap = os.path.join(hf, "hub", "models--Systran--faster-whisper-large-v3",
                        "snapshots", "abc123")
    _write(os.path.join(snap, "model.bin"), _TEST_MIN_BYTES + 50)
    assert model_cache.model_cached("large-v3") is True


def test_resolve_model_path_prefers_complete_local_dir(monkeypatch, tmp_path):
    """项目内已有完整模型目录时解析到该目录；否则原样透传（HF 仓库 id）。"""
    local = _local_only(monkeypatch, tmp_path)
    assert model_cache.resolve_model_path("large-v3") == "large-v3"
    _write(os.path.join(local, "large-v3", "model.bin"), 8)
    assert model_cache.resolve_model_path("large-v3") == os.path.join(local, "large-v3")
    # 已含路径分隔符 / 绝对路径的入参一律原样透传
    assert model_cache.resolve_model_path("Systran/faster-whisper-large-v3") == (
        "Systran/faster-whisper-large-v3")

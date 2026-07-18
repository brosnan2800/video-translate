"""Tests for config resolution priority (Spec 06)."""
import os

from video_translate.config import DEFAULT_HF_CACHE, resolve_config
from video_translate.proxy import DEFAULT_PROXY


def test_defaults(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.model == "large-v3"
    assert cfg.chunk == 240.0
    assert cfg.lang == "en"
    assert cfg.proxy == DEFAULT_PROXY
    assert cfg.src == "en" and cfg.tgt == "zh-CN"
    assert cfg.hf_cache_dir == DEFAULT_HF_CACHE


def test_toml_overrides_default(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text(
        "[transcribe]\nmodel = 'small'\nchunk = 120.0\n"
        "[translate]\ntgt = 'zh-TW'\n",
        encoding="utf-8",
    )
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.model == "small"
    assert cfg.chunk == 120.0
    assert cfg.tgt == "zh-TW"


def test_env_overrides_toml(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text("[transcribe]\nmodel = 'small'\n", encoding="utf-8")
    cfg = resolve_config(cwd=str(tmp_path), env={"VT_MODEL": "medium", "VT_CHUNK": "60"})
    assert cfg.model == "medium"
    assert cfg.chunk == 60.0  # env chunk cast to float
    assert cfg._sources["model"] == "env"


def test_cli_overrides_env_and_toml(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text("[transcribe]\nmodel = 'small'\n", encoding="utf-8")
    cfg = resolve_config(
        {"model": "large-v3"}, cwd=str(tmp_path), env={"VT_MODEL": "medium"}
    )
    assert cfg.model == "large-v3"
    assert cfg._sources["model"] == "cli"


def test_cli_none_is_ignored(tmp_path):
    cfg = resolve_config({"model": None}, cwd=str(tmp_path), env={})
    assert cfg.model == "large-v3"  # None override does not clobber default

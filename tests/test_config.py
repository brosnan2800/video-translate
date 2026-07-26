"""Tests for config resolution priority (Spec 06)."""
import os

from video_translate.config import DEFAULT_HF_CACHE, DEFAULT_PERSONA, resolve_config


def test_defaults(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.model == "large-v3"
    assert cfg.chunk == 240.0
    assert cfg.lang is None            # V2: auto-detect
    assert cfg.proxy is None           # V2: auto-detect / direct
    assert cfg.src == "en" and cfg.tgt == "zh-CN"
    assert cfg.hf_cache_dir == DEFAULT_HF_CACHE
    # V2 fields
    assert cfg.engine == "agent"
    assert cfg.persona == DEFAULT_PERSONA
    assert cfg.merge_enabled is True
    assert cfg.merge_max_dur == 8.0
    assert cfg.merge_max_gap == 0.5
    assert cfg.merge_max_chars == 42
    # V3 fields
    assert cfg.glossary is None


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


def test_toml_llm_and_merge_sections(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text(
        "[llm]\npersona = 'custom persona'\n"
        "[merge]\nmerge_max_dur = 10.0\nmerge_max_chars = 50\nmerge_enabled = false\n",
        encoding="utf-8",
    )
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.persona == "custom persona"
    assert cfg.merge_max_dur == 10.0
    assert cfg.merge_max_chars == 50
    assert cfg.merge_enabled is False


def test_env_overrides_toml(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text("[transcribe]\nmodel = 'small'\n", encoding="utf-8")
    cfg = resolve_config(cwd=str(tmp_path), env={"VT_MODEL": "medium", "VT_CHUNK": "60"})
    assert cfg.model == "medium"
    assert cfg.chunk == 60.0  # env chunk cast to float
    assert cfg._sources["model"] == "env"


def test_env_engine_and_merge(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={
        "VT_ENGINE": "google", "VT_MERGE_MAX_DUR": "12",
        "VT_MERGE_MAX_GAP": "0.8", "VT_MERGE_MAX_CHARS": "60",
    })
    assert cfg.engine == "google"
    assert cfg.merge_max_dur == 12.0
    assert cfg.merge_max_gap == 0.8
    assert cfg.merge_max_chars == 60


def test_env_https_proxy_fallback_when_no_vt_proxy(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={"HTTPS_PROXY": "http://10.0.0.1:8080"})
    assert cfg.proxy == "http://10.0.0.1:8080"
    assert cfg._sources["proxy"] == "env"


def test_vt_proxy_wins_over_https_proxy(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={
        "VT_PROXY": "http://vt:1", "HTTPS_PROXY": "http://std:2",
    })
    assert cfg.proxy == "http://vt:1"


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


def test_lang_auto_normalised_to_none(tmp_path):
    cfg = resolve_config({"lang": "auto"}, cwd=str(tmp_path), env={})
    assert cfg.lang is None


# --- V3: glossary + merge_max_chars CLI flag ---


def test_glossary_default_none(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.glossary is None


def test_toml_glossary(tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text("[translate]\nglossary = 'glossary.txt'\n", encoding="utf-8")
    cfg = resolve_config(cwd=str(tmp_path), env={})
    assert cfg.glossary == "glossary.txt"


def test_env_vt_glossary(tmp_path):
    cfg = resolve_config(cwd=str(tmp_path), env={"VT_GLOSSARY": "env_glossary.txt"})
    assert cfg.glossary == "env_glossary.txt"


def test_cli_glossary_override(tmp_path):
    cfg = resolve_config({"glossary": "cli_glossary.txt"}, cwd=str(tmp_path), env={})
    assert cfg.glossary == "cli_glossary.txt"
    assert cfg._sources["glossary"] == "cli"


def test_merge_max_chars_cli_flag(tmp_path):
    cfg = resolve_config({"merge_max_chars": 50}, cwd=str(tmp_path), env={})
    assert cfg.merge_max_chars == 50
    assert cfg._sources["merge_max_chars"] == "cli"

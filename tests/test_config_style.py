"""TDD red tests for style config resolution (Spec 21 / ADR-027).

Covers:
- default style == "film"
- VT_STYLE env
- toml [translate] style
- CLI --style override precedence
- invalid style -> ValueError
"""
import pytest

from video_translate.config import (
    VALID_STYLES,
    _coerce_env,
    resolve_config,
)


def test_default_style_is_film():
    cfg = resolve_config({})
    assert cfg.style == "film"


def test_vt_style_env():
    cfg = resolve_config({}, env={"VT_STYLE": "literal"})
    assert cfg.style == "literal"


def test_toml_translate_style(monkeypatch, tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text('[translate]\nstyle = "bilingual_study"\n')
    monkeypatch.chdir(tmp_path)
    cfg = resolve_config({})
    assert cfg.style == "bilingual_study"


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("VT_STYLE", "literal")
    cfg = resolve_config({"style": "bilingual_study"})
    assert cfg.style == "bilingual_study"


def test_toml_below_env(monkeypatch, tmp_path):
    toml = tmp_path / ".video-translate.toml"
    toml.write_text('[translate]\nstyle = "bilingual_study"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VT_STYLE", "literal")
    cfg = resolve_config({})
    assert cfg.style == "literal"


def test_invalid_style_raises():
    with pytest.raises(ValueError):
        _coerce_env("style", "poetic")


def test_valid_style_passes():
    assert _coerce_env("style", "literal") == "literal"
    assert _coerce_env("style", "film") == "film"

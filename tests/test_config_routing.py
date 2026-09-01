"""T5 / ADR-032: routing + decision-point config (env/toml/cli priority, types).

Pure config resolution — no ffmpeg, no network. We inject ``env`` directly into
``resolve_config`` so the host's real environment never leaks into the assertions.
"""
from video_translate.config import Config, resolve_config


def _cfg(env: dict, tmp_path, **overrides):
    return resolve_config(env=env, cwd=str(tmp_path), cli_overrides=overrides or None)


def test_routing_defaults(tmp_path):
    cfg = _cfg({}, tmp_path)
    # ADR-032 / T5 defaults.
    assert cfg.vad is False
    assert cfg.adaptive_vad is False
    assert cfg.vad_threshold == 0.35
    assert cfg.decision_timeout_seconds == 300
    assert cfg.merge_enabled is True


def test_vad_env_parsing(tmp_path):
    env = {
        "VT_VAD": "true",
        "VT_ADAPTIVE_VAD": "1",
        "VT_VAD_THRESHOLD": "0.1",
        "VT_DECISION_TIMEOUT_SECONDS": "600",
        "VT_MERGE_ENABLED": "false",
    }
    cfg = _cfg(env, tmp_path)
    assert cfg.vad is True
    assert cfg.adaptive_vad is True
    assert cfg.vad_threshold == 0.1
    assert cfg.decision_timeout_seconds == 600
    assert cfg.merge_enabled is False


def test_invalid_style_raises(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        _cfg({"VT_STYLE": "not_a_style"}, tmp_path)


def test_invalid_align_falls_back(tmp_path):
    cfg = _cfg({"VT_ALIGN": "bogus"}, tmp_path)
    assert cfg.align == "auto"


def test_cli_override_beats_env(tmp_path):
    env = {"VT_VAD": "true"}
    cfg = _cfg(env, tmp_path, vad=False)
    assert cfg.vad is False  # CLI override wins over env


def test_vad_threshold_too_low_still_parses(tmp_path):
    cfg = _cfg({"VT_VAD_THRESHOLD": "0.01"}, tmp_path)
    assert cfg.vad_threshold == 0.01

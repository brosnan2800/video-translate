"""Unit tests for proxy setup + auto-detection (Spec 07, ADR-003/007)."""
import os

import pytest

from video_translate.proxy import DEFAULT_PROXY, detect_proxy, is_socks, setup_http_proxy

_ALL = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


@pytest.fixture(autouse=True)
def clean_proxy_env():
    saved = {k: os.environ.get(k) for k in _ALL}
    for k in _ALL:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_is_socks():
    assert is_socks("socks5://127.0.0.1:7891")
    assert is_socks("SOCKS4://x")
    assert not is_socks("http://127.0.0.1:7890")


def test_setup_sets_four_http_vars():
    setup_http_proxy("http://127.0.0.1:7890")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert os.environ[k] == "http://127.0.0.1:7890"


def test_setup_pops_socks_all_proxy():
    os.environ["all_proxy"] = "socks5://127.0.0.1:7891"
    os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7891"
    setup_http_proxy(DEFAULT_PROXY)
    assert "all_proxy" not in os.environ
    assert "ALL_PROXY" not in os.environ


def test_setup_rejects_socks():
    with pytest.raises(ValueError):
        setup_http_proxy("socks5://127.0.0.1:7891")


def test_default_proxy_value():
    assert DEFAULT_PROXY == "http://127.0.0.1:7890"


# --- V2: direct connection (None / "") ---


def test_setup_none_is_direct_clears_http_vars():
    os.environ["http_proxy"] = "http://stale:1"
    setup_http_proxy(None)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert k not in os.environ


def test_setup_empty_string_is_direct():
    os.environ["https_proxy"] = "http://stale:1"
    setup_http_proxy("")
    assert "https_proxy" not in os.environ


def test_setup_none_pops_socks():
    os.environ["all_proxy"] = "socks5://x"
    setup_http_proxy(None)
    assert "all_proxy" not in os.environ


# --- V2: detect_proxy ---


def test_detect_no_proxy_flag_returns_none():
    assert detect_proxy(cli_no_proxy=True) is None


def test_detect_cli_proxy_wins():
    assert detect_proxy(cli_proxy="http://cli:1", cli_no_proxy=False,
                        env={"VT_PROXY": "http://env:1"}) == "http://cli:1"


def test_detect_vt_proxy_env():
    assert detect_proxy(env={"VT_PROXY": "http://vt:1"}) == "http://vt:1"


def test_detect_https_proxy_env_fallback():
    assert detect_proxy(env={"HTTPS_PROXY": "http://std:1"}) == "http://std:1"


def test_detect_http_proxy_env_fallback():
    assert detect_proxy(env={"HTTP_PROXY": "http://std:2"}) == "http://std:2"


def test_detect_probe_success(monkeypatch):
    monkeypatch.setattr("video_translate.proxy._probe", lambda h, p, t: True)
    assert detect_proxy(env={}) == "http://127.0.0.1:7890"


def test_detect_probe_failure_returns_none(monkeypatch):
    monkeypatch.setattr("video_translate.proxy._probe", lambda h, p, t: False)
    assert detect_proxy(env={}) is None

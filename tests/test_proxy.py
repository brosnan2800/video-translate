"""Unit tests for proxy setup (Spec 07 Gotcha 2, ADR-003)."""
import os

import pytest

from video_translate.proxy import DEFAULT_PROXY, is_socks, setup_http_proxy

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

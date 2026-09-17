"""Unit tests for proxy setup + auto-detection (Spec 07, ADR-003/007)."""
import os

import pytest

from video_translate.proxy import (
    DEFAULT_PROXY,
    DEFAULT_PROXY_PORT,
    detect_proxy,
    is_socks,
    probe_http_endpoint,
    resolve_probe_port,
    setup_http_proxy,
)

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
    assert DEFAULT_PROXY == f"http://127.0.0.1:{DEFAULT_PROXY_PORT}"
    assert DEFAULT_PROXY_PORT == 7899  # 本地代理软件的实际监听端口（可经 VT_PROXY_PORT 覆盖）


# --- VT_PROXY_PORT: 探测端口可配置（写死端口会让自动探测在他人机器上恒定失败） ---


def test_resolve_probe_port_default():
    assert resolve_probe_port(env={}) == DEFAULT_PROXY_PORT


def test_resolve_probe_port_env_override():
    assert resolve_probe_port(env={"VT_PROXY_PORT": "7890"}) == 7890
    assert resolve_probe_port(env={"VT_PROXY_PORT": " 7891 "}) == 7891


def test_resolve_probe_port_invalid_falls_back():
    """非法值一律回落默认 —— 一个环境变量写错不该让整条探测链崩掉。"""
    for bad in ("", "abc", "0", "-1", "70000", "78.9"):
        assert resolve_probe_port(env={"VT_PROXY_PORT": bad}) == DEFAULT_PROXY_PORT


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
    assert detect_proxy(env={}) == f"http://127.0.0.1:{DEFAULT_PROXY_PORT}"


def test_detect_probe_uses_configured_port(monkeypatch):
    """VT_PROXY_PORT 改变探测端口 —— 代理不在默认端口时也能被自动发现。"""
    seen: list[int] = []

    def _probe(h, p, t):
        seen.append(p)
        return True

    monkeypatch.setattr("video_translate.proxy._probe", _probe)
    assert detect_proxy(env={"VT_PROXY_PORT": "7890"}) == "http://127.0.0.1:7890"
    assert seen == [7890]


# --- probe_http_endpoint: 「端到端通路」判定的唯一实现 ---


class _OkResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_http_endpoint_never_raises(monkeypatch):
    """探测失败只返回 False（DNS / 连接 / TLS / 超时一律如此），绝不抛。"""
    import urllib.request

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert probe_http_endpoint("https://x.invalid/", None, timeout=0.01) is False


def test_probe_http_endpoint_ok_and_restores_proxy_env(monkeypatch):
    """成功时返回 True，且**原样恢复**代理环境（探测不得污染进程状态）。"""
    import urllib.request

    os.environ["http_proxy"] = "http://before:1"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: _OkResp())
    assert probe_http_endpoint("https://x.invalid/", "http://during:2") is True
    assert os.environ["http_proxy"] == "http://before:1"


def test_detect_probe_failure_returns_none(monkeypatch):
    monkeypatch.setattr("video_translate.proxy._probe", lambda h, p, t: False)
    assert detect_proxy(env={}) is None

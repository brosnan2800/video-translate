"""HTTP proxy setup and auto-detection (V2).

`detect_proxy()` resolves a usable HTTP proxy from CLI flags / env / TCP probe,
returning None for a direct connection. `setup_http_proxy(None)` is a no-op
(direct); `setup_http_proxy("<http-url>")` forces the four HTTP env vars.

GOTCHA (load-bearing, ADR-003): the proxy MUST be HTTP, never SOCKS. If
`all_proxy` is set to `socks5://...`, huggingface_hub's httpx client crashes with
a missing-`socksio` error. So when a proxy is set we force the four HTTP proxy
vars and explicitly pop any SOCKS `all_proxy`/`ALL_PROXY`.

V2 deviation from plan: detect_proxy returns None (direct) when no proxy source
is found, rather than raising. Direct egress often works (validated), and raising
would break local-only transcription (model cached, no network needed).
"""
from __future__ import annotations

import os
import socket
import urllib.request

DEFAULT_PROXY_PORT = 7899
DEFAULT_PROXY = f"http://127.0.0.1:{DEFAULT_PROXY_PORT}"

_HTTP_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_SOCKS_VARS = ("all_proxy", "ALL_PROXY")

# 端到端可达性探测的默认目标（见 probe_http_endpoint / preflight）。
GOOGLE_ENDPOINT_URL = (
    "https://translate.google.com/translate_a/single"
    "?client=gtx&q=hello&sl=en&tl=zh-CN&dt=t"
)
# YouTube 的 timedtext 入口：字幕抓取实际会打的端点，用它做通路预检最贴近真实。
YOUTUBE_ENDPOINT_URL = "https://www.youtube.com/"


def resolve_probe_port(env: dict[str, str] | None = None) -> int:
    """TCP 探测端口：``VT_PROXY_PORT`` 优先，缺失/非法回落 :data:`DEFAULT_PROXY_PORT`。

    引入动机：本地代理软件的监听端口因人而异（7890/7899/...）。写死一个端口会让
    「自动探测」在别人的机器上恒定失败并**静默退化为直连**——那正是"代理走不通"
    最难诊断的一种形态（报出来的错和病因无关）。故端口可配，且非法值一律回落默认，
    绝不因一个环境变量写错而让整条探测链崩掉。
    """
    raw = (env if env is not None else dict(os.environ)).get("VT_PROXY_PORT")
    if raw:
        try:
            port = int(str(raw).strip())
            if 0 < port < 65536:
                return port
        except (TypeError, ValueError):
            pass
    return DEFAULT_PROXY_PORT


def is_socks(url: str) -> bool:
    """Return True if `url` is a SOCKS proxy URL."""
    return url.strip().lower().startswith("socks")


def setup_http_proxy(proxy: str | None = DEFAULT_PROXY) -> None:
    """Configure environment for HTTP-only proxying.

    V2: ``proxy=None`` or ``""`` -> direct connection (HTTP vars cleared, SOCKS
    popped). ``proxy=<str>`` -> force the four HTTP env vars + pop SOCKS.

    Raises:
        ValueError: if `proxy` is a SOCKS URL (would break huggingface_hub).
    """
    for k in _SOCKS_VARS:
        os.environ.pop(k, None)
    if proxy is None or proxy == "":
        for k in _HTTP_VARS:
            os.environ.pop(k, None)
        return
    if is_socks(proxy):
        raise ValueError(
            f"SOCKS proxy not supported (breaks huggingface_hub httpx): {proxy!r}. "
            "Use an HTTP proxy, e.g. http://127.0.0.1:7890"
        )
    for k in _HTTP_VARS:
        os.environ[k] = proxy


def _probe(host: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_proxy(
    *,
    env: dict[str, str] | None = None,
    cli_proxy: str | None = None,
    cli_no_proxy: bool = False,
    probe_host: str = "127.0.0.1",
    probe_port: int | None = None,
    probe_timeout: float = 0.5,
) -> str | None:
    """V2: auto-detect a usable HTTP proxy. Returns None for direct connection.

    Resolution order:
      1. ``--no-proxy`` -> None (direct)
      2. ``--proxy X`` -> X (SOCKS rejected later by setup_http_proxy)
      3. ``VT_PROXY`` env -> use it
      4. ``HTTPS_PROXY``/``HTTP_PROXY`` env -> use it
      5. TCP probe ``127.0.0.1:<port>`` -> if open, use ``http://127.0.0.1:<port>``
      6. All else fails -> None (direct; operation works or fails clearly)

    ``probe_port`` 为 None 时从 ``VT_PROXY_PORT`` 解析（见 :func:`resolve_probe_port`）；
    显式传值仅供测试与特殊调用方。

    Never raises: SOCKS URLs are returned as-is and rejected by setup_http_proxy.
    """
    if cli_no_proxy:
        return None
    if cli_proxy:
        return cli_proxy
    env = env if env is not None else dict(os.environ)
    if env.get("VT_PROXY"):
        return env["VT_PROXY"]
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if env.get(k):
            return env[k]
    port = probe_port if probe_port is not None else resolve_probe_port(env)
    if _probe(probe_host, port, probe_timeout):
        return f"http://{probe_host}:{port}"
    return None


def probe_http_endpoint(url: str, proxy: str | None,
                        timeout: float = 5.0) -> bool:
    """``url`` 经 ``proxy``（或直连）是否可达。**Never raises**。

    临时应用代理环境变量，探测后**原样恢复**（探测不得污染进程状态）。返回
    ``200 <= status < 400``；任何异常（DNS / 连接 / TLS / 超时）一律 False。

    「端到端通路」判定的**唯一实现**：doctor 的 Google 探测与 captions 的
    preflight 都复用它，避免各自维护一套超时与异常口径。
    """
    saved: dict[str, str | None] = {}
    for k in _HTTP_VARS + _SOCKS_VARS:
        saved[k] = os.environ.get(k)
    try:
        setup_http_proxy(proxy)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False
    finally:
        for k in _HTTP_VARS + _SOCKS_VARS:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]


def _probe_google_endpoint(proxy: str | None, timeout: float = 5.0) -> bool:
    """Google Translate 端点可达性（doctor · ``--engine google``）。"""
    return probe_http_endpoint(GOOGLE_ENDPOINT_URL, proxy, timeout)


def _probe_youtube_endpoint(proxy: str | None, timeout: float = 5.0) -> bool:
    """YouTube 可达性（captions 的通路预检）。"""
    return probe_http_endpoint(YOUTUBE_ENDPOINT_URL, proxy, timeout)

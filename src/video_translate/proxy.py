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

DEFAULT_PROXY = "http://127.0.0.1:7890"

_HTTP_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_SOCKS_VARS = ("all_proxy", "ALL_PROXY")


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
    probe_port: int = 7890,
    probe_timeout: float = 0.5,
) -> str | None:
    """V2: auto-detect a usable HTTP proxy. Returns None for direct connection.

    Resolution order:
      1. ``--no-proxy`` -> None (direct)
      2. ``--proxy X`` -> X (SOCKS rejected later by setup_http_proxy)
      3. ``VT_PROXY`` env -> use it
      4. ``HTTPS_PROXY``/``HTTP_PROXY`` env -> use it
      5. TCP probe 127.0.0.1:7890 -> if open, use ``http://127.0.0.1:7890``
      6. All else fails -> None (direct; operation works or fails clearly)

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
    if _probe(probe_host, probe_port, probe_timeout):
        return f"http://{probe_host}:{probe_port}"
    return None


def _probe_google_endpoint(proxy: str | None, timeout: float = 5.0) -> bool:
    """Return True if the Google Translate endpoint is reachable via `proxy`.

    Temporarily applies the proxy env, performs a tiny HEAD/GET, then restores
    the previous proxy env. Never raises — network failures return False.
    """
    saved: dict[str, str | None] = {}
    for k in _HTTP_VARS + _SOCKS_VARS:
        saved[k] = os.environ.get(k)
    try:
        setup_http_proxy(proxy)
        url = ("https://translate.google.com/translate_a/single"
               "?client=gtx&q=hello&sl=en&tl=zh-CN&dt=t")
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

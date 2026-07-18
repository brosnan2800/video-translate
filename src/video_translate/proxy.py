"""HTTP proxy setup for model download (HuggingFace) and translation (Google).

GOTCHA (load-bearing): the proxy MUST be HTTP, never SOCKS. If `all_proxy` is set
to `socks5://...`, huggingface_hub's httpx client tries to use it and crashes with
a missing-`socksio` error. So we force the four HTTP proxy vars and explicitly
pop any SOCKS `all_proxy`/`ALL_PROXY`.
"""
from __future__ import annotations

import os

DEFAULT_PROXY = "http://127.0.0.1:7890"

_HTTP_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_SOCKS_VARS = ("all_proxy", "ALL_PROXY")


def is_socks(url: str) -> bool:
    """Return True if `url` is a SOCKS proxy URL."""
    return url.strip().lower().startswith("socks")


def setup_http_proxy(proxy: str = DEFAULT_PROXY) -> None:
    """Configure environment for HTTP-only proxying.

    Sets the four HTTP proxy env vars and removes any SOCKS all_proxy/ALL_PROXY.

    Raises:
        ValueError: if `proxy` is a SOCKS URL (would break huggingface_hub).
    """
    if is_socks(proxy):
        raise ValueError(
            f"SOCKS proxy not supported (breaks huggingface_hub httpx): {proxy!r}. "
            "Use an HTTP proxy, e.g. http://127.0.0.1:7890"
        )
    for k in _HTTP_VARS:
        os.environ[k] = proxy
    for k in _SOCKS_VARS:
        os.environ.pop(k, None)

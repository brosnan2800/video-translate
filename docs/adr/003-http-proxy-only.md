# ADR-003 — HTTP proxy only (reject SOCKS)

- **Status**: Accepted
- **Date**: 2026-07-16
- **Context**: The user runs Clash locally at `127.0.0.1:7890`. Both HF model
  download (`huggingface_hub`, via httpx) and Google translation need to traverse
  it. httpx's SOCKS support is optional/fragile and, in practice, a `socks5://`
  value in `all_proxy` breaks `huggingface_hub` downloads. The user also
  explicitly does NOT want a global proxy in `zshrc` (it would break domestic
  sites when Clash is off).

## Decision
`proxy.py::setup_http_proxy(proxy="http://127.0.0.1:7890")`:
1. Reject SOCKS URLs (`socks5://`/`socks4://`) with `ValueError` → CLI exit 4.
2. Force the four env vars `http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY`.
3. **Pop** `all_proxy/ALL_PROXY` so a stray SOCKS value can't leak into httpx.

The proxy is applied per-invocation inside the process, never written to the
shell profile.

## Consequences
- **Positive**: Reliable HF downloads and translation through Clash; no global
  shell pollution; clear, fast failure on a misconfigured SOCKS proxy.
- **Negative**: Users whose only proxy is SOCKS must front it with an HTTP proxy
  (Clash already exposes `7890` as HTTP). Documented in README/gotchas.

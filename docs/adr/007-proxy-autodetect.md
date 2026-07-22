# ADR-007 — Proxy auto-detection

Date: 2026-07-19 · Status: accepted

## Context
V1 hardcoded `proxy = http://127.0.0.1:7890` (Clash). If Clash was off, every
network call failed with a cryptic "connect to 7890" error. The user hit this
repeatedly.

## Decision
`proxy.detect_proxy()` resolves a proxy at runtime, in this order:
1. `--no-proxy` → `None` (direct)
2. `--proxy X` → `X` (SOCKS still rejected later by `setup_http_proxy`)
3. `VT_PROXY` env → use it
4. `HTTPS_PROXY` / `HTTP_PROXY` env → use it (only if VT_PROXY unset)
5. TCP probe `127.0.0.1:7890` → if open, use `http://127.0.0.1:7890`
6. else → `None` (direct)

`setup_http_proxy(None)` clears HTTP env vars (direct); `setup_http_proxy("<url>")`
forces them. SOCKS is still rejected (ADR-003 unchanged).

## Deviation from plan
The plan said probe failure should raise `ProxyNotAvailableError`. **This was
changed to return `None` (direct)** because:
- Direct egress often works (validated: this environment reaches Google directly).
- Raising would break local-only transcription (model cached, no network needed).
- A clear downstream failure is preferable to a hard gate.

## Consequences
- `--no-proxy` enables fully offline transcribe+merge (model cached).
- Standard `HTTPS_PROXY`/`HTTP_PROXY` are now respected (CI-friendly) without
  polluting the user's shell (their `.zshrc` intentionally exports no proxy).
- Probe adds ≤0.5s latency only when no explicit proxy is given.

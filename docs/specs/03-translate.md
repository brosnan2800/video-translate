# Spec 03 — Translate

Module: `translate.py` (+ `proxy.py`). Produces `{base}.zh_segments.json` and,
optionally, `{base}.agent_pending.json`.

## Algorithm
1. `setup_http_proxy(proxy)` — force HTTP proxy env, reject SOCKS (ADR-003).
2. Load `segments_en.json`; load existing `out` as resume checkpoint (parse keys
   to int; corrupt/missing → start fresh).
3. For each segment index `i` not already done:
   - `translate_one(text)` with up to `MAX_RETRIES=3`, `sleep(1+attempt)` backoff.
   - Empty/whitespace text → "" (no network call).
   - On final failure → append `{index, start, end, text}` to `pending`.
4. Checkpoint to `out` every `CHECKPOINT_EVERY=10` segments and at the end.
5. If `pending_path` given, write `pending` (even if empty → `[]`).

## Engine
- Primary: `deep_translator.GoogleTranslator(source=src, target=tgt)` (via proxy).
- Defaults: `src="en"`, `tgt="zh-CN"`, `proxy="http://127.0.0.1:7890"`.
- **Injectable**: `translate_fn` parameter allows swapping the engine (agent
  fallback, or a stub in tests) without touching the network.

## Fallback policy (user-specified)
Google is the primary. Segments Google cannot translate go to
`agent_pending.json` for the current agent to backfill — NOT MyMemory or other
low-quality free engines.

## Contract (testable without network — uses golden `zh_segments.json`)
- Output is `{str_index: str}`.
- Completeness: for the golden pair, every English index has a Chinese value.
- Resume: re-running with a full checkpoint performs zero translation calls.
- Incremental save: a crash after any checkpoint leaves a valid partial `out`.

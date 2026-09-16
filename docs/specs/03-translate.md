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

## Engine (V2: `--engine {agent,google}`, default `agent`)
- **agent** (default, Spec 09 / ADR-005): the CLI does NOT call an LLM. It emits a
  translation task file (`<base>.translate_task.json`, batched + context + persona)
  and returns exit 6 (`EXIT_AWAITING_AGENT`). The calling agent translates with its
  own LLM and writes `zh_segments.json`. No LLM client dependency.
- **Style track (T3 / ADR-027 / Spec 21)**: pass `--style {film,literal,bilingual_study}`
  to inject a preset persona + guidelines into the task file (`version: 3`, top-level
  `style` field). Multi-value `--style film,literal` emits one suffixed task per style
  (`<base>.film.translate_task.json` / `<base>.literal.translate_task.json`). An explicit
  `--persona`/`VT_PERSONA` overrides the style preset. Default `film` is byte-compatible
  with the legacy single-persona task.
- **google** (headless fallback, V1 path): `deep_translator.GoogleTranslator`
  via auto-detected HTTP proxy. Defaults: `src="en"`, `tgt="zh-CN"`.
- **Injectable**: `translate_fn` allows swapping the programmatic engine (used by
  google path and tests) without touching the network.

## Fallback policy (user-specified)
Google is the primary. Segments Google cannot translate go to
`agent_pending.json` for the current agent to backfill — NOT MyMemory or other
low-quality free engines.

## Contract (testable without network — uses golden `zh_segments.json`)
- Output is `{str_index: str}`.
- Completeness: for the golden pair, every English index has a Chinese value.
- **Single-line invariant (ADR-040)**: 每个 value 必须是单行（不含 `\r` / `\n`）。
  译文在**源头**即被压平（`translate_one` 与 `translate_segments` 均经
  `text_utils.to_single_line`；后者覆盖所有注入引擎），`generate` 在边界再兜一次。
  断行由剪辑软件处理，不由译文携带；双语 cue 的分行来自 `block()` 的 `lines`，非文本内容。
- Resume: re-running with a full checkpoint performs zero translation calls.
- Incremental save: a crash after any checkpoint leaves a valid partial `out`.

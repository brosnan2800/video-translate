# Spec 09 — Agent translation engine

Module: `translate.py` (V2 additions). Decision 0 / 1 / 3.

## Principle
The CLI does **not** call any LLM API. The calling agent (WorkBuddy / Claude Code /
Cursor / …) IS the translation engine — it reads a structured task file, translates
using its own LLM, and writes `zh_segments.json`. No `openai`/`httpx`/`anthropic`
dependency is added.

## `--engine {agent,google}` (default `agent`)
- **agent** (default): `prepare_translate_task` emits `<base>.translate_task.json`;
  the CLI returns `EXIT_AWAITING_AGENT` (6) with a `[AWAITING_AGENT]` marker and
  copy-paste `generate` instructions.
- **google**: V1 `translate_segments` via `deep_translator` (headless fallback).

## Task file schema (`prepare_translate_task`)
```json
{
  "version": 1,
  "persona": "<信达雅+口语感 default, configurable via [llm] persona>",
  "output_schema": { "type": "object", "description": "str(index) -> zh", ... },
  "batches": [
    {
      "batch_index": 0,
      "context_before": [{"index": 0, "text": "..."}, ...],   // sliding window
      "to_translate":  [{"index": 2, "text": "..."}, ...],     // the items to fill
      "context_after":  [{"index": 10, "text": "..."}, ...]
    }
  ]
}
```
Defaults: `batch_size=8`, `context_window=2` (before+after). `index_key` (default
None = positional; `"index"` for backfill to preserve original indices).

## `validate_zh(segments_path, zh_path) -> (ok, missing_indices)`
Checks every segment index has a zh entry. Used by the agent to self-check before
`generate`.

## `merge_agent_zh(zh_path, agent_zh_path)`
Merges agent-filled zh into `zh_segments.json` (existing keys kept, new ones
added/overwritten; int keys normalised to str). Used by `backfill --agent-zh`.

## Exit code 6
`EXIT_AWAITING_AGENT = 6` — transcribe+task done; the agent must translate and run
`generate`. Not an error. Documented in `AGENTS.md` + `README.md`.

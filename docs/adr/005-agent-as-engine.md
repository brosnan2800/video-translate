# ADR-005 — Agent as translation engine (no LLM client dependency)

Date: 2026-07-19 · Status: accepted

## Context
V1 used Google Translate (deep-translator) as the sole engine. The user found
the output "soulless" (literal, word-for-word). An LLM would produce more natural,
context-aware translation, but bundling an LLM client (openai/anthropic/httpx)
into the CLI means API keys, cost, and a dependency the user must configure.

## Decision
The CLI's default engine is **agent**: it emits a structured translation task
file and returns exit 6. The calling agent — which already has a capable LLM —
translates the task using its own model and writes `zh_segments.json`. The CLI
adds **no LLM client dependency**. Google remains as `--engine google` headless
fallback.

## Rationale
- The tool's primary usage is via an agent (WorkBuddy/Claude Code/Cursor). The
  agent already IS an LLM; having the CLI call a second LLM API is redundant.
- Avoids API-key/cost/dependency burden on the CLI.
- Inherits V1's `AGENTS.md §2` fallback flow (agent backfills pending) and
  promotes it to the primary path.

## Consequences
- `run --engine agent` (default) is not end-to-end: it stops at exit 6 and the
  agent must translate + `generate`. `run --engine google` is the headless
  end-to-end path.
- Golden zh is retranslated with Google (deterministic), not agent (ADR: golden
  requires reproducibility; LLM output drifts). See decision 决裁定稿 §1.

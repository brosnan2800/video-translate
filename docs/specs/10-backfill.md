# Spec 10 — Backfill subcommand

Module: `cli.py::cmd_backfill`. Decision A5. Codefies the V1 `AGENTS.md §2`
manual fallback into a one-command flow.

## Two modes

### Prepare (default)
```
video-translate backfill --pending <base>.agent_pending.json --out <base>.zh_segments.json
```
Reads `agent_pending.json` (`[{index, start, end, text}]`), writes
`<base>.backfill_task.json` via `prepare_translate_task(..., index_key="index")`
so the task carries the **original** zh_segments indices (not positional).
Returns `EXIT_AWAITING_AGENT` (6) with instructions.

### Merge (`--agent-zh`)
```
video-translate backfill --pending <base>.agent_pending.json --out <base>.zh_segments.json \
    --agent-zh <filled.json> --segments <base>.segments_en.json --outdir <dir> --base <base>
```
`merge_agent_zh` merges the agent-filled translations into `zh_segments.json`
(preserving existing keys), then runs `generate` to refresh the four output files.
Returns `EXIT_OK`.

## Index preservation (critical)
`agent_pending.json` items carry their original `index`. `prepare_translate_task`
is called with `index_key="index"` so `to_translate[*].index` are the real
zh_segments keys. The agent fills those exact keys; `merge_agent_zh` writes them
back correctly. (Positional indexing would corrupt the mapping.)

## Unification with the agent engine
`backfill` and `translate --engine agent` share `prepare_translate_task`. An agent
that has learned to fill a translate task can fill a backfill task identically.

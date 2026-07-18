# AGENTS.md — Execution guide for AI agents

You are an AI agent asked to generate bilingual subtitles from a video using this
project. Follow this protocol exactly. It is tool-agnostic (WorkBuddy, Claude
Code, Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules
below encode resume-safety, proxy correctness, and output verification that are
easy to get wrong.

Read order: this file → [`docs/specs/00-overview.md`](docs/specs/00-overview.md)
if you need behavior details → relevant [`docs/adr/`](docs/adr) for the "why".

---

## 0. Preflight (always run first)

```bash
cd <project-root>
test -f pyproject.toml && echo "in project root: ok"
.venv/bin/video-translate doctor    # or: make doctor
```

`doctor` reports: ffmpeg/ffprobe present, HF cache dir, model cached, device=cpu,
compute_type=int8, whether faster-whisper & deep-translator import.

Decide from the report:
- **ffmpeg/ffprobe MISS** → stop; ask the human to `brew install ffmpeg`.
- **faster-whisper NOT installed** → run `make install-dev`.
- **model NOT cached** → run `video-translate setup` (downloads large-v3 once,
  ~3GB, into the shared `~/.cache/huggingface`; reused next time).
- **No HTTP proxy / Clash down** → transcription's model download and translation
  will fail. Confirm the proxy (default `http://127.0.0.1:7890`) is up. **Never
  pass a SOCKS proxy** — it is rejected (exit 4).

## 1. Run the pipeline

Preferred one-shot:

```bash
.venv/bin/video-translate run \
    --input "<video>" --outdir outputs --base <base>
```

Or stage-by-stage (useful when a stage was interrupted):

```bash
video-translate transcribe --input "<video>" --outdir outputs --base <base>
video-translate translate  --segments outputs/<base>.segments_en.json \
                           --out outputs/<base>.zh_segments.json \
                           --pending outputs/<base>.agent_pending.json
video-translate generate   --segments outputs/<base>.segments_en.json \
                           --zh outputs/<base>.zh_segments.json \
                           --outdir outputs --base <base>
```

### Resume rules (do NOT delete intermediates)
- Transcription is **chunked & resumable**: `chunk_N.json` files in `outdir` are
  the checkpoints. If the process is killed (exit code 5), just **re-run the same
  command** — completed chunks are skipped. Deleting them forces a full re-run.
- Translation checkpoints `zh_segments.json` every 10 segments; re-running skips
  already-translated indices.
- To reuse existing transcription and only redo later stages:
  `run --skip transcribe`.

## 2. Handle the translation fallback (your job as an agent)

Google Translate is primary. Any segment it fails to translate is written to
`outputs/<base>.agent_pending.json` as `[{index, start, end, text}, ...]`.

If `agent_pending.json` is non-empty:
1. Translate each `text` **yourself** (you are the fallback engine — higher
   quality than free alternates; do NOT wire in MyMemory or similar).
2. Merge your translations into `zh_segments.json` under the matching `index`
   (as a string key), preserving all existing entries.
3. Re-run `generate` to refresh the four output files.

Aim for the *soul* of the sentence, not word-for-word — this is exactly why a
human/agent fallback exists instead of pure machine translation.

## 3. Verify before you report done

- **Existence**: four files `outputs/<base>.{bilingual.srt,zh.srt,en.srt,txt}`.
- **Alignment sanity**: open `<base>.bilingual.srt`; the first cue's timestamp
  must match the first segment in `segments_en.json`. Timestamps must be
  monotonic and never negative.
- **Completeness**: `agent_pending.json` is empty (or you have backfilled it).
- **Regression (if you touched code)**: `make test` must be green. The golden
  test (`tests/test_generate_golden.py`) proves output is byte-exact vs the
  validated `docs/golden/apollo_story.*` baseline. If it fails, you changed
  output formatting — revert or update golden intentionally with a note.

## 4. Deliver

Report the four output paths to the human and note anything you backfilled. The
importable file for 剪映 is `<base>.bilingual.srt`.

---

## Hard rules (violating these produces broken subtitles)

1. Never recompute timestamps in translate/generate — copy them verbatim.
2. HTTP proxy only; SOCKS is rejected.
3. Device is cpu/int8 — do not try cuda unless on a verified NVIDIA box (would
   need a new ADR).
4. Never delete `chunk_N.json` / partial JSON to "clean up" — you destroy resume.
5. zh index is 0-based in the map; segment `i` (1-based) reads `zh[i-1]`.

See [`docs/specs/07-gotchas.md`](docs/specs/07-gotchas.md) for the full list.

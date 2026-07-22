# AGENTS.md — Execution guide for AI agents (V2)

You are an AI agent asked to turn a video into bilingual (zh/en) subtitles using
this project. Follow this protocol. It is tool-agnostic (WorkBuddy, Claude Code,
Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules below
encode resume-safety, proxy correctness, the agent-as-engine translation step,
and output verification.

Read order: this file → [`docs/specs/00-overview.md`](docs/specs/00-overview.md)
for behavior → [`docs/adr/`](docs/adr) for the "why".

---

## 0. Trigger & video interaction (conversational)

When the user asks to translate/subtitle a video ("翻译这个视频 / 生成字幕 /
做中英字幕"):

1. **Find the video.** First look in the project's `videos/` directory.
   - If one video is there, use it.
   - If several, list them and ask the user which one.
   - If `videos/` is empty or missing, ask the user for the path. Supported
     formats: anything ffmpeg handles (mp4/mov/mkv/webm/avi/m4a/mp3/wav/flac…).
2. **Probe + confirm.** Run `ffprobe` to get duration and detect the audio
   language; show the user a one-line summary ("检测到 12 分钟、英文音轨") and
   **wait for confirmation** ("开始？") before proceeding.
3. **Default base/outdir.** `--base` defaults to the video filename stem;
   `--outdir` defaults to the video's own directory. Override only if the user
   asks.

## 1. Preflight (always run first)

```bash
cd <project-root>
test -f pyproject.toml && echo "in project root: ok"
.venv/bin/video-translate doctor    # or: make doctor
```

`doctor` reports ffmpeg/ffprobe, HF cache, model cached, deps, and V2 defaults
(`engine: agent`, `lang: auto-detect`, `proxy: auto-detect`).

Decide from the report:
- **ffmpeg/ffprobe MISS** → stop; ask the human to `brew install ffmpeg`.
- **faster-whisper NOT installed** → run `make install-dev`.
- **model NOT cached** → run `video-translate setup` (downloads large-v3 once,
  ~3GB, into the shared `~/.cache/huggingface`; reused next time).
- **proxy** → auto-detected. If the user has no proxy and needs Google/network,
  confirm direct egress works, or have them start Clash (7890). `--no-proxy`
  forces direct (fine for transcribe-only when the model is cached).

## 2. Run the pipeline (agent engine, default)

The CLI's default `--engine agent` does NOT translate itself — it transcribes,
merges, emits a translation task, and stops at **exit 6** for you to translate.

```bash
.venv/bin/video-translate run "<video>"           # base/outdir auto from video path
# → produces <video_dir>/<base>.segments_en.json (merged)
#   + <video_dir>/<base>.translate_task.json
#   exit 6: [AWAITING_AGENT]
```

Exit 6 means: **transcribe + merge done; YOU translate now.**

### Your translation step (you are the engine)
1. Read `<base>.translate_task.json`. It has `persona`, `batches[]` each with
   `context_before` / `to_translate` / `context_after`, and an `output_schema`.
2. For each batch, translate the `to_translate` items to Chinese **following the
   persona** (信达雅 + 口语感 — aim for the soul of the sentence, not word-for-word).
   Use the context_before/after for coherence.
3. Write `<base>.zh_segments.json` — a JSON object `{"<str(index)": "<zh>", …}`
   covering **every** index in `to_translate[*].index`.
4. (Optional sanity check) `.venv/bin/python -c "from video_translate.translate import validate_zh; print(validate_zh('<base>.segments_en.json', '<base>.zh_segments.json'))"`.
5. Generate the subtitles:
   ```bash
   .venv/bin/video-translate generate \
       --segments <base>.segments_en.json --zh <base>.zh_segments.json \
       --outdir <video_dir> --base <base>
   ```

### Headless alternative (`--engine google`)
If no agent translation is wanted (lower quality, fully automatic):
```bash
.venv/bin/video-translate run "<video>" --engine google
```
Runs transcribe → Google MT → generate end-to-end. Failures land in
`<base>.agent_pending.json`.

## 3. Backfill (if Google failed some segments)

```bash
# 1. prepare a task for just the failed segments (original indices preserved)
.venv/bin/video-translate backfill --pending <base>.agent_pending.json \
    --out <base>.zh_segments.json
# → exit 6: [AWAITING_AGENT], writes <base>.backfill_task.json

# 2. you translate the backfill task -> your_zh.json ({"<orig_index>": "<zh>"})
# 3. merge + regenerate
.venv/bin/video-translate backfill --pending <base>.agent_pending.json \
    --out <base>.zh_segments.json --agent-zh your_zh.json \
    --segments <base>.segments_en.json --outdir <video_dir> --base <base>
```

## 4. Resume rules (do NOT delete intermediates)
- Transcription is **chunked & resumable**: `chunk_N.json` files in `outdir` are
  the checkpoints. Re-running the same command skips completed chunks.
- Merge copies raw to `<base>.segments_raw.json` then writes merged to
  `<base>.segments_en.json`. `--no-merge` skips merge (raw stays as `segments_en`).
- Translation (`--engine google`) checkpoints `zh_segments.json` every 10 segments.
- To reuse existing transcription and only redo later stages: `run --skip transcribe`.

## 5. Verify before you report done
- **Existence**: four files `<base>.{bilingual.srt,zh.srt,en.srt,txt}`.
- **Alignment sanity**: first cue's timestamp matches the first merged segment;
  timestamps monotonic, never negative.
- **Completeness** (`--engine google`): `agent_pending.json` is empty or backfilled.
- **Regression (if you touched code)**: `make test` green. Golden tests:
  `test_generate_golden` (build_outputs byte-exact), `test_merge_golden` (merge
  determinism), `test_v1_golden_preserved` (V1 archived as `.v1`).

## 6. Deliver
Report the four output paths to the human and note anything you backfilled. The
importable file for 剪映 is `<base>.bilingual.srt`.

---

## Hard rules (violating these produces broken subtitles)
1. Never recompute timestamps in merge/translate/generate — copy them verbatim.
   Merge takes group's first start / last end (real acoustic boundaries).
2. HTTP proxy only; SOCKS is rejected. `--no-proxy` = direct.
3. Device is cpu/int8 (ADR-001).
4. Never delete `chunk_N.json` / `segments_raw.json` / partial JSON to "clean up"
   — you destroy resume.
5. zh index is 0-based in the map; segment `i` (1-based) reads `zh[i-1]`.
6. Exit 6 = awaiting your translation; not an error. Don't retry the same `run`
   expecting a different result — translate the task and `generate`.

See [`docs/specs/07-gotchas.md`](docs/specs/07-gotchas.md) for the full list.

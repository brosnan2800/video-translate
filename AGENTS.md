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

---

## V3 additions (word-level, splitting, glossary, doctor probe)

- **`doctor` now also probes Google reachability** (V3, Spec 15): it resolves the
  proxy and actually checks the Google Translate endpoint. By default it still
  exits 0 and only prints `[MISS]` if unreachable — so a 7-minute transcribe
  won't fail first. Add `--strict` to make unreachable return exit 7.
- **Word-level timestamps (V3, Spec 12):** transcribe now uses `stable_whisper`
  with `word_timestamps=True`. `chunk_N.json` and `segments_en.json` carry a
  `words` list per segment. These power split + silence preservation.
- **Splitting (V3, Spec 13):** after merge, long cues are split at **word
  boundaries** to ~42 chars (`--merge-max-chars`). Default ON; `--no-split`
  restores V2 behavior. Because split changes the cue count, a V3 `zh` must be
  **retranslated** from the new `segments_en.json` — never reuse a V2 `zh`.
- **Silence preservation (V3, Spec 15):** cues use first-word/last-word
  boundaries (no leading-silence "early" cue), and `--gap` (default 0.2s) only
  trims trailing silence, never fabricates gaps. Real pauses survive from the
  source via `split_by_gap`.
- **Glossary (V3, Spec 14):** pass `--glossary PATH` (txt/json) to keep
  character/proper-noun names consistent across episodes. It is injected into
  the task's persona context (soft guidance, not forced replacement).
- See [`docs/design/translation-design.md`](docs/design/translation-design.md)
  for the principles, and [`docs/V3-STATUS.md`](docs/V3-STATUS.md) for what
  shipped / what's deferred.

## V4 additions (quality, layout, drift-snap, scene context)

- **Beam search (V4):** transcribe now uses `BEAM_SIZE=5, BEST_OF=5` instead of
  greedy — ~3-5× slower (still CPU), much fewer hallucinations.
  `CONDITION_ON_PREVIOUS_TEXT=False` breaks cross-chunk echo loops.
- **Hallucination filter (V4):** `drop_hallucination_segments()` uses two-signal
  detection (word-collapse ≥50% **and** shared 3-gram with neighbour). Safe to
  leave on; part of the default merge pipeline.
- **Cache fingerprint (V4):** chunk cache names now include a sha1 of **all
  recipe params** (`{base}.{fp}.chunk_N.json`). Changing any transcribe param
  auto-invalidates old caches — no need to manually `make clean`.
- **Output layout (V5):** final files go to `<outdir>/<base>/` with `_vN`
  collision bumps. 剪映 always imports fresh. Use `--flat` for legacy layout,
  `--prune-old` to keep only 2 newest.
- **Display window (V6):** `--offset` (default 0) and `--tail` (default 0.3s)
  shift the display window to fix "subtitle ahead of speech." These **never**
  touch alignment timestamps — only the SRT display range.
- **Drift-snap (V6):** `snap_drifted_words()` detects DTW word-timestamp drift
  (a word seconds before its sentence) and snaps it before split. Default ON;
  `--no-drift-snap` to disable. This prevents stray orphan cues like "可" from
  mis-split words.
- **Scene context (V6):** pass `--source "电影《天国王朝》..."` to inject
  film/scene background into the translation persona. The agent task (v2) ships
  a full English transcript so the LLM translates with whole-scene awareness.
  `translate_task.json` now has `source`, `guidelines`, `full_transcript`,
  `full_transcript_truncated` fields.
- **VAD threshold (V4):** `VAD_THRESHOLD` lowered to 0.35 (was 0.5);
  `--vad-threshold` exposes it. Trade-off: very short opening utterances
  may be missed (e.g. "Saladin" at 1.84s). Manual cue recommended for imports.

## V7 additions (quiet / low-volume & whisper video handling)

Discovered 2026-08-04 on the 《母与子》上/下 clips. **No code changed** — V7 is
a battle-tested *operating procedure* for videos whose audio is too quiet/low
for the default VAD (0.35) to segment correctly. The default V1–V6 pipeline
silently drops most speech as "silence" on these.

### Root cause
VAD mis-segments quiet audio because the **level is too low**, not because the
model is weak. Healthy speech ≈ mean −16..−20 dB / max ≈ 0 dB. When
`mean_volume < -20` or `max_volume < -5`, the default VAD 0.35 treats most of
the clip as silence (e.g. 24 of 26 s cut on a −20.9 / −5.3 dB clip).

Also note: VAD leakage is **not** limited to quiet audio. On music-heavy clips
where speech is buried under the score, the overall level can still read normal
(e.g. −14 dB) yet VAD 0.35 drops most speech as "silence" — see the
LongLiveTheKing case (2026-08-05): default VAD emitted only 2 stray fragments,
but a VAD-off bare run exposed 30 s of real dialogue. **Whenever the default
VAD output looks suspiciously sparse or timestamps are oddly split, run a
VAD-off bare pass first** before delivering.

### Standard procedure (quiet/low video)
1. **Probe level:** `ffmpeg -i <vid> -af volumedetect -f null -`
   If mean < -20 or max < -5 → low level, proceed to normalize.
2. **Normalize (audio only, stream-copy video):**
   `ffmpeg -y -i <vid> -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy -c:a aac -b:a 192k /tmp/<name>_norm.mp4`
3. **DELETE old chunk cache** — `transcribe_fingerprint` keys on params only,
   NOT audio content. After normalization the params are unchanged, so a stale
   cache would be reused. `rm videos/<name>.<fp>.chunk_*.json` before re-running.
4. **Run with tuned VAD + locked language:**
   `run /tmp/<name>_norm.mp4 --base <name> --outdir <orig_dir> --vad-threshold 0.1 --lang en --tail 0.4`
   (normalized level is healthy, so 0.1 is safe; `--lang en` avoids the V6
   "hi"/Indic mis-detect trap). If 0.1 still misses segments, probe sub-ranges
   with `ffmpeg -ss N -i <norm> -af volumedetect` to tell real silence/music vs
   dropped speech.
5. **Verify & generate** as normal (Section 2).

### Hard-won pitfalls (the actual value of V7)
- **Cache trap:** normalized and original files share one cache fingerprint →
  future runs on the *original* would hit the normalized cache. Clear manually.
- **Regeneration discipline:** when re-`generate`ing the same video multiple
  times, **never `rm -rf` the output subfolder** to dodge the collision bump —
  that freezes the filename at the no-suffix first version and 剪映 re-imports
  the stale cached file. Let `generate` auto-bump `_v1/_v2`, or `mv` the final
  to `<base>_vN`.
- **Whisper / faint speech:** acoustic features don't look like speech, so
  `loudnorm` does NOT help. Solution: disable VAD (`vad_filter=False`) and let
  faster-whisper run bare over the whole clip. The CLI hard-codes
  `vad_filter=True`, so use a standalone script calling the faster-whisper API
  (extract audio → `WhisperModel.transcribe(vad_filter=False)` → restore
  timestamps + N offset). **Always set `HF_HUB_OFFLINE=1
  TRANSFORMERS_OFFLINE=1`** to avoid proxy hits when checking the model.
- **VAD over-split + isolated hallucination:** the opposite trap — VAD can cut
  transient env/music into short fragments; whisper then hallucinates filler
  ("I love you, baby.") on them with no context. **Always cross-check VAD
  fragments against a VAD-off, `word_timestamps=True` bare run** before trusting
  them.
- **Translation method (quiet/whisper mis-IDs):** for known films/clips,
  **WebSearch the original script** (search a unique line). Priority:
  original script > context inference > phonetic guess. Edit `segment.text` to
  fix the EN line without shifting timestamps.

### Limits
- VAD 0.1 still correctly marks pure music/ambient as non-speech (expected).
- whisper still mis-hears very faint speech (e.g. martyr→mother); correct via
  context at the translation layer, keep the EN line as transcribed.

# AGENTS.md — Execution guide for AI agents (V2)

You are an AI agent asked to turn a video into bilingual (zh/en) subtitles using
this project. Follow this protocol. It is tool-agnostic (WorkBuddy, Claude Code,
Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules below
encode resume-safety, proxy correctness, the agent-as-engine translation step,
and output verification.

Read order: this file → [`docs/specs/00-overview.md`](docs/specs/00-overview.md)
for behavior → [`docs/adr/`](docs/adr) for the "why".

---

## 项目铁律（cross-tool hard rules — 任何工具 / 任何 Agent 都必须遵守）

1. **本协议（AGENTS.md）是项目唯一权威。** 开工前先 Read 本文件；不要用各自记忆替代。
   README 是指路口，AGENTS.md 是规则本身。换工具（Claude Code / Codex / Trae / Cursor）
   也只读这个文件，不依赖 WorkBuddy 的私有 memory。
2. **每次提交必须同步文档。** 改代码的同时，必须更新 `AGENTS.md` + `README.md` +
   相关 `docs/specs`、`docs/adr`；只改代码不更新文档的提交视为**未完成**，不得合并/推送。
3. **改代码始终遵循 SDD + TDD。** 先写/更新 spec（行为）+ ADR（决策），再用测试护航
   （`make test` 必须 green，golden 测试保字节级稳定）。

> 注意：`.workbuddy/`（含 memory/skills）是 WorkBuddy 私有数据，**永不提交**；
> 已写入 `.gitignore`。跨工具可移植的规则只能落在仓库文件（AGENTS.md / README / docs）里。

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

   #### 纯文本诗歌翻译（纯文本，非带轴字幕）
   当待译内容是**纯文学诗歌**（不嵌入视频、无时间轴约束）时，在「信达雅 + 口语感」的
   散文基线之上，额外遵循 **许渊冲三美论**：

   - **意美（神似）**：抓原诗灵魂与意境，不逐字死译；隐喻、象征、双关必须保留，
     找不到中文近似双关时标注原文字面 + 意涵，而非硬翻。
   - **音美（可诵）**：保留原诗的押韵 scheme、节奏、顿挫；中文译文音节数尽量贴近
     原句，便于吟诵。此场景**无时间轴**，韵律与形式可完整保留，不必为同步破韵。
   - **形美（形似）**：行数、节、句式、诗体（古体 / 近体 / 自由诗）尽量对应。

   并遵循 **形似 / 神似辩证**：直译保形、意译保神，二者冲突时以「意美」优先，但不得
   牺牲音美与形美到不可诵、不可辨诗体的程度。

   文化专有项（典故、用典）密度显著高于口语场景，须有固定处理规则：保留原典 + 注，
   或替换为本土等价意象，不得逐句即兴。

   > 注意：本条仅适用于**纯文本诗歌**。带时间轴的字幕（含夹带歌词 / rap）遵循上文
   > 散文 / 口语规范，且「时间轴同步」优先级高于「雅 / 形美」——二者不可混用。

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
  `loudnorm` does NOT help. **VAD is now opt-in (off by default)** — the default
  `transcribe`/`run` already calls faster-whisper bare (`vad_filter=False`). To
  force VAD on for clean studio audio, pass **`--vad`** (see V13). For one-off raw
  passes you may still call the faster-whisper API directly (extract audio →
  `WhisperModel.transcribe(vad_filter=False)` → restore timestamps + N offset).
  **Always set `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`** to avoid proxy hits when
  checking the model.
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

> The V8–V13 pit narrative (big-segment drop, in-segment collapse, prefix
> collapse, echo leak, zh/en drift, residual holes, plus the unresolved Oval
> Office overlap-echo item) is in [`docs/POSTMORTEM-JamieFoxx.md`](docs/POSTMORTEM-JamieFoxx.md)
> — this section only covers V7 and earlier.

---

## V8–V13 additions (hardening: no-VAD default, gap-fill, alignment guard, engine-first)

> Code state as of commit `ea77a83` (master). These close the five defect classes
> found on the Jamie Foxx fan-edit (2026-08-10). They are **not optional tuning** —
> they are the default pipeline now.

### V8 — mixed-language audio (auto-detect is asymmetric)
- `language="ja"` over English audio triggers X→ja cross-lingual generation: Whisper
  understands the English sense, then emits fluent Japanese. Katakana = proper-noun
  transliteration shell; hiragana particles + kanji = real translation. Mixed output
  = English sense → Japanese translation, **not** transliteration.
- auto-detect mislabels a mixed clip as the minority language → majority-language
  segments get "translated" into the minority language (source label wrong, but sense
  still passes downstream).
- **Discipline:** default `auto`; only use `resegment --lang ja` / `--lang en` when you
  *know* the source is mixed. Don't wait to see the output — that failure is invisible.
- **Hallucination iron rule:** a segment packing lots of text into a tiny window
  (e.g. 16 chars in 0.64 s) is low-confidence Whisper hallucination — drop it, don't translate.

### V9 — language handling decision (user-ratified)
- **Default `auto`; never force a language** (already the default, no code change needed).
- No per-sentence auto-detection mechanism — over-engineering for a rare case; manual
  `resegment`/`--lang` correction suffices.
- auto→en over "English-dominated + a Japanese line" is the *invisible* failure (the
  Japanese line is silently decoded into fluent English, no subtitle tell). auto→ja is
  *visible* (your own language shows up). Hence default `auto` is safer than forcing `en`.

### V10 — big-segment drop fix (two silence gates)
Root cause on fan-edits (laughter / score / overlapping speech, low SNR): speech is
dropped wholesale.
1. When VAD is enabled, it marks "speech inside laughter/score" as silence and discards
   it — which is why VAD is now **opt-in (off by default)**.
2. Even with VAD off (the default), Whisper's internal `no_speech_threshold` (default 0.6)
   still blanks low-SNR windows.
**Fix:** `NO_SPEECH_THRESHOLD = 0.0` + `TEMPERATURE_FALLBACK=[0.0,0.2,0.4]`. VAD is now
opt-in (off by default), so the default `run` already runs VAD-off; add `--vad` only for
clean studio audio. Clean interview/stand-up clips (high SNR) rarely hit this — **content
type, not video length, decides**. Use `run --no-proxy` for transcribe-only when the model
is cached.

### V11 — fill_gaps audit (`fill_gaps.py`, new module)
Recovers dropped speech missed by the gap scan:
1. **Inter-segment holes:** gap > 8 s between neighbours → extract + force-decode
   (`no_speech_threshold=0.0, temperature=0.0`).
2. **In-segment collapse:** timeline continuous (gap scan blind). Flag when
   `cps = len(text)/dur < median*0.45` and `dur ≥ 4 s`.
3. **Prefix collapse (first-segment lock):** probe pad too large drags a neighbour's
   tail into the window start → decoder emits a fragment then predicts end-of-transcript
   → whole window blanked. Fix: `_PROBE_PADS=(0.2,0.0,0.5)` multi-pad, pick by covered
   duration.
4. **Echo leak:** forced decode over true silence emits a neighbour's tail; skip when
   `difflib.SequenceMatcher > 0.7` matches a neighbour. Only insert holes with *new* narration.
5. **Audit is not one-shot:** re-scan until no new holes.

### V12 — zh/en index drift guard (`verify_align.py`, new module)
Agent writes translations batch-by-batch keyed by index; skipping one line shifts every
later translation by one (`zh[i]` actually = `en[i+1]`). **English track unchanged, so
the pipeline is invisible** — counts match, indices contiguous, no nulls, SRT valid, yet
wrong. v4 reused v3's mis-aligned translations via `(start,text)` key → inherited + amplified.
- **Self-check runs automatically before `generate`** (warning only):
  ① length-profile Pearson correlation sliding window vs shift ±1/±2 (main signal,
     language-agnostic); ② source digits present in `zh[i]` but found in `zh[i±1/±2]` =
     off-by-N fingerprint. Disable with `--no-align-check`.
- **Iron rule A:** batch-by-index translation needs a cross-modal consistency check —
  "three checks pass" ≠ aligned.
- **Iron rule B:** before reusing old translations, first verify the old translations
  themselves were aligned.
- **Fix discipline:** a blind shift re-pollutes (we had inserted 40 segments in v4) —
  re-translate the mis-aligned contiguous range, asserting index coverage (no gaps/dupes).

### V13 — orchestration: engine-first, guard wired in
- **Agent engine is decided first; proxy/Google probe only runs under `--engine google`.**
  With `--engine agent` (default) there is no network/proxy dependency at all — this is
  the design from ADR-005. Don't probe Google just because the binary started.
- The V12 alignment self-check is now **wired into `cmd_generate`** (runs before
  `generate_subtitles`), so drift is caught at render time, not by a human viewer.
- CLI flags: `--vad` (opt-in Silero VAD; default off), `--no-audit`, `--no-align-check`.
  `doctor` stays the preflight entry point.

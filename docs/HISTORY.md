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
- See [`archive/design/translation-design.md`](archive/design/translation-design.md)
  for the principles, and [`archive/V3-STATUS.md`](archive/V3-STATUS.md) for what
  shipped / what's deferred.

## V4 additions (quality, layout, drift-snap, scene context)

- **Beam search (V4):** transcribe now uses `BEAM_SIZE=5, BEST_OF=5` instead of
  greedy — ~3-5× slower (still CPU), much fewer hallucinations.
  `CONDITION_ON_PREVIOUS_TEXT=False` breaks cross-chunk echo loops.
- **Hallucination filter (V4):** `drop_hallucination_segments()` uses two-signal
  detection (word-collapse ≥50% **and** shared 3-gram with neighbour). Safe to
  leave on; part of the default merge pipeline.

## V5 additions (CUDA/Win, tail-echo hallucination guard — ADR-020)

### V5 — tail-echo hallucination guard (ADR-020)
The V4 dual-signal (collapse ≥50% + 3-gram) has a blind spot on **tail-echo**
hallucinations: a phantom segment that re-emits the tail of the previous real
sentence (e.g. real `give me a yogurt either way.` followed by phantom
`I'm not hungry either way.`). faster-whisper's DTW collapses the phantom tokens
onto the neighbour's word boundaries — partly as zero-duration, partly by
*re-using* the neighbour's timestamps (so "either way" overlaps verbatim). That
dilution leaves both V4 signals just short (collapse 40% < 50%; shared n-gram 2
words < 3). Observed 6 such segments in one sitcom episode.

Two new **independent** signals added (merged OR with the existing three):
- **4th signal (deterministic):** a segment whose words overlap a neighbour's
  word intervals ≥50% **and** contain ≥1 zero-duration word is an audio-sharing
  echo — the geometric fingerprint of DTW collapse. No probability threshold, so
  it cannot false-positive on genuine overlapping talk (no zero-duration words).
- **5th signal (Whisper confidence):** low segment `avg_logprob` (gated by
  `no_speech_prob`) carried from `transcribe.py` via `_seg_to_dict` into the
  emitted segments. `avg_logprob` is the reliable half; `no_speech_prob` alone is
  unreliable on laughter/applause (energy present) so it only gates.

`transcribe.py` now carries `avg_logprob` / `no_speech_prob` / `compression_ratio`
from the faster-whisper `Segment` (omitted if absent → backwards compatible;
cache fingerprint auto-invalidates via payload change). Both signals are covered
by unit tests replaying the real sitcom samples.
- **Cache fingerprint (V4):** chunk cache names now include a sha1 of **all
  recipe params** (`{base}.{fp}.chunk_N.json`). Changing any transcribe param
  auto-invalidates old caches — no need to manually clean caches.
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

### V5 — 双轨翻译风格体系 (T3, ADR-027 / Spec 21)
- **三轨 Persona 矩阵** `STYLE_PERSONAS`：`film`（默认，影视二创口语感，等价于历史
  `DEFAULT_PERSONA`）/ `literal`（忠实直译，学术/技术/法律保真优先）/
  `bilingual_study`（双语精读，生僻词括号注记）。
- **`--style {film,literal,bilingual_study}`** 注入 `translate_task.json`（schema v2 → v3，
  新增 `style` 字段）。优先级：CLI `--style` > `VT_STYLE` > toml `[translate].style` > 默认 `film`。
  显式 `--persona` / `VT_PERSONA` 覆盖风格预设人设。
- **双轨输出**：`--style film,literal` 一次生成多份任务文件
  （`<base>.film.translate_task.json` / `<base>.literal.translate_task.json`），Agent 各译一份，
  `generate --style <name>` 产出 `<base>.<style>.bilingual.srt` 等。默认单轨文件名向后兼容。
- **诗歌/歌词**：归入 `film` 轨，靠 `source` 字段引导（不单列 `poetic` 预设）。

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
> Office overlap-echo item) is in [`POSTMORTEM-JamieFoxx.md`](POSTMORTEM-JamieFoxx.md)
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
  `doctor` stays the preflight entry point.
- The V12 alignment self-check is now **wired into `cmd_generate`** (runs before
  `generate_subtitles`), so drift is caught at render time, not by a human viewer.
- CLI flags: `--vad` (opt-in Silero VAD; default off), `--no-audit`, `--no-align-check`.

### V14 — fill_gaps recovery 起点回溯（ADR-036）

修复 `emily-blunt.mp4` 实测暴露的 hard-cut prefix collapse：洞起点落在前一句半句处时，recovery 仅 ±0.5s 的 pad 无法绕开 prefix collapse，只掏回洞尾碎片，洞内对白（W2 821.44–832.74s、W3 894.46–905.76s 各约 10s）全部丢失。

- 扩展 `_PROBE_PADS = (0.2, 0.0, 0.5, 2.0, 4.0, 6.0)`，让 `_probe` 尝试回溯到前一句完整开头。
- 新增 `_coverage_in_hole`：coverage 打分只统计洞内区间 `[gs, ge]`，避免大 pad 拉回的前句尾巴虚高早停（否则洞内对白尚未恢复就误判已覆盖）。
- `_probe_long_hole` 首个 sub-window 前推至 `gs - max_pad` 并用多 pad 解码，超宽洞头部同样避开半句起点。
- 守卫层（`_is_recovered_hallucination` / `_dedupe_seams` / `_is_echo`）全部复用，前句回声由既有守卫拦截，下游 verify / 控制平面无感。
- git 调查确认 ADR-033/034/035 未引入此回归；本案为 v5 起已存在的 recovery 起点设计缺陷（原 `_PROBE_PADS` 假设"洞起点附近即真实起点"）。
- 测试：`tests/test_fill_gaps_prefix_collapse.py`（5 passed）。
- 关联：[ADR-036](adr/036-fill-gaps-prefix-collapse-recovery.md)、[ADR-016](adr/016-recall-recovery-net.md)、[spec 16](specs/16-fill-gaps.md)。

### V15 — 字幕文本单行不变量（ADR-040）+ verify 与 ASR 自证解耦（ADR-041）+ 全大写伪影规范化

两项**行为变更**（2026-09-16），均由实测问题驱动：

**① 字幕文本单行不变量（ADR-040 · Spec 01 / 04）**
- 问题：Agent 写入的 `zh`（以及转写 `text`）可能带内嵌换行，被原样打包进 cue 后与显示层的
  「中英分行」混叠，剪映导入出现异常断行 / 劈裂。
- 修法：内容层（`segments_raw[].text` / `segments[].text` / `zh`）统一为**单行文本**，
  在写入边界（transcribe / translate / generate）用 `text_utils.to_single_line` 压平
  `\r` / `\n` / 连续空白；cue 的多行**只能**来自 `srt_utils.block` 的 `lines`
  （中英分行 / display-merge 折行），不再来自内容本身。
- 契约登记：`artifacts.py` 的 `segments_raw` / `segments` / `zh` 条目注明单行不变量（ADR-040 D4）。
- 延伸（同日落地）：`verify` 内容 lane 增加「内嵌换行」巡检（issue `embedded-linebreak`，
  strict 下计 flag）；task 文件的 `guidelines` 显式声明「译文必须单行」，
  使只读 task 的 Agent 也能看到该契约。

**② verify 与 ASR 自证解耦（ADR-041 · Spec 18）**
- 问题：`verify.find_low_confidence_segments` 用 **ASR 模型自身的评分字段**
  （`no_speech_prob` / `avg_logprob`）巡检 ASR 自己的产物，并**参与 strict gate**
  （不通过即 exit 8）——「用 Whisper 验证 Whisper」，不构成独立验证。
- 修法：**低置信道整体移除**（函数 + `LOW_CONFIDENCE` 常量 + gate 参与权）。
  `merge.py::_low_confidence` 与 `review.py` 信号 A **保留** —— 它们是①层**选手自检**，
  不冒充裁判（裁判只用 FFmpeg 独立参照与客观几何）。
- 影响：原先仅因低置信道红灯的交付物不再被 strict 拦下；声学 lane 的客观几何项不变。

**③ 转写文本：Whisper 全大写伪影规范化（转写层）**
- 问题：高能量 / 喊叫段（`st` 视频后半段实测）Whisper 整段输出**全大写**，读起来像坏字幕，
  也容易被误判为模型或配置故障而反复重跑排查。
- 修法：`transcribe._normalize_caps` —— **整段字母全大写且 ≥4 字母**才触发，小写化并恢复
  句首大写；短 token（`OK`/`ID`/`TV`/`AI`）与混合大小写段一律不动。位置在转写阶段**最末**
  （`asr.run_asr` 内、`apply_merge` + `fill_gaps` 之后——这两步都会重写段文件，早于它们会被
  覆盖）（只改文本，不碰时间戳，ADR-012）。
- 已知边界：短缩写拼成整段且合计 ≥4 字母仍会触发（`AI TV` → `Ai tv`）；单独成段的
  ≥4 字母全大写专名会被小写化（`NASA` → `Nasa`）。权衡后保留，边界已写进 Spec 01 与单测。

- 测试：`tests/test_single_line_text.py`（单行不变量）、`tests/test_verify_hardening.py`
  （低置信道用例已删并注明）、`tests/test_pipeline_field_contract.py`（字段契约）、
  `tests/test_transcribe_contract.py`（全大写伪影单测 + transcribe_video 不越层）、
  `tests/test_asr_facade.py`（run_asr 末端落盘接线）。
- 关联：[ADR-040](adr/040-single-line-subtitle-text.md) / [ADR-041](adr/041-verify-decoupled-from-asr-self-report.md) /
  [Spec 01](specs/01-segment-schema.md) / [Spec 04](specs/04-generate-srt.md) / [Spec 18](specs/18-verify.md)。

### V16 — 接口型 ASR（captions）首轮真实数据实测：两个真 bug

2B 通路（ADR-042 / Spec 29）落地后首次拿真实视频实弹验证
（`https://www.youtube.com/shorts/GxggU7XoCLg`），暴露两个单测没覆盖的缺陷：

**① 「只有自动轨」被误判成「整片无字幕」**
- 现象：`captions --list` 明明列出 `auto en` 一条轨道，`captions <url>` 却报
  `no usable transcript (NoTranscriptFound)`。
- 根因：`_pick_transcript` 按「无匹配返回 `None`」写，但库的
  `find_manually_created_transcript` / `find_generated_transcript` **无匹配时抛
  `NoTranscriptFound`**（见库 `_find_transcript`）。于是最常见的「只有自动轨」在
  第一类轨道上就抛出去，并被 `_guarded` 映射成"整片无字幕"的终态结论。
- 修法：逐类轨道接住异常再试下一类；`_pick_transcript` 不再套 `_guarded`
  —— 它自带错误语义，中间步骤的 `NoTranscriptFound` 不该被当成终态。

**② 滚动轨的**时间侧**：`duration` 越过下一条 `start` → 同片段内第二句被挤成 0.01s**
- 现象：句子化产出出现 `0.01s` 的不可见微段（`{start: 3.36, end: 3.37}`）。
- 根因：YouTube 自动轨的 `duration` 是**显示时长**、会越过下一条的 `start`
  （滚动窗口在**时间**上的表现）。按 `[start, start + duration]` 取窗时，同一条
  片段里的**第二句**被前一句占满窗口，再被「互不重叠」钳制挤成 0.01s。
  **Spec 29 原文记的正是这个错模型。**
- 修法：有效窗口改为 **`[s_k, s_{k+1})`**（`effective_windows`），窗口内按**字符
  长度比例**线性插值（`char_times`）—— 落实 ADR-042 D2 的「成比例」。
- 附带修正：无标点兜底的 `dur > max_dur` 原本是**不可达死代码**（循环条件只由字符
  驱动），改为**字符与时长双上限取先到者**；并补回「未超限 → 整段保留」分支 ——
  改窗口模型时漏掉它，一度把所有正常段静默丢弃、只剩 1 段（同样由实测发现）。

- 实测结果（24s Shorts，auto 轨）：18 条原始片段 → 14 段句子化 → 合并后 13 条 cue；
  零微段、零重叠，时间戳与真实换行点对齐（`2.56` / `3.36` 等）。
- **连带修复（合并层）**：`merge.split_long_cues` 原是**纯词级**切分器 —— 接口型 ASR 的段
  没有 `words[]`，落进「无词 → 整段不拆」的退化路径（Spec 13 原写「理论上不会发生，
  因 Spec 12 已保证」，**ADR-042 后该前提失效**）。叠加合并阶段 `respect_sentence_end`
  又把本层的**句内**兜底切合回去，于是 >42 字符的长 cue 会原样进最终字幕。
  已补**文本级退化路径** `merge._split_wordless`（切点空白 → 标点 → 硬切，不劈开单词；
  时间戳按字符数比例分摊），Spec 13 同步更正。实测同一条视频：超限 cue 由 **4 条
  （最长 95 字符）降到 1 条**；剩下那条是 `merge_short_cues` 为可读性把 0.69s 尾块并回
  所致 —— **词级路径行为完全相同**（既有取舍，非本次引入）。Whisper 路径零影响：
  两条转写路径都写死 `word_timestamps=True`，不存在无词段。
- 文档：Spec 29 步骤 3 改为有效窗口模型（原文为错模型）并说明反面教训；TDD 清单补
  两条回归守卫。
- 测试：`tests/test_ytcaptions.py`（`_pick_transcript` 四分支 + `effective_windows`
  两分支 + 同片段多句不挤成微段 + 比例性）。
- 关联：[ADR-042](adr/042-youtube-captions-as-asr-source.md) / [Spec 29](specs/29-interface-asr-captions.md)。

### V17 — 测试入口漂移守卫：补齐 ADR-029 的覆盖（Spec 23 §2.1）

一项**环境契约**修复（2026-09-17），由实测问题驱动：

- 问题：`uv run pytest` **静默跑到系统 Python 上**。ADR-029 的不变量是「运行环境恒为
  `<repo>/.venv`」，但实测测试进程 `sys.executable == F:\Python311\python.exe` ——
  **正是 ADR-029 / Spec 23 逐字点名要杜绝的那个解释器**。
- 根因（两步叠加，缺一不可）：
  1. `uv run <cmd>` **只对该命令已在 `.venv` 内时**才保证项目环境；找不到就**静默回退
     PATH**（不报错、不警告）。而 `pytest` 属 `[project.optional-dependencies].dev`，
     plain `uv sync`（**不带 `--extra dev`**）会把它从 `.venv` 剪掉 —— 偏偏 R3/E1 把
     plain `uv sync` 定为依赖变更后的**常规操作**，所以这不是偶发，是**必然复发**。
  2. ADR-029 决策 2 的自检（`resolve_command_entry`）只挂在 **CLI 入口**（`doctor`）；
     Spec 23 §1 表格里的**测试**那一行从来没人守。
- 后果：测试跑在**没有项目依赖**的解释器上，却照常报绿/报红。实测同一批用例：
  venv 里过、系统 Python 里 `ModuleNotFoundError`（缺新加依赖），而**两次都"跑完了"**
  —— 结论相反且无人察觉。附带症状：全量耗时从 7.4s 涨到 50s+。
- 修法：
  - `toolchain.require_project_venv(action=...)`：判定为 `bare` 即抛 `EntryDriftError`，
    消息必须含漂移解释器 + 项目 venv + 可执行修复命令（`uv sync --extra dev`）；
    判定复用 `resolve_command_entry()`，不引入第二套逻辑。
  - `tests/conftest.py::pytest_configure` 调用它并转成 `pytest.UsageError` ——
    **测试会话直接不启动**。CLI 侧仍保持 Spec 23 §2 的非致命 `[WARN]`（用户可见即可）；
    **测试侧从严**，因为错误环境下的测试结果没有意义。
- 文档同步：Spec 23 §1 修正「`uv run` 无视 PATH 旧环境」这一**过强表述**
  （仅对已装进 `.venv` 的命令成立），新增 §2.1 契约 + TDD 清单；
  TOOLCHAIN.md §1.3 增补「`uv run` 的前提」与 `uv sync --extra dev`。
- 验证：系统 `pytest.exe` 直跑 → 会话被拦下并打印修复指引；`uv run pytest` → 783 passed。
- 测试：`tests/test_environment_entry.py`（`require_project_venv` 三分支 + 当前进程自身
  必须在项目 venv 内）。
- 关联：[ADR-029](adr/029-command-entry-uv-run.md) / [Spec 23](specs/23-environment-location.md)
  / TOOLCHAIN.md §1.3。

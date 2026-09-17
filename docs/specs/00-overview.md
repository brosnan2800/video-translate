# Spec 00 — Overview & Data Flow

## Purpose
`video-translate` turns a video into bilingual (Chinese/English) subtitles that
import cleanly into Jianying (剪映). It is a faithful, testable migration of a
previously validated ad-hoc pipeline.

## Design invariant (the one rule everything serves)
> **Timestamps are carried unchanged, never recomputed** — but they are *not*
> acoustic facts.
> faster-whisper's `start`/`end` (word- and segment-level) are DTW *posterior
> estimates*: they drift and collapse, they are not ground truth. Therefore:
> - Segment boundaries are anchored to **VAD / silence** where possible
>   (content-type routing, ADR-011);
> - Alignment must be cross-checked against an **independent reference**
>   (`ffmpeg silencedetect` measured silence), never self-asserted by whisper;
> - True acoustic repair (forced alignment, 96% vs word-level 82%) uses
>   WhisperX wav2vec2 and is **already landed** (T4 / [ADR-028](../adr/028-whisperx-alignment-pass.md),
>   default `--align auto` on GPU); the Mac path only *detects + routes*
>   (falls back to `none`), it does not *repair*.
> Subtitle correctness has three orthogonal layers — **acoustic / content /
> presentation** (Spec 18) — and must not be collapsed into one.
> See ADR-012.

## Three-stage pipeline

> 全部产物统一收进 `videos/{base}/`（ADR-037）：中间产物、缓存、状态链、最终字幕四件套均在
> 该子目录下，外层 `videos/` 只保留源视频本体。

```
video (mp4/…)
   │  stage 1: transcribe  (faster-whisper large-v3, CPU/int8, chunked+resumable)
   ▼
videos/{base}/{base}.segments_en.json         list[{start, end, text}]
   │  stage 2: translate   (Google Translate via HTTP proxy, incremental)
   ▼
videos/{base}/{base}.zh_segments.json         {str_index: zh_text}
   │  stage 3: generate    (pure function, byte-stable)
   ▼
videos/{base}/{base}.bilingual.srt   {base}.zh.srt   {base}.en.srt   {base}.txt
```

Intermediate per-chunk artifacts: `videos/{base}/chunk_0.json`, `chunk_1.json`, … (stage 1
resume state). Failure artifact: `videos/{base}/{base}.agent_pending.json` (segments Google
could not translate, for agent backfill).

## Stage responsibilities
| Stage | Module | Deterministic? | Golden-testable? |
|---|---|---|---|
| transcribe | `transcribe.py` + `ffmpeg_utils.py` | No (model, timing) | Contract only |
| translate | `translate.py` + `proxy.py` | No (network) | Contract only |
| generate | `generate.py` + `srt_utils.py` | **Yes (pure)** | **Byte-exact** |

## Scope
- **v1 (locked at tag `v1.0.0`)**: faithful migration of transcribe → translate →
  generate; CLI; tests; docs. **Golden fixtures 已停止仓库跟踪**：`docs/golden/`
  缺失时相关用例自动 skip，不构成失败（见 MAJOR_VERSION_PLAN §0.2 铁律 6）。
- **v2 (tag `v2.0.0`)**: segment-merge layer (Spec 08); agent-as-engine
  translation (Spec 09, default `--engine agent`, no LLM client dep); `backfill`
  subcommand (Spec 10); CLI/UX overhaul — zero-config positional input, auto
  base/outdir/lang/proxy (Spec 11, ADR-006/007). V1 invariants preserved:
  timestamps never recomputed, SOCKS rejected, CPU/int8 forced.

## Related specs
- Segment schema: `01-segment-schema.md`
- Per-stage detail: `02-transcribe.md`, `03-translate.md`, `04-generate-srt.md`
- Stage-1 internals: `08-segment-merge.md` · `12-word-level-timestamps.md` · `13-cue-splitting.md`
- Translation: `09-agent-translate.md` · `10-backfill.md` · `14-glossary.md`
- Config / CLI: `06-config.md` · `11-cli-v2.md`
- Timing: `15-timing-silence.md`
- Gotchas: `07-gotchas.md`（**红线主源见 `AGENTS.md` §1，本表为开发者视角补充**）
- Hardening (V7–V13): `16-fill-gaps.md` (V11 coverage audit) · `17-verify-align.md` (V12 zh/en index-drift guard) · `18-verify.md` (unified self-check: acoustic/content/presentation lanes, ADR-012)
- V5 additions (T2/T3/T4 + E1–E4): `19-vocal-separation.md` (T2 demucs) · `20-env-readiness.md` (E1–E4 doctor) · `21-translation-styles.md` (T3 style tracks) · `22-whisperx-alignment.md` (T4 forced alignment) · `23-environment-location.md` (`uv run` entry)
- Entry point (T8): `24-pipeline-behavior.md`（`pipeline` 幂等推进器，[ADR-033](../adr/033-control-plane-pipeline-entry.md)）
- Command hygiene: `25-cli-path-hygiene.md`（中文路径等参数边界校验，入口 exit 2）
- Presentation: `26-display-merge.md`（显示层短块合并，不改内容层）
- ASR layer extraction (T11): `27-asr-provider.md`（接口契约 · 第一步）· `28-asr-facade-and-readiness.md`（① 层门面 + Provider 自报就绪 · 第二步）
- Interface-based ASR (plan §2B): `29-interface-asr-captions.md`（`captions` 取平台现成字幕 → 与转写同契约产出，直接接 P2）

> **完整索引见 [`docs/index.md`](../index.md)**。Spec 05 已删除（说明见 `11-cli-v2.md`）。
> Spec 00–21 为行为契约、随代码演进，不设 Accepted/Superseded 状态；Spec 22 起因对应
> 明确里程碑而带状态字段。

## 最近变更（2026-09 汇总）

> 便于回看「最近这波改了什么」。逐条完整叙述见 [`HISTORY.md`](../HISTORY.md)（V15–V18）；
> 每条都给出它的**决策记录 / 实现契约**，改动细节请查那里，本表不复述。

| 日期 | 变更 | 决策 / 契约 | 影响面 |
|---|---|---|---|
| 09-17 | **入口按输入形态自动判定**：`pipeline "<YouTube 链接>"` 自动走接口型 ASR；非油管 URL → `exit 2` + 指引 | [ADR-043](../adr/043-input-form-auto-routing.md) · [Spec 24 §6](24-pipeline-behavior.md) | 分发层（`cmd_pipeline`）+ `ytcaptions` 判定 + ctx 来源注入；**本地路径行为不变**；附带修 Spec 25 的 URL 卫生豁免 |
| 09-17 | 接口型 ASR **首轮真实数据实测**修掉两处：库的 `find_*` **抛异常**语义（`--list` 有轨、`fetch` 说无字幕）、滚动片的**时间侧**（`duration` 是显示时长 → 同片段第二句被挤成 0.01s 微段） | [Spec 29](29-interface-asr-captions.md) | `ytcaptions` 内部算法；**产物契约不变** |
| 09-17 | `merge.split_long_cues` 补**无词段**文本级切分 —— Spec 13 原写「无 `words` 理论上不会发生」，该前提被 ADR-042 推翻 | [Spec 13](13-cue-splitting.md) | 合并层；Whisper 路径零影响（两条转写路径均写死 `word_timestamps=True`）；超限 cue 由 4 条降到 1 条 |
| 09-17 | **测试入口漂移守卫**：`uv run pytest` 曾在系统 Python 上跑（plain `uv sync` 剪掉 dev extra → `uv run` **静默回退 PATH**），测试结论来自错误环境 | [Spec 23 §2.1](23-environment-location.md) · [ADR-029](../adr/029-command-entry-uv-run.md) | `toolchain.require_project_venv` + `tests/conftest.py` 硬拦（**非项目 venv 拒绝启动会话**） |
| 09-17 | 转写文本：Whisper **全大写伪影**规范化，落在**转写阶段最末**（`asr.run_asr`，覆盖 `apply_merge` / `fill_gaps` / `resegment` 的重写路径） | [Spec 01](01-segment-schema.md) | 文本层；**不碰时间戳**（ADR-012） |
| 09-16 | 字幕文本**单行不变量** + `verify` 与 ASR **自证解耦**（低置信道整体移除） | [ADR-040](../adr/040-single-line-subtitle-text.md) · [ADR-041](../adr/041-verify-decoupled-from-asr-self-report.md) · [Spec 01](01-segment-schema.md) / [Spec 04](04-generate-srt.md) / [Spec 18](18-verify.md) | 内容层文本 / verify lane 构成 |
| 09-11 ~ 09-17 | **ASR 层抽离两步落地**：`ASRProvider` 接口 → `run_asr` 门面 + **Provider 自报就绪**接线到 doctor / 闸门；「第二个 Provider」由接口型 ASR 验证 | [ADR-038](../adr/038-asr-layer-extraction.md) · [Spec 27](27-asr-provider.md) · [Spec 28](28-asr-facade-and-readiness.md) | ① 层可插拔；产物契约不变（[ADR-035](../adr/035-pipeline-data-contract.md) 仍唯一事实来源） |

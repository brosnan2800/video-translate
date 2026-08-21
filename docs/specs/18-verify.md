# Spec 18 — 统一自检门 `verify`（声学 / 内容 / 表现 三 lane）

Module: `verify.py` + `cmd_verify`. Decision ADR-012. 补充 [Spec 17](17-verify-align.md)
（仅内容层索引漂移）与 `validate_zh`（仅覆盖度）；本 spec 把分散的自检收成一道
**分层、按层报错** 的统一门。

## Problem
字幕正确性 = 三个**正交**维度，此前被揉成一团，导致「哪层错、该查什么」永远模糊：

| 维度 | 问句 | 本次 IF 的 bug | 现状 |
|---|---|---|---|
| 声学层 | cue 的 [start,end] 是否压在真语音上？ | V1 漂移、片头 Hubsan、首句早 0.4s | **无**独立校验，只信 whisper 漂移的 DTW 自证 |
| 内容层 | zh[i] 是否忠实 en[i]？ | 错译静默通过 | 仅 `validate_zh`(覆盖) + `verify_align`(索引) |
| 表现层 | 字幕何时出/收符合感知？ | V2 早出现/早退 | `offset/tail` 事后遮丑，且被人为收紧 |

`verify_align` 名字像「时间轴对齐」实际查内容层索引漂移 → 所有人误以为时间轴被查过。
`fill_gaps` 在声学层工作却会强制解码静音、反手注入内容层幻觉。职责不清是「不对劲」
的结构性来源（ADR-012 诊断）。

## Algorithm
`verify` 读 `segments_en.json`（含 cue 时间轴）+ `zh_segments.json` + 视频文件，跑三 lane，
**按层独立报错**，互不阻塞。复用 `audio_profile`（独立声学参照）。

### Lane 1 — 声学（acoustic）
- 调 `audio_profile` 取 `silencedetect` 静音区间。
- 逐 cue 比对：
  - **落在静音**：cue 的 [start,end] 整体落在某静音区间内 → flag（疑似空白/幻觉段）。
  - **跨静音**：cue 跨越静音边界且中段为静音 → flag（段边界漂移，如 IF V1）。
  - **首句提前**：首 cue `start` < 第一个静音区间 `end`（即字幕早于真实开口）→ flag。
- **漏检探测（uncovered-audio，ADR-016 / T2b）**：调用
  `find_uncovered_speech(segments, silences, duration, min_dur=2.0)` —— 先求所有
  cue 的并集覆盖，取补集得到「无字幕区间」，再剔除其中落在静音的部分，剩下的
  「有声音却无字幕、且 ≥ `min_dur`」区间即 `UNCOVERED_AUDIO` 报警。这是「从头到尾
  再捞一遍」的**检测**侧：它只 flag，不重解码（重解码归 `fill_gaps` / `resegment`）。
  以前一个 region 真被漏掉时根本没有 cue 可查，三 lane 会误报 clean；此 lane 补上该漏洞。
- 输出每条可疑 cue / 漏检区间的时间戳与类型，供人工/agent 校正。

### Lane 2 — 内容（content）
- 跑 `validate_zh`：覆盖度（每个 segment index 都有 zh，无漏译行）。
- 跑 `verify_align`（`Spec 17`）：索引漂移（off-by-N，长度相关性 + 数字共现）。
- **中英混杂检测（ADR-016/V14）**：`find_untranslated_latin_words(zh[i])` 揪出译文里
  残留的**小写拉丁实词**（如 `rivalry` 未译）——覆盖度 / 索引漂移都抓不到这类夹生，
  确定性兜底（不依赖 agent 肉眼回读）。全大写（`OK`/`AI`）与首字母大写专名
  （`Ken`/`Barbenheimer`）不 flag；误报（`app`/`rap` 等外来词）仅报警交 agent 确认。
- **语义回读（默认开）**：`build_semantic_reread_task` 逐条列出 en+zh **带中英文邻居
  上下文**（`context_before/after` + `context_before_zh/after_zh`，`window=3`），由
  agent 结合上下文判断「单句看着通、放上下文里指代/语气/术语是否一致」。这是唯一
  能抓语义层错误的 lane，且它**默认开启**（`--no-semantic` 关闭以省 token）。

### Lane 3 — 表现（presentation）
- 读 `generate` 所用参数（`--tail` / `--min-dur` / `--offset`）：
  - `tail == 0` 或 `min_dur == 0` → **warning**（削掉默认感知留白，易致早退/一闪而过）。
  - 首 cue `start - offset`（显示起点）< 声学静音边界 → warning（字幕早于开口）。
- 这一 lane 防「V2 式人为收紧」回归，不依赖人耳发现。

## Invariant（load-bearing）
- **独立参照优先**：声学 lane 只用 `silencedetect`（ffmpeg 自带）作真相源，不引用
  whisper 自身时间戳自证（ADR-012 核心）。
- **只检测不重写**：三 lane 均只 flag，不修改 `segments_en.json` / `zh_segments.json`
  / SRT，校正留给人工/agent（与 Spec 17 一致）。
- **非致命**：任一 lane 报警只打印到 stderr / 报告文件，`verify` 退出码 0（可按
  `--strict` 改为非零，供 CI）。
- **零新重依赖**：`silencedetect` / `volumedetect` 均为 ffmpeg 滤镜；语义回读不引 LLM。

## Wiring
`cli.cmd_verify`：
- 入参：`--segments`、`--zh`、`--video`、`--out`（报告路径，默认 stdout）、
  `--strict`（报警转非零退出）、`--no-semantic`（关闭语义回读，省 token；
  语义回读**默认开**）。
- 顺序：声学（需 video）→ 内容（需 segments+zh）→ 表现（需生成参数，可选）。
- 缺 `--video` 时声学 lane 自动跳过并提示。
- `make test` 覆盖纯函数：`check_cue_in_silence`、`check_cue_cross_silence`、
  `check_first_cue_early`、`find_uncovered_speech`（漏检探测）、
  `find_untranslated_latin_words`（中英混杂）、`build_semantic_reread_task`
  （中文邻居上下文），用合成静音区间 + cue 序列 fixture。

## Defaults
| Param | Default | Notes |
|---|---|---|
| `noise` | `-30dB` | `silencedetect` 噪声门限，与 `audio_profile` 共用 |
| `d` | `0.3` | 静音最小持续（s） |
| `--strict` | off | 报警转非零退出（CI 用） |
| `--no-semantic` | off | 语义回读**默认开**；显式 `--no-semantic` 关闭以省 token（吐任务本身不耗 token，耗的是 agent 回读） |

## Golden
- 单元：合成静音区间 + cue 序列（正常 / 落在静音 / 跨静音 / 首句提前）fixture，
  断言各 lane flag 正确；无字节级 golden（纯分析，无文件输出）。
- 集成：在 IF（`videos/IF.segments_en.json` 校正版）上跑 `verify --video videos/IF.mp4`，
  断言声学 lane 干净（漂移已根治）、表现 lane 在默认 `tail 0.3` 下无 warning。

# ADR-020 — 尾部回音幻觉防御（确定性时间嵌套信号 + Whisper 置信度信号）

- 状态：接受
- 日期：2026-08-21
- 关联：ADR-012（声学时间戳真相 / 静音窗信号）、Spec 12（词级时间戳）、Spec 02（transcribe）、Spec 18（verify 三 lane）、`merge.py` `drop_hallucination_segments`、`transcribe.py` `_seg_to_dict`

## 背景

V4 的 `drop_hallucination_segments` 用**双信号**（word 塌缩率 ≥50% + 与邻居共享 ≥3 词连续 n-gram）拦截无声学支撑的幻觉段；ADR-012 又补了**第三信号**（整段落在 `silencedetect` 静音窗）。三者在 sitcom《Everybody Loves Raymond》实战中暴露盲区：

实战中 57s 附近出现 `I'm not hungry either way.` 紧接真实句 `give me a yogurt either way.`：
- word 级时间戳证明它是幻觉：前缀 `I'm`/`not`/`hungry` 零时长压在 54.22-54.26；尾部 `either way` 与真实句**逐字精确重叠**（DTW 把幻觉得推到邻居词边界上）。
- 但双信号**双双差一点**：塌缩率仅 3/5=40%（<50%，因为 `either way` 借到真实音频、不零时长）；共享 n-gram 仅 `either way` = 2 词（<3）；静音窗信号也不触发（笑声有能量）。
- 后果：过滤器放行幻觉段 → SRT 生成器把真实句截短成 0.64s（一闪而过），幻觉句插在后面。

全片用 whisper 窗口重转写做声学真值确认，共筛出 **6 条同类尾部回音幻觉**（15/18/338/368/394/398），全部是「复述上一句的尾部 + 与前驱共享音频」。这是 Whisper 自回归解码的固有缺陷（窗口无清晰语音时「脑补」最像话的文本，并延续最近上下文把刚说的复述一遍），不是本项目特有 bug。

候选方案：
- **(A) 维持双信号 + 手工救火**：每次靠 agent 对照 whisper 重转写补刀。代价：重复、易漏、不可扩展。
- **(B) 升级过滤器为确定性 + 置信度双冗余（本 ADR）**：把人工诊断用的两个判据固化为自动信号——（4）word 级共享音频（确定性几何指纹）；（5）Whisper 自带 `avg_logprob`/`no_speech_prob`/`compression_ratio`（转写层先保留字段）。

## 决策

选 **(B)**，在 `merge.py` 的 `drop_hallucination_segments` 新增第四、第五信号：

1. **第四信号（时间嵌套，确定性）**：段 B 的**整个时间窗口被邻居段 A 的时间窗口包含**（B 骑在真实语音上，DTW 把回声词压到 A 的音频上），**且** B 内存在 ≥1 个零时长词 → 判定为回音幻觉。
   - 确定性：纯时间戳几何判定（区间包含 + 零时长词），不依赖任何概率阈值。
   - 区分边界模糊：真实相邻段只会边界交叉（后段 start 略早于前段 end），但**不会整体嵌套进邻居窗口**；本信号因此不会误杀正常相邻说话。最初草案用「词级重叠比例 ≥0.5」会误杀边界模糊的相邻段（如 `I like it` 首词区间被后驱 `like it with pizza` 的 "like" 锚点覆盖），改为区间嵌套后消除该误杀。
   - 复现：本次 6 条嵌套回声（39/101/162/339/341/433）100% 命中，并经验证窗口重转写确认无独立语音。
2. **第五信号（低置信度）**：Whisper 段级 `avg_logprob` < `avg_logprob_thr`（默认 -1.0）且（`no_speech_prob` 缺失或 ≥ `no_speech_thr` 默认 0.6）→ 判定为幻觉。
   - `avg_logprob` 是可靠半边：复述型幻觉在无声学支撑下输出文本，token log-prob 显著低于真实语音。
   - `no_speech_prob` 在笑声/掌声场景不一定高（有能量），故仅作 gate，避免误杀。
   - 字段由 `transcribe.py` 的 `_seg_to_dict` 从 faster-whisper Segment 携带进 `segments_en.json`（向后兼容：缺失字段时信号自动 inert）。

两个新信号与原有三信号**独立并存**（任一命中即丢弃），且不改动时间戳（遵守 ADR-012 不变量：只删除、不重算）。

## 理由

- **根因是确定性的**：尾部回音的 DTW 塌陷本质是「幻觉得推到邻居音频上」，这是一个可几何验证的事实，比任何概率阈值更稳。
- **置信度字段是免费午餐**：faster-whisper Segment 自带 `avg_logprob`/`no_speech_prob`/`compression_ratio`，原转写层丢弃是浪费；携带后过滤器无需重解码即可用 Whisper 自己的判据。
- **误伤可控**：第四信号要求「共享音频 + 零时长词」合取，真实叠音/相邻说话无零时长词，必不触发；第五信号阈值保守（avg_logprob -1.0 远低于真实语音常见 -0.3~-0.6）。
- **可回归**：本次 6 条真实样本已固化为单元测试（`test_acoustic_verify.py` 的 `test_drop_hallucination_audio_sharing_echo` 等）。

## 后果

- **`src/video_translate/merge.py`**：
  - `drop_hallucination_segments` 新增参数 `nested_eps=0.1`、`avg_logprob_thr=-1.0`、`no_speech_thr=0.6`。
  - 新增辅助函数 `_is_time_nested`、`_has_zero_dur`、`_low_confidence`。
- **`src/video_translate/transcribe.py`**：
  - 新增 `_seg_to_dict(s, offset)`，从 faster-whisper `Segment` 抽取 segment dict 并携带 `avg_logprob`/`no_speech_prob`/`compression_ratio`（缺失则省略，向后兼容）。
  - `transcribe_video` 与 `transcribe_window` 均改用 `_seg_to_dict`，缓存指纹因 payload 改变而自动失效（安全）。
- **`tests/test_acoustic_verify.py`**：新增第四/五信号单测（含真实样本回放与无零时长词不误伤）。
- **`tests/test_transcribe_contract.py`**：新增 `test_transcribe_carries_confidence_fields` 验证字段序列化。
- **AGENTS.md / Spec 文档**：更新幻觉拦截描述，纳入第四、第五信号。

## 已知限制

- 第四信号要求段含 word 时间戳且能判定窗口嵌套；若某段缺 words 或窗口不嵌套邻居，信号 inert（不会误杀，但也不会拦截该回音）。
- 第五信号的 `avg_logprob` 阈值依赖模型规模/语言；large-v3 实测 -1.0 安全，换小模型可能需要放宽。
- 多说话人同时发言（叠音）且 Whisper 拆成两段、两段均含零时长词且一段整体嵌套进另一段时，第四信号理论上可能误杀——但影视场景 Whisper 通常会把叠音合并进单段，实战未观测到该误杀。如未来出现，可加「文本不相似则不触发」的二次 gate。

## 追加修订（2026-09-03）：信号 5b（高 nsp 连贯非语音）+ 信号 6（重叠近重复）

实战复现两类原第五信号漏网的幻觉（见 `video-translate` 仓库 `videos/` 下
Walken 单口视频，`segments_en.json` 事故几何）：

1. **连贯非语音幻觉**：`~39s` 出现挪威语 `Takk for at du så på.`（"感谢收看"），
   `no_speech_prob=0.779`（Whisper 78% 判定非语音）、`avg_logprob=-0.621`。原第五信号
   要 `avg_logprob < -1.0` 才丢，这条 -0.621 不到；而 `no_speech_prob` 被设计**故意压在
   low-alp 之后**（避免笑声/掌声 bed 误杀），于是高 nsp 连贯幻觉漏网。
2. **临界带低置信填充句**：`~2:48` "they have a cake." 后跟 "I don't know." /
   "I just think."，`avg_logprob=-0.969`（刚好高于 -1.0）、`nsp=0.252`。漏网。
3. **重叠/重复段**（用户"同时间段多个字幕重叠"的直觉对应）：全片 16 对相邻段时间重叠，
   其中 `#46→#47`（"…i'm in all alone." ‖ "You know, i'm all alone."，0.2s 重叠）紧邻 2:48；
   另有 `#96→#97`、`#211→#212` 等整句重复。原守卫第四信号只查 time-nested、不查
   time-overlap，纯重复不塌缩的漏掉。

### 决策

- **信号 5 修订为两条尾**：
  - (a) `avg_logprob < avg_logprob_thr`（默认 **-0.95**）——极低 alp 独立判删。
    阈值**不能**取 -0.85：该视频真实低置信语音（"Or champagne." -0.894、
    "Ah, thank you very much." -0.895、"I'm out." -0.911、"I'm a f***ing..." -0.933）
    均带低 nsp 且为真实语句，会误删；真实语音 alp 下限约 -0.93，与幻觉 -0.969 之间
    的间隙定在 -0.95。
  - (b) **高 nsp 连贯非语音**（新增独立信号）：`no_speech_prob >= no_speech_thr`(0.6)
    **且** `avg_logprob < -0.4` → 判删。覆盖 #1。高 nsp 在笑声/掌声 bed 表现为**低**值，
    故高 nsp gate 不误杀真说话；`-0.4` 伴随闸挡住 F3 fill-gaps 恢复段（nsp=0.892 但
    alp=-0.235，真实）。
- **信号 6（重叠近重复，新增）**：段 B 起点早于前驱 A 终点（`overlap > overlap_eps`=0.15s，
  排除 <~0.1s 边界模糊）**且** 与前驱文本 Jaccard ≥ `overlap_jaccard_thr`(0.3) → 判删 B。
  安全闸：纯续句（"…soy milk." / "milk. this is tragic…" Jaccard 0.08）不删，避免丢内容；
  **链式中段**（B 同时重叠其前驱与后继，拥有独立内容）保留，回声尾由其后一次迭代删。
  覆盖 #3（#46→#47 等）。

### 后果

- `merge.py` `drop_hallucination_segments`：`avg_logprob_thr` 默认 -1.0→**-0.95**；
  新增参数 `overlap_eps=0.15`、`overlap_jaccard_thr=0.3`；新增辅助 `_jaccard`、`_is_overlap_duplicate`。
- `tests/test_merge.py`：新增 7 个事故几何回归用例（#11/#53/#54 删除；真实低置信
  `#56/#464/#471/#473` 与 F3 恢复 `#294-296` 保留；overlap 删回声/留续句/留链式中段）。
- 已知限制更新：信号 6 的 Jaccard 闸对**改写式**回声（如 #46→#47 改写 "all alone"，
  Jaccard 恰 0.30）临界，低于 0.3 的改写回声不拦截；链式中段保留策略会少删尾部 1~2 词
  （如 "beautifully"），属可接受的轻微内容保留，优先保证不丢真实内容。

---

## 补遗二（2026-09-03，回归修复）：信号 5b 加静音窗闸 + 信号 6 回退

> 本节 **supersede 上一节的信号 6 决策**，并修正信号 5b。
> 事故来源：全量 `uv run pytest` 跑出 **4 red**
> （`tests/test_pipeline_field_contract.py` 3 个 +
> `tests/test_acoustic_verify.py::test_boundary_blur_not_false_positive` 1 个）。

上一节的 5b 与信号 6 均**误杀真实语音**，两类根因不同，处置也不同。

### 一、信号 5b：高 nsp 必须配合静音窗（修正，非回退）

**误杀**：`tests/test_pipeline_field_contract.py::merged_chain` 的
`'muffled words under laughter'`（nsp=0.72 / alp=-0.65）——**笑声掩盖下的真实语音**
被判删。该 fixture 注释白纸黑字写明契约：「merge 幻觉过滤器**不丢**（alp 未低于 -1.0）」。

**根因**：5b 假设「高 nsp ⇒ 非语音」，但**笑声 / 欢呼 bed 上的真实语音 nsp 同样偏高**
（背景能量使模型自判非语音）。实测两组数值几乎重叠：

| 样本 | no_speech_prob | avg_logprob | 真值 |
|---|---|---|---|
| 挪威语幻影 `Takk for at du så på.`（~39s，应删） | 0.779 | -0.621 | 幻觉 |
| `muffled words under laughter`（应留） | 0.720 | -0.650 | **真实语音** |

仅靠 `(alp, nsp)` 二维**无法区分**；二者唯一的物理差异是**窗内有无声学能量**：
幻影整段落在 `silencedetect` 静音窗内，笑声下的真音不在。

**修正**：5b 追加静音窗闸 —— `no_speech_prob >= no_speech_thr(0.6)` **且**
`avg_logprob < -0.4` **且**该段整段落在已探测的静音窗内，才判删。
未提供 `silence_intervals` 时 5b **惰性（inert）**。

这与 ADR-034「默认裸跑 + 转写后 G1/G2/G3 自动救回」一致：笑声掩盖的真音应由
**恢复链路救回**，而不是在 merge 里先删了再说。

### 二、信号 6：整体回退（supersede 上一节）

**误杀**：`tests/test_acoustic_verify.py::test_boundary_blur_not_false_positive`
—— `I like it`(136.92–137.24) ‖ `like it with pizza`(137.06–137.82) 这类
**Whisper 常见的真实重叠连续语音**被判删。

**实测几何对比**（`_tokens` + Jaccard 实测，非估算）：

| 样本 | Jaccard | 后段新增占比 | 后段被前段覆盖率 | 后段⊆前段 | 重叠 |
|---|---|---|---|---|---|
| 回声（应删）`…in all alone.` → `You know, i'm all alone.` | 0.30 | 0.40 | 0.60 | False | 0.20s |
| 真音（应留）`I like it` → `like it with pizza` | **0.40** | 0.50 | 0.50 | False | 0.18s |

三条结论：

1. **真音的 Jaccard（0.40）比回声（0.30）更高** —— 「Jaccard 越高越像回声」的
   判据方向**本身是反的**；收紧阈值只会先放过回声、再误杀真音。
2. `后段 ⊆ 前段` 两组**皆为 False**（回声新增了 `you` / `know`）→ 严格子集判据会漏掉回声。
3. 唯一方向正确的维度是「后段被前段覆盖率」（0.60 vs 0.50），但 **gap 仅 0.10**，
   本质是阈值调参，换不来可靠边界。

**决策**：**移除信号 6** 及 `_jaccard` / `_is_overlap_duplicate` 与
`overlap_eps` / `overlap_jaccard_thr` 两个参数、3 个阈值用例。

理由：正负样本几何不可区分时，任何阈值都只是用漏判换误判，而此处**误判代价极不对称**——
漏一个回声只是多一句可疑字幕（verify 声学 lane 会报出，人工可见、可修）；
误杀一句真音则是**字幕静默漏句**（用户完全无感，且直接违反 ADR-034 对笑声下真音的保护）。
确定性回声仍由**第四信号**（窗口被邻居嵌套 + 零时长词）覆盖：不依赖阈值、不误杀。

### 后果

- `merge.py`：
  - `_low_confidence` 新增 `silence_intervals` 入参，5b 分支加静音窗闸；
  - 删除 `_jaccard` / `_is_overlap_duplicate` 与 `overlap_eps` / `overlap_jaccard_thr`；
  - `drop_hallucination_segments` 向 `_low_confidence` 透传 `silence_intervals`。
- `tests/test_merge.py`：
  - `test_hallucination_high_nsp_non_speech_dropped` 改为传入静音窗几何（37.0–39.6s）；
  - **新增** `test_hallucination_high_nsp_laughter_speech_kept` —— 本次误杀样本
    `'muffled words under laughter'`（nsp=0.72 / alp=-0.65）的回归守卫（S3）；
  - 删除信号 6 的 3 个用例，并就地注明回退理由与实测数据。
- 回归：全量 `uv run pytest` → **628 passed / 12 skipped**（修复前 4 red）。
- 教训：**改幻觉阈值必须跑全量测试，不能只跑新增用例文件。** 本次即因只跑了
  `test_merge.py` 而漏掉跨文件契约（`test_pipeline_field_contract.py`）。



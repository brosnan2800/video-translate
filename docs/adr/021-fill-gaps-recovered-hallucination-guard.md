# ADR-021 — fill_gaps 恢复段幻觉得防（ADR-020 补遗）

- 状态：接受
- 日期：2026-08-22
- 关联：ADR-020（尾部回音幻觉防御）、ADR-016（漏音补洞 / T2 recall）、`fill_gaps.py` `_is_recovered_hallucination`、`merge.py` `drop_hallucination_segments`、`tests/test_fill_gaps_recovered_guard.py`

## 背景

ADR-020 在 `merge.py` 的 `drop_hallucination_segments` 新增第四（时间嵌套+零时长词）、第五（低 `avg_logprob`）信号，专门拦「尾部回音」类幻觉。但 jimmy.mp4 实战暴露一个结构性盲区：

`drop_hallucination_segments` 在 `merge.py` 的转写后流水线里、于 `fill_gaps` **之前**运行，只处理 Whisper 原产段。而 `fill_gaps` 的漏音补洞流程会用 `no_speech_threshold=0` 强制重解码时间洞，恢复出的 `_recovered` 段**只经过文本相似度的 `_is_echo` 检查**，时间戳几何完全不看。

结果：jimmy 视频里 7 条 `fill_gaps` 补洞脑补回音溜进最终字幕：

| 时间 | 文本 | 几何特征 |
|---|---|---|
| 13.40-13.56 | `Don't worry.` | 3 词/0.16s=18.8wps；窗口整体嵌在邻居 [12.38,13.60] 内 |
| 55.87-56.61 | `I'm fucking fired!` | 与前段 `motherfucking…` 尾部重叠 0.5s |
| 111.54-113.04 | `Thank you.`（实为 `that's`，听错型） | 孤立；内容错误，无几何指纹 |
| 137.45-138.93 | `Субтитры…`（俄语水印脑补） | 与后段 [138.75,…] 重叠 0.18s |
| 193.49-193.67 | `I'm a clown.` | 3 词/0.18s=16.7wps；含 2 个零时长词；嵌在邻居内 |
| 254.38-254.66 | `Hi, son.` | 与 `Awesome.` [254.06,254.58] 重叠 0.2s |
| 283.36-284.12 | `Now what?` | 与前段尾部重叠 0.2s |
| 377.28-377.78 | `This is bad.`（实为 `he's back`，听错型） | 与 `…is back` 整体重叠 0.5s |

共同指纹：**恢复段与已确认段窗口实质重叠（骑在真实音频上）** 或 **语速物理不可能**。这正是 ADR-020 第四信号的判据，但作用在 `fill_gaps` 之后，恢复段根本没机会过它。

## 决策

在 `fill_gaps.py` 新增模块级函数 `_is_recovered_hallucination`，并在 `_decode_once` 内、紧接 `_is_echo` 之后调用（不替换 `_is_echo`，叠加为更严格的第二道关）。恢复段 dict 同时携带 `avg_logprob`/`no_speech_prob`/`compression_ratio`（仿 `transcribe.py` 的 `_seg_to_dict`，由 faster-whisper Segment 自带，向后兼容：缺则省略）。

三类信号（任一命中即丢弃，全保守防误杀真实补洞语音）：

1. **信号 A（重叠，核心）**：恢复段**词数 < `max_words_for_overlap`（默认 4）**且与任一现有段窗口重叠 > `overlap_eps`（默认 0.12s）→ 判为骑在已确认音频上的回音。
   - 依据：fill_gaps 的职责是填洞（洞即「现有段之间的空白」），短恢复段窗口与现有段重叠 = 解码到了已确认音频。jimmy 数据：7 个短幻觉段与邻居的**绝对重叠量**为 0.16/0.5/0.18/0.18/0.2/0.2/0.5s（最小 0.16s）；它们词数均 ≤3。
   - **长段豁免**：真实相邻段边界模糊（ADR-020 已知），如 `anxious. There's a difference.`（4 词）与前段 `...you get anxi[ous]` 边界重叠 0.20s 却为真实续接。仅当 `词数 < max_words_for_overlap` 才启用重叠信号，长句（≥4 词）豁免，改由信号 B/C 兜底。jimmy 的真实长段（`And the vocals…`、`anxious.…`）全部豁免保留；`Субтитры…`（3 词）仍被拦（确为脑补水印）。
   - **为何用绝对重叠量而非比例**：原草案用「重叠占自身窗口比例 >40%」，但 `Now what?`（0.76s 窗口只重叠 0.2s=26%）和 `Субтитры`（1.48s 重叠 0.18s=12%）会漏判。绝对重叠量稳定可靠。
2. **信号 B（语速）**：`词数 >= min_words`(2) 且 `语速(wps) > max_wps`(8.0) → 物理不可能（3 词/0.16s=18.8wps）。防御深度，兜极短脑补。
3. **信号 C（低置信度）**：`avg_logprob < avg_logprob_thr`(-1.0) 且（`no_speech_prob` 缺失或 >= `no_speech_thr`(0.6)）→ Whisper 自判低自信。兜**听错型**幻觉（如把音乐听成 `Thank you.`），此型无几何指纹、单靠 A/B 拦不住。

### collapse 替换路径的特殊处理

`_decode_once` 同时被「洞恢复」与「collapse 替换」两条路径调用。collapse 路径要重新解码被替换段的窗口、用更多语音替换它——恢复段窗口**本就与被替换段重叠**，若对它也开信号 A 会把真正替换回来的语音误杀。故新增 `check_overlap` 参数：洞/长洞路径 `True`，collapse 替换路径 `False`（仍启用信号 B/C）。`fill_gaps` 内部通过给 `_probe` 加 `check_overlap` 透传实现。

### 不变量

- 只删除、不重算时间戳（遵守 ADR-012）。
- 信号 C 缺字段时自动 inert（向后兼容，不会误杀）。

## 理由

- **根因确定**：补洞恢复段绕过了转写层过滤器，是最低成本的修复点；在 `fill_gaps` 内部加一道几何守卫，与 ADR-020 形成双保险（文本相似度 + 时间戳几何）。
- **阈值保守可回归**：0.12s 重叠来自单视频 12 个样本标定的最小误判边界；真实恢复段间隙为 0，不误杀。
- **与已有机制互补**：`_is_echo`（文本）+ `_is_recovered_hallucination`（时间戳/语速/置信度）覆盖不同维度，互不强依赖。

## 后果

- **`src/video_translate/fill_gaps.py`**：
  - 新增 `_is_recovered_hallucination`、`_overlap_with_any`、`_recovered_wps`。
  - `_decode_once` 在 `_is_echo` 之后调用守卫；恢复段 dict 携带 `avg_logprob`/`no_speech_prob`/`compression_ratio`。
  - `_probe` 新增 `check_overlap` 参数；collapse 替换路径传 `False`。
- **`tests/test_fill_gaps_recovered_guard.py`**（新增）：jimmy 实测案例固化（7 幻觉命中 + 真实恢复段不误杀 + collapse 路径不受重叠信号影响 + 字段携带贯通），含 3 个端到端（FakeModel 替换 `faster_whisper`，无真实 GPU 依赖）。
- **`AGENTS.md`**：红线表新增「补洞恢复段幻觉」行；质量护栏表幻觉拦截行纳入 `_is_recovered_hallucination`。

## 已知限制

- `Thank you.` 这类听错型幻觉能否被信号 C 拦住依赖 `avg_logprob` 表现（把有能量噪声听成词时 log-prob 不一定低，ADR-020 已知局限）；真正兜住它的可能是语义回读（第三类）或 whisperX 对齐，与「第二三类等 whisperX 上了再试」的分工一致。
- 阈值 0.12s 从单视频标定，样本量有限；真实边界模糊的恢复段若重叠 0.15-0.3s 理论上可误杀，但 jimmy 数据无此类案例，且 ADR-020 经验支持「真实相邻段只模糊数百 ms、不整体侵入」。
- 信号 C 的 `avg_logprob` 阈值依赖模型规模；large-v3 实测 -1.0 安全，换小模型或需放宽（与 ADR-020 第五信号同源约束）。

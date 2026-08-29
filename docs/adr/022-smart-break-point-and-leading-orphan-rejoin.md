# ADR-022 — 断句切点智能回退与句首孤儿并右（V8）

- 状态：接受
- 日期：2026-08-22
- 关联：ADR-004（merge 阶段 golden）、Spec 13（merge→split 不可逆）、`merge.py` `_smart_break_index` / `rejoin_leading_orphans`、`tests/test_split_smart_break.py`

## 背景

jimmy.mp4 实测暴露 48 处「句尾词被掐到下一条字幕」（前段无句末标点 + 后段小写开头，如 `my sister` ‖ `deidre dixon`、`Annalise and` ‖ …、`we don't` ‖ `know if…`）。归因（基于词级时间戳逐对核查）：

| 根因 | 数量 | 机制 |
|---|---|---|
| `_split_by_length` 贪心切分 | 39 | 装满 42 字符就在当前词切，切点完全不看语义 |
| `_split_by_gap` 停顿切分 | 8 | >1s 真实停顿切开（Spec 13 要求保留），但留下 `because` 这类句首连接词孤儿 |
| 词时间戳塌缩（merge 条件4） | 1 | 本轮不动 |

42 字符是剪映单行显示上限（表现层硬约束），**不能取消切分**；问题只在切点选得蠢。

## 决策

### A. `_split_by_length` 智能切点回退（信号 1→2→3）

贪心窗口溢出触发切分时，切点在窗口内**向前回退**到更安全的位置：

1. **标点边界**（`,;:.!?`）：取**最靠后**的候选（前组尽量长、视觉扰动最小）。`So we're at home for three months, we don't know…` 从贪心的 `don't|know` 回退到 `months,|we`。
2. **最大词间停顿**：无标点时切在 `> DEFAULT_SMART_PAUSE`(0.3s) 的最大气口。>1s 的停顿已被 `_split_by_gap` 先行切走，这里捕 0.3–1.0s 的次级气口（说话人呼吸点）。
3. **贪心兜底**：无标点、无气口 → 维持旧行为。

**硬约束**（保证不变量）：
- 回退只向前挪 → 任何组都不可能超 `max_chars`（尾组须仍能容纳触发溢出的词）
- 前组 ≥ 2 词（不制造单词 cue）
- 只挪切点，词时间戳原样保留（ADR-012）

### B. `rejoin_leading_orphans` 句首孤儿并右（pipeline stage 5）

`_split_by_gap` 的合法产物里有一类**句首连接词孤儿**：停顿恰好落在连接词之后，切出 `because` [116.33,117.33]（1.35s 停顿后才是 `you do take…`）。V4 `merge_short_cues` 只左并（且 `because` dur=1.00 卡 `<1.0` 线漏接），而这类碎片的句子在**右边**。

并右条件（全满足才触发）：
1. ≤ 2 词
2. 不以 `[.!?]` 结尾（`Yes.` 类真短句不吞；`So,` 逗号尾仍算未完成句，并）
3. 距右段 < `DEFAULT_LEADING_ORPHAN_GAP`(1.5s)——jimmy 的 `But I` 距右段 1.89s，保持独立
4. 并后 span ≤ `max_dur`(8s)

停顿在 cue 内部存活（时间窗取并集，不重算时间戳）。V4 已接受 rejoin 后 cue 可略超 42（generate 不再切），B 同级风险。

### 放弃的设计（记录以免重蹈）

- **孤儿并左**：贪心前组装满才触发切分，孤儿并回左组在词和口径下**数学上恒超限**（否则贪心会继续装）。死路，未实现。
- **merge 条件4 放宽**（词塌缩重叠仍合并）：仅 1 处，收益不抵回归风险，不动。

## 验证（jimmy 全片重放，raw 94 段）

| pipeline | 嫌疑切断* | 段数 |
|---|---|---|
| 旧（贪心 + 无 leading rejoin） | 48 | 115 |
| 新（智能切点 + 句首并右） | **41** | 118 |

\* 嫌疑 = 前段不以 `.!?,;:` 结尾且后段小写开头。逗号结尾是合格断句不计入。

修复样本：`months,|we don't know`（原 `don't|know`）、`dad,|the real Jamie`（原 `Jamie|motherfucking`）、`because + you do take`（句首并右）。

### 已知天花板（41 处残余的成分）

残余切断的词间 gap **全部为 0.0**——无标点、零间隙，两个信号都物理不存在，贪心兜底是唯一选择（密集采访语流：`show like this`、`a room of` 类）。这不是参数问题：降停顿阈值无意义（没有 >0 的 gap 可选）。出路是 whisperX 的 forced alignment 拉开词边界让气口浮现——与「第二三类等 whisperX」分工一致。

## 后果

- `src/video_translate/merge.py`：新增 `_smart_break_index`、`rejoin_leading_orphans`、常量 `DEFAULT_SMART_PAUSE`/`DEFAULT_LEADING_ORPHAN_GAP`；`_split_by_length` 集成切点回退；`apply_merge` 新增 stage 5 与 `rejoin_leading` 参数（默认开）。
- `tests/test_split_smart_break.py`（12 用例）：jimmy 实测样本固化 + 贪心兜底回归保护 + 孤儿防误杀（`Yes.`、远距、超时窗、词数上限）+ 端到端 sub-cue 词边界不变量。
- 全量回归 263 passed；golden（apollo 无词数据）不受影响（`_split_by_length` 仅在有词时参与）。
- `AGENTS.md`：红线表新增「断句切点」行。

## 已知限制

- 切点回退使前组变短、后组继承更多词，极端时后组再触发切分，总段数 +3（115→118）——语义完整性优先于段数最少。
- 句首并右会吞掉 ≤1.5s 停顿到 cue 内部（`because` 的 1.35s）——阅读性优先于停顿外显，与 V4 左并哲学一致。
- 无标点 + 零 gap 的密集语流（41 处残余）本轮无解，待 whisperX。

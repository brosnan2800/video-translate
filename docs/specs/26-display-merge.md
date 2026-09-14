# Spec 26 — Display-layer short-cue merge (B 显示层短块合并)

## 背景

`st.mp4`（Steve Harvey 励志口播）翻译与生成链路跑完后，用户反馈：最后一分钟字幕
"碎"——大量 1~1.5s 的短字幕连闪。根因是那段说话人是"一句一停"的排比式喊话，
句间真实停顿 0.7~2.7s，whisper 忠实切开 → 每句独立字幕。

经排查，现有三道机制都兜不住：
- `merge_segments`：`gap < 0.5s` 且"左句不以 `.!?` 结尾"才并 → 停顿超、句末标点拦 → 不并。
- `split_long_cues`：内部含 >1.0s 静音的段**恒定**重新切开（`_split_by_gap` 非长度门控）。
- `merge_short_cues`：只救 `dur < 1.0s 且 ≤3 词` 的碎片，且左邻居不能以 `.!?` 结尾 →
  绝大多数尾段 cue 都 ≥1.0s（被 `--min-dur` 撑过），且左邻居多句末标点 → 不救。

仅调 `--merge-max-gap` 也无效：merge 放宽后 split 在 >1.0s 处切回。

## 决策（已与用户确认）

- **只动显示层**，不动 `segments_en.json` / `zh_segments.json` / `words` / `start` / `end`。
  与现有 `--tail` / `--min-dur` / `--gap` / `--offset` 同类（ADR-009/012 声学时间戳不变式）。
- 合并间隙阈值 **G = 0.8s**（= 用户愿意让一条字幕挂过的最长停顿）。
- 合并后**中/英各最多 2 行**：英文上限 **84** 字符（2 行 × 42），中文上限 **40** 字符（2 行 × 20）。
- 先**只在 st 上带 flag 试跑**，默认值不动；确认观感后再决定是否把 CLI 默认打开。

## 合并判定（全部满足才并）

相邻两条 cue `i-1`、`i`，`i` 加入当前组当且仅当：

1. 显示间隙 `gap_i = start_i - end_{i-1} ≤ G`（默认 0.8s）；
2. `i` 是"短块"：`dur_i ≤ 2.0s` **且** `词数_i ≤ 8`；
3. 合并后总时长 `≤ 6.0s`；
4. 合并后英文总字符 `≤ 84`（能按 42 宽折成 ≤2 行）；
5. 合并后中文总字符 `≤ 40`（能按 20 宽折成 ≤2 行）。

贪心：长块或间隙超限的 cue 自立一组；组只增不拆。链式合并受 (3)(4)(5) 上限在中间断开。

## 合并产物

- 窗口 = `[首条 start, 末条 end]`（内部停顿留在窗内）。
- 文本：中文各段拼接、英文各段拼接（按换行宽度各自折行）。
- 合并后**再跑一次** `min_dur` / `--gap` 钳制（与单条一致），保证不重叠、短条撑长。

## 不变式（强制）

- `segments_en.json` / `zh_segments.json` / `words` / 时间戳**只读**，绝不回写。
- 库默认 `display_merge=False` → `build_outputs` 输出与今日**逐字节一致**（golden 回归不受影响）。
- 不修声学层 14 项 cross-silence / overlap（那是转写分段特征，属另一条路线）。

## 可观测性（铁律）

- 侧车 `<base>.display_merge.json`：参数回显、每段合并覆盖的 `source_indices`、窗口、
  合并/拒绝的相邻对及原因（`gap=` / `en-overflow` / `zh-overflow` / `dur-overflow` / `right-not-short`）。
- 登记于 `artifacts.py`（`display_merge` artifact），`generate_opts.json` 扩展相应字段。

## 参数名（D3 命名单一来源）

| flag | 默认 | 含义 |
|---|---|---|
| `--display-merge` | off | 总开关 |
| `--display-merge-gap` | 0.8 | 相邻合并间隙上限 (s) |
| `--display-merge-max-dur` | 6.0 | 合并后窗上限 (s) |
| `--display-merge-max-chars` | 84 | 英文字符预算 (2×42) |
| `--display-merge-max-zh` | 40 | 中文字符预算 (2×20) |
| `--display-merge-short-dur` | 2.0 | 短块时长上限 (s) |
| `--display-merge-short-words` | 8 | 短块词数上限 |

模块常量定义在 `generate.py`（`DEFAULT_DM_*`），cli 默认值与之对齐。

## 测试（TDD）

- `tests/test_generate_display_merge.py`：纯函数边界 + st 尾段真实数据回归（17→8 实测）。
- `tests/test_pipeline_field_contract.py`：侧车 schema + index 覆盖完整性（每个 index 恰好被一个显示 cue 覆盖一次）。

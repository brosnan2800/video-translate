# ADR-043 — 输入形态自动判定（`pipeline` 按输入选 ASR 方案）

- **状态**：接受（**已实现** —— 见 [Spec 24](../specs/24-pipeline-behavior.md) §6 / [Spec 29](../specs/29-interface-asr-captions.md)；测试 `tests/test_pipeline_input_routing.py`）
- **日期**：2026-09-17
- **关联**：[ADR-042](042-youtube-captions-as-asr-source.md)（接口型 ASR：本项把它**接进入口**）、[ADR-033](033-control-plane-pipeline-entry.md)（pipeline 单一入口）、[ADR-030](030-control-plane.md)（控制平面：流程推进归代码）、[ADR-038](038-asr-layer-extraction.md)（可插拔 ASRProvider：本项是切点的**消费方**）、[ADR-035](035-pipeline-data-contract.md)（数据契约总线）、[Spec 23](../specs/23-environment-location.md)（命令入口）、[Spec 25](../specs/25-cli-path-hygiene.md)（路径卫生边界）
- **落地**：`src/video_translate/ytcaptions.py`（判定纯函数）、`cli.py`（`cmd_pipeline` 分发 + 卫生校验放行 URL）、`pipeline.py`（ctx 注入来源）、`pipeline_def.py`（按来源渲染 `cli` 串）；测试 `tests/test_ytcaptions.py` / `tests/test_pipeline_input_routing.py`

## 背景

[ADR-042](042-youtube-captions-as-asr-source.md) 引入了**第二套 ASR 方案**（接口型：从 URL 取现成字幕），并落地为独立子命令 `captions <url>`。产物与转写同契约，直接接 P2。

但它留下一个**入口断层**：`captions` 是独立子命令，而 [ADR-033](033-control-plane-pipeline-entry.md) 确立的**唯一入口**是 `pipeline`。于是：

1. 使用者（尤其是 Agent）必须**自己判断**输入形态再决定调 `captions` 还是 `pipeline` —— 这与「流程推进归代码」的既定分层相悖；
2. 若把 URL 直接丢给 `pipeline`，今天的行为是**把它当本地文件路径**：

```
$ uv run video-translate pipeline "https://www.youtube.com/shorts/GxggU7XoCLg"
```

- `_default_base(url)` = `Path(url).stem` = `GxggU7XoCLg`（侥幸正确）
- `_default_outdir(url)` = `Path(url).parent` = **`https:/www.youtube.com/shorts`**（垃圾路径）
- `next_action` → `transcribe` → **`cmd_run`**：拿本地 Whisper 去跑一个 URL

**这正是 [ADR-042](042-youtube-captions-as-asr-source.md) D1 明确反对的情形** —— D1 写的是「入口独立（不走 `run <video>`）……把 URL 塞进 `run` 会让『我明明没让它转写』变成隐性长任务」。今天 `pipeline` 干的就是这件事。

需求方（2026-09-17）要求消除该断层，并明确三点：① 判定放 `pipeline`；② 非 YouTube URL 明确报错；③ Agent 侧零负担。

### 实测证据（2026-09-17）

| 检查 | 实测结果 |
|---|---|
| `pipeline <url>` 是否自动走接口型 ASR | ❌ 分发到 `cmd_run`（本地 Whisper） |
| `STAGES` 是否有 captions 阶段 | ❌ 无；`transcribe` 阶段 `cli` 写死 `run <video>` |
| 全库是否有 URL 探测逻辑 | ❌ 无 |
| **入口卫生校验是否放行 URL** | ⚠️ **部分放行**：`/shorts/GxggU7XoCLg` 的 basename 是纯 id → 放行；但 `watch?v=<id>` 的 basename 是 `watch?v=<id>`，`?` 命中 Windows 非法字符集 `< > : " \| ? *` → **exit 2** |
| `README.md` / `AGENTS.md §0` 是否记录该通路 | ❌ README 零处；AGENTS.md 仅 §1 一条「取字幕通路」的**代理红线**，无「何时该用」 |

## 决策

### D1. 判定放**控制平面**（`pipeline`），不放 Agent 层

**这是本 ADR 的核心决策，其余各项都由它推导。**

| 维度 | 放 `pipeline`（采纳） | 放 Agent 层（否决） |
|---|---|---|
| 规则性质 | 输入形态是**确定性字符串分类**，不需要判断力 | 把机械规则交给"判断力"是错配 |
| 与既有架构 | [ADR-030](030-control-plane.md)/[ADR-033](033-control-plane-pipeline-entry.md) 已把「流程推进」整体收归代码状态机 | **自相矛盾** —— `AGENTS.md` 开篇自称「流程推进与闸门由代码状态机负责」，而入口判定属流程推进 |
| 可移植性 | 一份实现，所有调用方共享 | 每个 Agent 实现（WorkBuddy / Claude Code / Cursor / 纯 shell）**各自漂移** |
| 可测试性 | 纯函数 + 单测 | 靠每个 Agent 自觉，无契约 |

Agent 的职责边界不变：**只做代码做不了的事（翻译 + 语义回读）**（`AGENTS.md` 开篇既有口径）。

### D2. 判定规则：**唯一纯函数**，三态输出

判定**只定义一次**，放 `ytcaptions.py`（URL 形态本就属于接口型 ASR 的领域知识），由 `cli` 消费 —— 避免 `cmd_pipeline` 自写第二套正则。

三态：

| 状态 | 判据 | 行为 |
|---|---|---|
| `youtube` | 带 scheme 的 URL，**host 是 YouTube 域**，且 `parse_video_id` 能解出 11 位 id | 走接口型 ASR |
| `url-unsupported` | 带 scheme 的 URL，但 host 非 YouTube，或未含可解析的视频 id | `exit 2` + 指引（D3） |
| `path` | 其余一切 | 走本地 Whisper（**行为逐字节不变**） |

**判据一：只认带 scheme 的 URL**（`^[A-Za-z][A-Za-z0-9+.\-]+://`，scheme **至少 2 字符**）：

- `C:\videos\a.mp4` / `C:/videos/a.mp4` → 盘符后是**单斜杠**，不匹配 `://` → `path` ✅
- 手滑写成 `C://videos/a.mp4` → scheme 仅 1 字符，被「≥2 字符」收紧挡住 → `path` ✅
- UNC `\\server\share\x.mp4` → 无 scheme → `path` ✅
- 相对路径 `videos/a.mp4` → `path` ✅

**判据二：`youtube` 还要求 host 属于 YouTube 域**（`youtube.com` / `youtu.be` / `youtube-nocookie.com` 及其子域）。**只看 id 形状不够**：`https://example.com/abcdefghijk` 的末段恰好 11 字符、能通过 `parse_video_id`，会被误判成 YouTube —— 于是失败信息指向完全错误的病因。加上 host 判定，`url-unsupported` 才名副其实。

**边界（显式记录，避免被误读为 bug）**：**无 scheme** 的 `youtube.com/watch?v=x` 按 `path` 处理（与现状一致）。理由：把它当 URL 会让「本地真有个叫 `youtube.com` 的目录」这类路径产生新歧义，而完整 URL（浏览器地址栏复制）必然带 scheme。

> **措辞红线**：本判定只回答「**从哪里取原始段**」，不回答「取不取得到」。通路可达性归 [ADR-042](042-youtube-captions-as-asr-source.md) D8 的 `preflight_network`（`exit 8`）；解析出 id ≠ 该视频有字幕。

### D3. 非 YouTube URL：**明确报错**，不静默降级

`exit 2`（`EXIT_ARGS`，用法错误），打印指引说明「接口型 ASR 目前仅支持 YouTube；本地文件请传路径」。

**被否的方案**：

- **静默当成本地路径** → 会走到 `cmd_run` 拿 URL 跑 Whisper，报出的错与病因无关（最坏的一类失败）；
- **静默回退本地转写** → [ADR-042](042-youtube-captions-as-asr-source.md) D2 明令禁止（「我明明没让它转写」）；
- **新增退出码** → 退出码是项目契约，且 `exit 2` 语义（用法错误）本就精确匹配。

### D4. 与 ADR-042 D1 的关系：**强化**，不是推翻

必须交代清楚，否则两篇 ADR 读起来矛盾：

| | ADR-042 D1 说的是 | 本 ADR 说的是 |
|---|---|---|
| 反对什么 | **把 URL 塞进 `run`**（本地 Whisper 通路） | 同左 —— 本项正是把 URL **从 `run` 手里拿走** |
| 主张什么 | `captions` 是**独立入口**（URL 输入 ≠ 本地转写意图） | `pipeline` 把 URL **路由到那个独立入口** |

D1 的担忧与技术手段是两件事：它要保证「URL 永远不会触发本地转写」，而 `pipeline <url>` 今天**恰恰在违反它**。本项使该保证成立。

**`captions <url>` 保留为显式子命令**，理由：`--list` 探轨道、`--refresh` 强制重取、直连排查都需要一个不经过编排的直达入口（[ADR-042](042-youtube-captions-as-asr-source.md) D1「入口独立」的合理内核）。

### D5. URL 输入的产物落点：`outdir=videos`、`base=<video_id>`

与 `cmd_captions` 既有默认**完全一致**（`outdir` 缺省 `"videos"`、`base` 缺省 `video_id`），从而 `pipeline <url>` 与 `captions <url>` 落到同一目录 —— 幂等续跑与缓存复用才成立。

**不改**通用的 `_default_outdir` / `_default_base`：它们被 `run` / `generate` / `verify` 等复用，且 Spec 11 的语义（默认取输入自身目录与 stem）对本地路径是对的。URL 的默认值在 `cmd_pipeline` 内**按来源**计算。

### D6. 决策点在 URL 输入下的行为：**保留风格选择，跳过音频画像**

`next_action` 是纯函数、不感知来源，首跑仍返回 `stop_decision_point` —— 这是**正确的**（URL 输入同样需要定翻译风格）。

但 `cmd_pipeline` 该分支会调 `_ensure_audio_profile(..., input_path=URL, ...)`，而 URL **没有本地音频**。故按来源分支：**跳过画像**，改用固定文案说明「接口型 ASR 无本地音频，画像不适用（ADR-042 D3）」，风格选择照旧。

**被否的方案**：URL 输入免决策点 → 会让 URL 与本地路径的**翻译风格行为分叉**，且 `pipeline --prompt always` 的语义不再统一。

**由此产生的入口差异**（须写进文档）：`pipeline <url>` **经**决策点；`captions <url>` **不经**（既有行为）。

### D7. verify：URL 来源**不传 `--video`**

`cmd_pipeline` 的 verify 分支现状**无条件**传 `--video input_path`。而 `cmd_verify` 的逻辑是：**仅当 `--video` 缺失且 `asr_source.has_audio_reference == false`** 时才产出 `acoustic-unavailable`。

若把 URL 当 `--video` 传进去，该标记保持为空 → verify 会**真的去探测一个 URL** → 声学 lane 变红，且病因误导。

修法：来源为 URL 时**不传 `--video`**，让 [ADR-042](042-youtube-captions-as-asr-source.md) **D7** 的 `acoustic-unavailable` 契约正常生效（计入 flag → strict 默认 `exit 8`；`--no-strict` 显式弃权）。

> 注意：这是**分发层**的修法，`cmd_verify` 与 `verify.py` 零改动。

### D8. 入口边界卫生校验必须**放行 URL**（本项实测发现的既有缺陷）

[Spec 25](../specs/25-cli-path-hygiene.md) 的 `_path_hygiene_error` 在**每个子命令之前**校验路径参数，做法是「取 basename → 查非法字符」。而 URL 的 basename 可能**天然含 `?`**：

| 输入 | basename | Windows 非法字符集 `< > : " \| ? *` | 结果 |
|---|---|---|---|
| `.../shorts/<id>` | `<id>` | 无 | ✅ 放行 |
| `.../watch?v=<id>` | `watch?v=<id>` | `?` 命中 | ❌ **exit 2** |

即：**最常见的 `watch?v=` 形式在入口就被拦下**，此时自动判定根本没机会执行 —— 它会让本 ADR 在真实使用中直接失效。

**决策**：`_path_hygiene_error` 对**带 scheme 的 URL 值跳过文件名校验**（URL 不是文件名，`?` / `:` / `/` 在 URL 里是合法语法）。判定复用 D2 的同一谓词，不引入第二套规则。

**为什么不是「把 `?` 从非法字符集里删掉」**：那会削弱 Spec 25 对**真实文件名**的保护（`?` 在 Windows 文件名里确实非法，是编码事故的指纹之一）。URL 与文件名是两类东西，应按类别豁免，而不是放宽整类。

### D9. 幂等、状态与缓存

- URL 输入的 `state["video"]` 记**原始 URL**（可追溯来源，且与本地路径形态可区分）；
- `captions_cache` 的失效口径**沿用既有**（请求语言变化 / `--refresh`）；
- **本期不给 `pipeline` 增加 `--refresh`**：需要强制重取时用显式 `captions --refresh`（逃生门写进文档），避免把 `pipeline` 的参数面铺开（ADR-033 已收窄过一次）。

### D10. 非目标（明确排除）

- **不接 `yt-dlp`、不支持非油管平台取字幕**（属 `RESEARCH-voice-pro` **P3**，[ADR-042](042-youtube-captions-as-asr-source.md) D5 的取舍联动）；
- **不改 `run_asr` / `ASRProvider` 契约**：本项只是**决定选哪个入口**，Provider 层零改动；
- **不动 `merge` / `generate` / `verify` 内部逻辑**；`gc` 不改产物 schema（[ADR-035](035-pipeline-data-contract.md) 契约表是唯一事实来源）。

## 理由

- **零新增概念**：不新增状态机阶段、不新增退出码、不新增产物文件 —— 只在现有分发点加一次分类；
- **复用而非新建**：`cmd_captions` 已是完整通路（`parse_video_id` → 代理 → `YtCaptionProvider` → `run_asr` → 落 `asr_source` → 记 transcribe 阶段 → 打印 NEXT），本项只是让它**可被 `pipeline` 到达**；
- **失败语义收敛且可诊断**：非油管 URL → `exit 2`（用法错误）；通路不通 → `exit 8`（既有 gate）；取不到字幕 → `exit 1`（既有）。**每一类都能一眼看出病因**；
- **本地路径分支零变化**：判定前置且互斥，URL 的所有特化逻辑（落点默认、决策点跳画像、verify 不传 `--video`）都用来源条件包住。

## 后果

- **正面**：`pipeline <url>` 与 `pipeline <本地文件>` 成为**同一入口的两种输入形态**，Agent 侧零判断（消除 ADR-042 留下的入口断层）；[ADR-038](038-asr-layer-extraction.md) 的可替换性从「Provider 可换」推进到「**入口不必换**」；
- **代价 / 注意**：
  - 入口多一层分类逻辑（但为纯函数、O(1)、无 I/O）；
  - `pipeline` 与 `captions` 在**决策点**上的差异成为一处需要解释的行为（D6），文档必须写明，否则会被当成不一致；
  - 触碰了 Spec 25 的边界校验（D8）—— 需在 Spec 25 补注「URL 豁免」的契约，并补回归用例；
  - **无 scheme 的 `youtube.com/...` 仍按路径处理**（D2 边界），文档需写明，避免被当成 bug。

## 未来（不在本 ADR 范围）

- **P3 `fetch` 入口**（`yt-dlp` 拉片源 → 本地转写）：届时「非油管 URL」可能从「报错」升级为「可下载后转写」，本 ADR 的 `url-unsupported` 分支即为那时的挂载点；
- **更多接口型来源**（B 站字幕 / 其他平台）：D2 的三态分类与 D3 的报错语义可直接扩展（`parse_video_id` 换成多平台解析器）；
- **`pipeline` 层级的 `--refresh`**：若实测发现缓存失效场景频繁，再考虑上提（D9 已留此路）。

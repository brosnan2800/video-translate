# Spec 24: Pipeline 单一入口（幂等推进器行为契约）

- 状态: 批准（实现）
- 日期: 2026-09-03
- 关联: ADR-033（决策）、ADR-030（控制平面）、ADR-032（决策点协议）、ADR-034（默认裸跑）、[ADR-043](../adr/043-input-form-auto-routing.md)（输入形态自动判定）、[ADR-042](../adr/042-youtube-captions-as-asr-source.md)（接口型 ASR）、MAJOR_VERSION_PLAN §T8

## 目标

提供唯一入口 `pipeline`：每次调用把流水线**推进到下一个协作挂起点**，重复
调用幂等续跑。Agent 不再编排命令序列，只在挂起点接手（问人 / 翻译 / 语义回读）。

## 范围

- **IN（本 Spec）**：`pipeline` 子命令与 `--prompt` 三档；`next_action` 纯决策
  函数；决策点收窄为 style 单项（ADR-034 调和）；NEXT 块与退出码契约；
  **输入形态自动判定**（§6，ADR-043）。
- **OUT**：不新增状态机阶段、不新增退出码、不改 `run`/`generate`/`verify`
  三个原语的行为与参数（golden 保护）；不新增产物文件或缓存指纹维度。
  §6 的输入形态判定**不违反本条** —— 它只在分发层按来源选择执行器，
  三个原语的自有闸门与退出码原样透传。

## 配置（三级覆盖）

| 级别 | 形式 | 示例 |
|---|---|---|
| CLI | `--prompt {always,never,require-profile}` | `--prompt never` |
| 环境变量 | `VT_PROMPT` | `VT_PROMPT=never` |
| toml | `[pipeline] prompt = "never"` | 经 `_TOML_SECTIONS` 摊平 |
| Python 字段 | `Config.prompt` | 默认 `"always"` |

非法值 → stderr 告警并回落 `"always"`（不崩溃，与 `VT_ALIGN` 同 idiom）。

## 行为契约

### 1. 六动作决策表（`pipeline.next_action`，纯函数）

输入：`pos`（`resolve_position` 输出）、`prompt_mode`、`routing_explicit`
（`decisions.routing.origin == "explicit"`）。

| 当前位置 | always（默认） | never | require-profile |
|---|---|---|---|
| 无产物（preflight 未做） | `stop_decision_point`（画像先落盘） | `transcribe` | 无 explicit → 拒跑（见 §4） |
| routing 已落盘（transcribe 待跑） | `transcribe` | `transcribe` | 有 explicit → `transcribe` |
| translate 待做（zh 缺失） | `stop_translate` | `stop_translate` | `stop_translate` |
| generate 待做（zh 有、srt 缺） | `generate` | `generate` | `generate` |
| verify 待做（srt 有、verify 未 ok） | `verify` | `verify` | `verify` |
| 全部完成 | `done` | `done` | `done` |

不变量：决策表只依赖 `pos` 与两个布尔，零 I/O；`pos` 来自
`resolve_position`（产物为准、state 只加信号，ADR-030 口径不变）。

### 2. 停点与退出码

- **决策点停点**：画像（duration / silence_intervals / recommendation）落盘后
  打印 NEXT 块标注 `(STOP POINT — decision point: style only)`，返回
  **exit 6**。Agent 按 §4.5 问 style（默认 film），落盘 routing 后重跑
  `pipeline` 自动续 transcribe。
- **翻译停点**：与 `run` 完全一致（translate_task 已生成、zh 缺失），exit 6，
  NEXT 标注翻译职责。
- **`--prompt never`**：决策点不挂，`cmd_run` 按画像推荐自动路由
  （origin=profile），一路推进到翻译停点。
- **`--require-profile`**：无 `origin=explicit` 的 routing → 打印修复指引，
  **exit 8**（复用 `_resolve_routing` 硬闸路径，不另起炉灶）。
- **done**：打印完成信息（含 pending 语义回读提示），exit 0。

### 3. 执行分派（不复制 gate）

`cmd_pipeline` 把动作映射到既有执行器，执行器自带闸门与退出码：
`transcribe → cmd_run`（含 G1/G2/G3 review 与 exit 6 翻译停点）、
`generate → cmd_generate`（enforce 前置闸）、`verify → cmd_verify`（strict 默认）。
执行器返回非 0 时 `pipeline` 原样透传该退出码。

### 4. 显式 flag 透传

`pipeline` 接受与 `run` 相同的路由相关显式 flag（`--style` / `--vad` /
`--adaptive-vad` / `--separate-vocals` / `--vad-threshold` / `--engine` 等），
经 `_resolve_routing` 以 `CLI flag > routing > 画像推荐` 合并并落盘
origin=explicit。决策点问出的 style 由 Agent 以重跑
`pipeline --style <picked>` 落盘。

### 5. TDD 清单

- `next_action` 六动作 × 三档 prompt 的决策表全覆盖（纯函数，无 mock I/O）；
- `Config.prompt` 三值校验 + 非法值回落 `always`；
- `cmd_pipeline` 分派：mock 执行器断言调用了正确的原语并透传退出码；
- 停点 NEXT 文本含 `STOP POINT` 与 `decision point` / 翻译职责标注；
- 底层原语回归：`run`/`generate`/`verify` 既有测试零改动全绿。

## 6. 输入形态自动判定（ADR-043）

`pipeline <输入>` 接受**两种输入形态**，由控制平面在分发前判定；**Agent 侧无需判断**
（[ADR-043](../adr/043-input-form-auto-routing.md) D1：入口判定属流程推进，归代码）。

### 6.1 三态判定（唯一纯函数）

判定实现在 `ytcaptions.classify_input`（[ADR-043](../adr/043-input-form-auto-routing.md) D2），
`cli` 消费其结果，**不另写正则**：

| 状态 | 判据 | 分发 |
|---|---|---|
| `youtube` | **带 scheme** 的 URL 且能解出 11 位视频 id | `transcribe` 动作 → **接口型 ASR 通路**（`cmd_captions`，[Spec 29](29-interface-asr-captions.md)） |
| `url-unsupported` | 带 scheme 的 URL 但非 YouTube | **`exit 2`** + 指引，**不落任何产物**（ADR-043 D3） |
| `path` | 其余一切 | `transcribe` 动作 → `cmd_run`（本地 Whisper），**行为逐字节不变** |

- 只认**带 scheme** 的 URL（`scheme://`）：`C:\` / `C:/` 盘符（单斜杠）与 UNC 路径不匹配 → `path`。
- **边界（显式记录）**：无 scheme 的 `youtube.com/watch?v=x` 按 `path` 处理（ADR-043 D2）。

### 6.2 URL 输入的落点与差异

| 项 | 本地路径 | **URL 输入** |
|---|---|---|
| `outdir` 缺省 | 输入自身目录（Spec 11） | **`videos`**（与 `captions` 一致） |
| `base` 缺省 | 文件名 stem（Spec 11） | **解析出的 video id** |
| 决策点 | 画像 + 风格 | **仅风格** —— 无本地音频，跳过画像（ADR-043 D6） |
| verify 的 `--video` | 照传 | **不传** → [Spec 29](29-interface-asr-captions.md) 的 `acoustic-unavailable` 生效（ADR-043 D7） |
| `--refresh` | 不涉及 | `pipeline` **不提供**；强制重取用显式 `captions --refresh`（ADR-043 D9） |

`run` / `generate` / `verify` 三个原语的**行为与参数零改动**；URL 的差异全部在分发层
用来源条件包住（ADR-043「本地路径分支零变化」）。

### 6.3 入口边界：URL 豁免路径卫生校验

Spec 25 的 `_path_hygiene_error` 对**带 scheme 的 URL 值跳过文件名校验** —— URL 不是文件名，
`?` / `:` 在 URL 里合法，但命中 Windows 非法字符集。**不豁免则最常见的 `watch?v=` 形式会在
入口被 `exit 2` 拦下**，本节规则根本无从执行（ADR-043 D8，实测缺陷）。
豁免只针对**URL 形态**，真实文件名仍受 Spec 25 全量保护。

### 6.4 TDD 清单（本节新增）

- 三态判定纯函数：`youtube` / `url-unsupported` / `path`；边界含 Windows 盘符（`C:\`、`C:/`）、
  UNC、相对路径、无 scheme 域名、裸 id、带查询串的 `watch?v=` / `youtu.be/...?t=`；
- `pipeline <youtube url>` → 分发到接口型 ASR 通路（mock），落点 `videos/<id>/`；
- `pipeline <非油管 url>` → `exit 2` + 指引，且**不落任何产物**；
- `pipeline <本地路径>` → 分发与行为**与改动前一致**（回归保护）；
- URL 输入下 verify **不传 `--video`**；
- 卫生校验：`watch?v=` 形式**放行**；真实含 `?` 的 Windows 文件名**仍拒绝**（Spec 25 保护不回退）。

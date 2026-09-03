# Spec 24: Pipeline 单一入口（幂等推进器行为契约）

- 状态: 批准（实现）
- 日期: 2026-09-03
- 关联: ADR-033（决策）、ADR-030（控制平面）、ADR-032（决策点协议）、ADR-034（默认裸跑）、MAJOR_VERSION_PLAN §T8

## 目标

提供唯一入口 `pipeline`：每次调用把流水线**推进到下一个协作挂起点**，重复
调用幂等续跑。Agent 不再编排命令序列，只在挂起点接手（问人 / 翻译 / 语义回读）。

## 范围

- **IN（本 Spec）**：`pipeline` 子命令与 `--prompt` 三档；`next_action` 纯决策
  函数；决策点收窄为 style 单项（ADR-034 调和）；NEXT 块与退出码契约。
- **OUT**：不新增状态机阶段、不新增退出码、不改 `run`/`generate`/`verify`
  三个原语的行为与参数（golden 保护）；不新增产物文件或缓存指纹维度。

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

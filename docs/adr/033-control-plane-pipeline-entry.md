# ADR-033 — Pipeline 单一入口：幂等推进器 + 决策点收窄

- **Status**: Accepted（T8 落地记录）
- **Date**: 2026-09-03
- **关联**: ADR-030（控制平面）、ADR-032（决策点协议）、ADR-034（音频路由重构）、ADR-035（数据契约总线）、Spec 24（pipeline 行为契约）、MAJOR_VERSION_PLAN §T8
- **落地**: `pipeline.py`（`next_action` 纯决策）、`cli.py`（`cmd_pipeline` + `pipeline` 子命令 + `--prompt`）、`config.py`（`prompt` 字段）、AGENTS.md §3 瘦身

## 背景

ADR-030 把流程推进收归代码状态机，但入口仍是 `run` / `generate` / `verify`
三个离散原语：「该跑哪一步」仍靠 AGENTS.md §3 的 Phase 0–4 编排散文由 Agent
软判断。散文与状态机是两套说法，容易打架（P0 skip 事故的根因之一），且 Agent
必须记忆 generate/verify 的参数拼装。

同时，T8 起草于 ADR-034 之前：原设计「决策点问 style/vad/separate 三项」与
ADR-034 的「默认裸跑、画像仅供参考」已经冲突——`profile_recommendation` 现在
恒返回 `vad=False/adaptive_vad=False/separate_vocals=False`，再在决策点问 VAD
与分离等于把刚拆掉的画像路由又架回来。

## 决策

**新增 `pipeline` 子命令 = 幂等推进器。** 每次调用 `resolve_position()` 定位，
再执行「当前该做的下一步」，推进到下一个协作挂起点；重复调用自动续跑。

1. **决策/执行分层**：`pipeline.next_action(pos, prompt_mode, routing_explicit)`
   是零 I/O 纯函数，返回六个动作之一（`done | stop_decision_point |
   transcribe | stop_translate | generate | verify`）；`cmd_pipeline` 只把动作
   分派到既有执行器（`cmd_run`/`cmd_generate`/`cmd_verify`），**不复制任何
   gate / 执行逻辑**——底层三个原语行为零变化。
2. **决策点收窄为 style 一项**（与 ADR-034 调和）：preflight 画像落盘后，
   决策点只问翻译风格（默认 film，源自 config）；VAD / 人声分离退化为
   `pipeline` 的显式 CLI flag（经 `_resolve_routing` 的 `CLI > routing > 画像`
   合并，origin=explicit 落盘）。`--require-profile` 硬闸语义不变。
3. **停点复用 exit 6**：决策点停点与翻译停点共用 `EXIT_AWAITING_AGENT(6)`，
   以 NEXT 块文本区分（`decision point` vs `translate`）。不新造退出码。
4. **画像落盘单源**：`_resolve_routing` 内「无快照则 analyze_audio + 落盘」
   抽为 `_ensure_audio_profile`，决策点与 run 共用——画像全链仍只算一次
   （ADR-035 M2 口径不变）。
5. **AGENTS.md 瘦身**：§3 删 Phase 0–4 逐命令编排散文，只留 ① 入口调用
   （`uv run video-translate pipeline <video>`）② 挂起时 Agent 职责
   （决策点问人 + 翻译 + 语义回读）③ §4.5 决策点协议（改 style 单项）
   ④ §1 红线反模式与 §3.5 退出码速查。

## 理由

- **编排权彻底收归引擎**：Agent 从「记命令的指挥家」变成「挂起点的接手人」，
  散文与状态机不再并存，软约束源头被消灭。
- **幂等 = 断点续跑的自然延伸**：任何时刻重跑 `pipeline` 都安全，与 chunk
  缓存 / state 指纹机制天然契合。
- **不新造阶段、不新造退出码**：两个停点（决策点 / 翻译）复用既有 STAGES 表
  与 exit 6 语义，blast radius 最小；`run`/`generate`/`verify` 原语保留供脚本
  与 golden 回归。

## 后果

- 正面：单一入口跑通全流程（含 Nobody 验证）；AGENTS.md 瘦身后只剩机器覆盖
  不到的内容；决策点与 ADR-034 口径一致，画像路由不会被决策点复活。
- 负面 / 注意：`prompt` 默认 `always` 意味着首次 `pipeline` 必在决策点停一次
  （Agent 问人或超时自动）；想全自动的脚本用 `--prompt never`。
- 规范入口：Spec 24；单测 `tests/test_pipeline_advance.py`。

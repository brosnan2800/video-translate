# ADR-030 — 控制平面：流程收归代码状态机 + 意图闸 + strict verify

- **Status**: Accepted（已全量落地，cp Steps 1–10）
- **Date**: 2026-09-01
- **关联**: `docs/archive/CONTROL-PLANE-PLAN.md`（设计定稿）、ADR-005（Agent-as-Engine 文件契约）、ADR-012（声学真值）、ADR-020/021（幻觉拦截）、ADR-028（WhisperX 对齐）、ADR-029（命令入口）、Spec 18（verify）、Spec 23（环境定位）
- **落地**: `capabilities.py`、`state.py`、`pipeline_def.py`、`pipeline.py`、`cli.py`（意图闸 / generate 前置闸 / verify strict / `status` / NEXT 块 / 退出码 8）、`toolchain.py` 工具解析持久化；AGENTS.md 双职责分离；Makefile 移除

## 背景

master 基线的失控不在算法层（19 模块、300+ 测试、断点续跑、幻觉拦截均健康），
而在三个无强制力的交接处：**P0 地基只打印不应用 → 停点 A 之后 generate 无前置校验 →
P4 verify 默认放行**。流程推进靠 AGENTS.md 文字编排（Agent-as-Orchestrator），
文字是软约束——agent 可以跳步、回退、自创 flag，静默点（explicit 缺包静默降级、
画像失败 skip、uncovered 异常吞成 `[]`、语义回读只产不消费）全部由此滋生。
实测事故：对齐后段数 40→42，旧翻译按 index 错行串行，generate 照样出货。

## 决策

**把交接处的契约从 agent 的脑子里搬进代码：状态落盘 + 阶段前置 enforce 闸门 +
意图分级（explicit 必须被满足）+ verify strict 默认。**

1. **意图闸（exit 8 硬停）**：显式 `--align whisperx` 而包不可用、显式
   `--separate-vocals` 而 demucs 未装 → 硬停 + 修复指引，不再静默降级；
   `auto` 默认路径保留降级（原因落盘 `decisions.align.resolved`）。
   统一逃生门 `--allow-degrade`，必须显式传入。
2. **状态链**：`<base>.vt_state.json` 落盘 `decisions`（value + origin:
   explicit|default）、`stages`（status + `segments_sha` 指纹 + n_segments）、
   capabilities 快照。损坏自动从产物文件重建；**闸门只依赖 segments/zh 文件
   本身，不依赖 state**（防「删 state 绕闸」）。
3. **generate 前置闸**：zh 覆盖率 100% + en/zh 段数匹配 + `verify_align` 命中 +
   `segments_sha` 陈旧检测（对齐后忘重译 → 拦截）——40→42 错行事故的机器防线。
4. **verify strict 默认**：`--zh`/`--video` 必填（缺 = exit 2）；任一 lane 红灯
   = exit 8（`--no-strict` 逃生）；画像失败/探测异常 = 红灯而非 skip；
   语义回读 result 被消费（非 ok 判定 = 红灯），缺失则重挂 task 并置
   `pending_agent`。
5. **声明式状态机**：`pipeline_def.STAGES` 纯数据表（requires/caps/gate/
   stop_point），`pipeline.py` 引擎（<400 行）负责 check/enforce/position/
   NEXT 渲染；新子命令 `video-translate status [--json]` 回答「你在哪/缺什么/
   下一步」；`run`/`generate` 尾部打印 [NEXT] 块。
6. **AGENTS.md 双职责分离**：翻译剧本（Phase 2）与语义回读原样保留；
   Phase 0/1/3/4 改写为状态机客户端接口；新增 §3.5 状态机速查（退出码 0–8、
   `status --json`、NEXT、pending_agent）。
7. **Makefile 移除**：`$(...)`/`rm -rf`/`command -v` 全是 sh 语法，Windows 必挂；
   且与 R7（单入口 `uv run`）矛盾。编排不设第二软约束，删除。

## 理由

- **约束源从文字移到代码**：机器闸不会跳步、不会忘、不会自行发明逃生门；
  agent 从「指挥家」收窄为「翻译员 + 语义回读员」，只剩机器给不了的两件事。
- **停点语义显式化**：exit 6（停点 A）/ exit 8（闸门）/ `pending_agent`
  （停点语义回读）三态可被人和 agent 无歧义消费。
- **能力探测单源**：`capabilities.py` 复用 doctor 既有探测，doctor 变为其
  一种渲染；`toolchain.py` 工具解析持久化使库调用（绕过 `main()`）自愈。

## 后果

- 正面：九个静默点全部收口（映射见 CONTROL-PLANE-PLAN §2.1）；带洞翻译
  （覆盖率/段数/漂移/陈旧）无法再出货；`status --json` 让 agent 不再靠读散文编排。
- 负面 / 注意：verify strict 化会拒绝历史上能退 0 的坏输出（**设计意图**）；
  逃生门必须显式；既有 CI/脚本若依赖旧默认需加 `--no-strict`。
- 规范入口：`AGENTS.md` §3.5 / `docs/archive/CONTROL-PLANE-PLAN.md`；单测
  `tests/test_capabilities.py`、`test_state.py`、`test_control_plane_intent.py`、
  `test_generate_gate.py`、`test_verify_gate.py`、`test_pipeline.py`、`test_state_chain.py`。

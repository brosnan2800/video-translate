# 翻译流水线扩展规范（Pipeline & Workflow Extension）

> 新阶段/新子命令/新守卫不是「写个函数」的事——它们改变状态机契约。按清单逐项打勾。

## 新增阶段（stage）清单

1. **纯数据进表**：`pipeline_def.py` STAGES 加条目（id/title/requires/caps/gate/
   produces/cli/stop_point，零执行逻辑），**同步** `state.py STAGE_ORDER`。
2. `pipeline.py` 定义执行与闸门解释；agent/human 停点必须打印 `[NEXT]` 块
   （`--json` 机器可解析）。
3. **产物契约**：写什么文件、命名 `<base>*.json`、落在 videos/ 与产物同目录；
   requires 的 ctx key 与产物 key 对齐。
4. **闸门**：`enforce()` 只查产物文件本身（永不查 state）；拦截必须带修复指引
   （exit 8）；确需逃生门显式留痕。
5. **决策落盘**：显式 flag → `_record_run_decisions` 模式记 origin；
   静默点必须在 ADR-030 表里登记编号。
6. **agent 交接**：机器产任务 JSON（`translate_task.json` / `semantic_reread_task.json`
   模式），**CLI 永不直接调 LLM**（ADR-005）；停点返回 `EXIT_AWAITING_AGENT(6)`，
   agent 结果回读时消费（非 ok = 红灯，缺失 = 挂任务 pending_agent）。
7. 退出码映射（0-8 语义，不私造新码）、`status --json` 字段、`doctor` 能力探测
   （capabilities.CAPS）同步。
8. 测试三件套：成功路径 + 闸门拦截路径 + 陈旧/缺失产物路径。
9. 文档：AGENTS.md 状态机章节 + README 命令表 + ADR。

## 新增子命令（CLI）清单

`build_parser` 接线（参数与 help）→ `cmd_*(args) -> int` 返回退出码 →
完成时 `_record_*_stage` 落状态链 → 若改 segments 必须**刷新 segments_sha 锚点**
（resegment 先例）→ README/AGENTS.md 文档 → 成功/失败双路径测试。

## 新增守卫/巡检清单

放对层（转写/恢复段/校验）→ 纯函数优先（合成数据可测）→ 单测用**真实事故几何**
→ 拦截打印可见 → 阈值进模块常量并在 ADR 里标定来源 → **所有产出路径共用**
（防止绕道）→ AGENTS.md 红线表登记。

## 修「产物已错」的两条正路

1. **守卫补丁优先**（防全片重跑复发），再考虑修数据；
2. 手工修数据必须走「合法修订」路径（resegment / backfill），并在提交说明里
   指明为何守卫没拦住、已如何堵上。
   **禁止**：直接改 SRT/segments 不留痕、删 chunk 缓存、删输出目录
   （剪映缓存碰撞 → 靠 `_vN` 递增）。
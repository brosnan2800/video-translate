# 给 CodeBuddy 的入口说明

这是 **brosnan2800/video-translate**（AI 视频翻译流水线）的「失控防治」方案包。目标：让 AI 辅助写代码/跑翻译时，人不用盯全程也不会失控。

## 你要做的事（一句话）
**实现 Gap B**：把 Phase 4 的质量红线从「报告型」升级成「半自动硬闸门」，含分流 + 熔断。详细施工单见 `GAPB_IMPLEMENTATION.md`，照着做即可。

## 阅读顺序
1. **`GAPB_IMPLEMENTATION.md`** ← 先读这个，是给你的施工单（文件/函数/`MAX_RETRY=2`/schema/smoke test 全写死）。
2. `SOLUTION.md` §7.4「Phase 4 硬闸门 + 自动恢复 + 熔断」← Gap B 的设计依据与两个硬约束。
3. `PIPELINE.md` Stage 4 + 红线 R10 ← 执行流程与红线定义。
4. `advisors/ADVISORS.md` ← 决策项扩展规范（新增项 = 新建 `advisor_*.py`，不改旧代码）。
5. `gates/checkpoint.py`、`gates/preflight_decision.py`、`advisors/*` ← 已有的「Phase 0→1 人工签字闸门」参考实现，照它的风格写 `gates/verify_gate.py`。

## 两个硬约束（用户原话，不可违背）
- **① 熔断**：红线命中后自动重跑不能无限循环。`retry >= MAX_RETRY(=2)` 必须停下转「半自动」（人批 `make approve verify`）。
- **② 分流**：声学 / 确定性内容问题走自动恢复环；**语义回读是独立子任务，命中即转半自动**，绝不进自动循环。

## 已有 vs 待做
- ✅ 已写：Phase 0→1 签字闸门（`preflight_decision.py` / `advisors/*` / `Makefile` 改写），12 项 smoke test 全过。
- ✅ 已写：verify 人审 `GATED`（`checkpoint.py` 里 `verify` 标 GATED，不批不放行）。
- ⬜ **待你写**：Gap B 全部（`gates/verify_gate.py` + 改 `checkpoint.py`/`Makefile`/真实仓库 `verify.py`）。

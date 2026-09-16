# 规则目录职责划分（Scope）

本目录（`.clinerules/`）= **工程规范**：代码怎么改、测试怎么写、依赖怎么加、
流水线怎么扩展、怎么提交。对任何 AI 编码助手永久生效。

`AGENTS.md` = **业务执行协议**：翻译流水线怎么跑（状态机、停点、防呆红线、
VAD 路由决策表）。它不是编码规范，不要往里加工程规则。

优先级：操作流水线时 AGENTS.md 的防呆红线优先；改代码时本目录规则优先；
两者冲突时 = 设计缺陷，提出来讨论，不要擅自择一。

文件索引：
- `01-sdd-tdd.md` — 何时规格先行、红绿循环、事故固化义务
- `02-architecture-and-code.md` — 分层方向、纯函数纪律、控制面原则、守卫可见性
- `03-dependencies-and-tools.md` — Python 依赖准入、外部二进制/模型接入模式
- `04-pipeline-extension.md` — 新阶段/新子命令/新守卫的接入清单
- `05-testing.md` — 测试分层、mock 规范、真实事故几何
- `06-commits-and-docs.md` — 提交纪律与文档同步义务

## 读取约定（C5，指引）

读项目内**文档 / 代码 / 产物 / `videos/`** 一律走**本地文件系统目录**，不用 git 追踪范围
判断存在性 —— **不在 git ≠ 不存在**：`.gitignore` 覆盖的 `videos/` / `.codebuddy/` /
`models/` / `tools/` / `docs/golden/` 都真实可读。**否定性判断**（「不存在」「已删除」）
必须先正向验证（`Test-Path` / 显式列目录 / 含隐藏项），不得从带过滤条件的枚举结果反推。

> **主源**：`.codebuddy/rules/operation-constraints.mdc` §C5（全文与理由）；
> 本处只作指引，不复制正文（避免双源漂移）。
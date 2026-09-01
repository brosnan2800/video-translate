# 提交与文档（Commits & Docs）

## 提交纪律

- **一个逻辑变更一个 commit**；禁止把无关文件夹带进同一提交。
- 提交前检查：`uv run pytest -q` 全绿 + `git status --short` 只含本变更文件。
- message 格式：`type(scope): 中文主题`。type ∈ feat / fix / docs / refactor /
  chore / test / adr-N（ADR 落地提交用 `adr-N:` 前缀）。scope 用模块/子系统名
  （control-plane / resegment / verify…）。例：
  - `adr-031: 恢复段质量硬化——resegment 守卫/恢复段可见化/…`
  - `fix(control-plane): resegment refreshes segments_sha anchor …`
- 提交信息是 UTF-8 中文；Windows 控制台 `| Select-Object` 管道显示乱码是
  解码假象，以 `git log -1 --format=%s` 直连输出为准。

## 文档同步义务（改了什么就必须同步什么）

| 改动 | 必须同步 |
|---|---|
| 架构/协议/闸门/数据格式决策 | `docs/adr/NNN-*.md`（编号递增不复用） |
| 流水线行为 | `docs/specs/NN-*.md`（编号递增） |
| 新守卫/新巡检/新阈值 | AGENTS.md 红线表与质量护栏表（业务护栏登记处） |
| 新子命令/参数 | README 命令表 + AGENTS.md 对应章节 |
| 事故复盘 | docs/HISTORY.md（或独立 postmortem）+ 红线表 |
| 依赖变更 | pyproject + uv.lock 成对 + TOOLCHAIN.md（如涉及安装路径） |

## 语言与风格

- 文档/注释/commit：中文；代码标识符：英文。中英不混排标识符。
- 规格文档三要素齐全：背景（证据）/决策（编号 D1-Dn）/测试计划与后果。
- 事实必须可溯源：阈值写明标定来源（哪支视频哪次实测），几何数据写明段号时间。
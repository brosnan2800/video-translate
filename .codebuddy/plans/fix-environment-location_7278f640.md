---
name: fix-environment-location
overview: 一次性根除"环境定位漂移"：裸 python/video-translate/make 命中旧环境 F:\Python311，统一命令入口为项目 .venv 确定路径（uv run / .venv\Scripts\ 绝对路径 + 激活脚本），修正 Makefile/README/AGENTS 中的裸命令，并可选清理系统 PATH 旧环境残留。
todos:
  - id: add-env-ps1
    content: 新增 env.ps1 一键激活脚本，cd 项目根并激活 .venv、打印 python/whisperx/torch 确认
    status: pending
  - id: fix-makefile
    content: 修改 Makefile，PY/PIP 改用 .venv 确定入口，doctor/setup/test 不再裸调 python
    status: pending
  - id: fix-agents-md
    content: 修改 AGENTS.md 红线表新增环境定位条目，并修正 Phase 0 命令入口
    status: pending
  - id: fix-readme-md
    content: 修改 README.md，将裸命令示例统一改为 .venv 确定入口与激活前置说明
    status: pending
  - id: verify-new-shell
    content: 用 env.ps1 复测 doctor 确认 whisperx OK、CUDA 指向 .venv，并复测中文视频路径不再乱码
    status: pending
    dependencies:
      - add-env-ps1
  - id: path-cleanup-guide
    content: 输出 PATH 清理的可选 PowerShell 指引命令，由用户确认后自行执行，不自动改系统
    status: pending
    dependencies:
      - verify-new-shell
---

## 需求概述

一次性根除「环境定位漂移」问题：当前裸运行 `python` / `video-translate` / `make` 都会命中旧环境 `F:\Python311`（缺 whisperx），而项目真实环境是 `.venv`（uv 管理，whisperx 3.8.6 + torch 2.8.0+cu128，CUDA 可用）。用户要求先把这个结构性缺陷修好，再开新窗口复测，避免以后每次重新排查环境。

## 核心目标

- 建立**确定性命令入口**，使所有 CLI / Python / 测试命令不再依赖 PATH 解析，永远定位到项目 `.venv`。
- 提供 Windows 一键激活脚本，让「新开窗口」只需一条命令即进入正确环境并打印版本确认。
- 修正 Makefile、AGENTS.md、README.md 中依赖裸命令的入口，固化「环境定位」红线。
- 验证 whisperx OK、CUDA 指向 `.venv`，并复测中文视频文件名在正确环境下不再乱码。
- 交付后可无缝继续「凯西大神教你」视频翻译流水线（run → 翻译 → generate → verify）。

## 范围边界

- 不修改 `src/` 业务代码，不改变转写/翻译/生成逻辑。
- PATH 清理属系统级破坏性改动，仅提供指引命令供用户自行执行，不自动修改系统环境。

## 技术栈

- 运行时：项目 `.venv`（uv 0.12.5、CPython 3.13.12、whisperx 3.8.6、torch 2.8.0+cu128）
- 脚本：PowerShell（`env.ps1` 激活脚本）
- 构建/文档：GNU Make（Makefile）、Markdown（AGENTS.md / README.md，TOOLCHAIN.md 已正确无需改）

## 根因（已核实）

1. 用户 PATH 中 `F:\Python311\Scripts`、`F:\Python311` 排在前面，项目 `.venv\Scripts` 不在 PATH 中。
2. 裸命令解析结果：`python` → `F:\Python311\python.exe`；`video-translate` → `F:\Python311\Scripts\video-translate.exe`；`uv` → `F:\Python311\Scripts\uv.exe`。
3. `make` 在此机器不存在（`CommandNotFoundException`），`make setup/doctor` 入口失效。
4. Makefile 写死 `PY := python`，`doctor/setup/test` 全部裸调用，命中旧环境。
5. README.md、AGENTS.md Phase 0 大量使用裸 `video-translate` 命令。
6. TOOLCHAIN.md 第 267–284 行已写清正确入口（`.venv\Scripts\video-translate.exe`、`. .venv\Scripts\Activate.ps1`），但执行层未遵守。

## 实现方案：入口确定化

核心原则：**所有命令入口不再依赖 PATH 解析**，改用项目根相对/绝对的 `.venv` 确定路径，或先激活 `.venv`。

- 主 CLI 入口（Windows）：`cd <repo>` 后运行 `.venv\Scripts\video-translate.exe ...`
- Python 工具入口：`.venv\Scripts\python.exe ...`
- 一键激活入口：新增 `env.ps1`，执行 `Set-Location` 到脚本所在目录 + 激活 `.venv` + 打印 `sys.executable`、whisperx、torch/CUDA 版本确认
- `uv run` 作为等价替代（依赖 PATH 中有 uv，非首选；文档中仅作备注）

### 目录结构

```
f:/workbuddy/github/video-translate/
├── env.ps1        # [NEW] 一键进入正确环境并打印版本确认
├── Makefile       # [MODIFY] PY/PIP 改为 .venv 确定入口
├── AGENTS.md      # [MODIFY] 红线表新增「环境定位」条目 + Phase 0 入口修正
└── README.md      # [MODIFY] 裸命令统一改为确定入口/激活前置说明
```

### 各文件修改要点

- **env.ps1（新增）**：`Set-Location` 到项目根、`.\.venv\Scripts\Activate.ps1` 激活、打印 python 路径与 whisperx/torch/CUDA 状态；对新窗口仅需 `. .\env.ps1` 一条命令。
- **Makefile（修改）**：定义 `VENV_PY := .venv/Scripts/python.exe`（正斜杠兼容 make 与 Windows），`PY`/`PIP` 指向它；`doctor`、`setup` 后半段、`test` 全部改用 `$(VENV_PY)`，避免裸 `python` 漂移。
- **AGENTS.md（修改）**：红线表新增「环境定位」行——禁止裸 `python`/`video-translate`/`make`，必须走 `.venv\Scripts\` 绝对路径或先激活；Phase 0 的 `make setup/doctor` 改为 `.venv` 确定入口并注明 `env.ps1`。
- **README.md（修改）**：在命令示例前统一加「先激活或使用 `.venv\Scripts\video-translate.exe`」说明，替换裸命令示例。

### 实现注意

- 不修改 `src/` 逻辑，仅改入口与文档，改动面小、可回滚（均为文本文件）。
- PATH 清理不自动执行；在交付时输出 PowerShell 指引命令（定位用户级 `Path` 环境变量并移除 `F:\Python311` 相关项），由用户确认后自行操作。
- 中文视频名乱码问题：在正确 `.venv` 入口下复测 `doctor --video` / `run`；若仍乱码再单独处理（不在本计划内擅自扩展）。
---
name: fix-environment-location
overview: 以 uv run 为唯一正统运行入口（uv 既管安装 uv sync 又管运行 uv run），把 uv 从旧环境 F:\Python311 独立到用户级，统一 AGENTS/README/Makefile 的命令入口为 uv run，可选清理 PATH 旧环境残留，然后复测并继续视频翻译流水线。
todos:
  - id: fix-agents-md
    content: 修改 AGENTS.md：红线表新增环境定位条目，Phase 0 命令入口统一改为 uv run
    status: completed
  - id: fix-readme-md
    content: 修改 README.md：裸命令示例统一改为 uv run，并补充 cd 项目根与 uv run 前缀说明
    status: completed
  - id: fix-makefile
    content: 修改 Makefile：PY/PIP 改用 uv run，doctor/setup/test 不再裸调 python
    status: completed
  - id: verify-uv-run
    content: 用 uv run 复测 doctor 确认 whisperx OK、CUDA 指向 .venv，并复测中文视频路径不再乱码
    status: completed
    dependencies:
      - fix-agents-md
      - fix-readme-md
      - fix-makefile
  - id: harden-uv-path-guide
    content: 输出 uv 独立化与 PATH 清理旧环境的可选加固指引，由用户确认后自行执行
    status: completed
    dependencies:
      - verify-uv-run
---

## 需求概述

一次性根除「环境定位漂移」问题：当前裸运行 `python` / `video-translate` / `make` 都会命中旧环境 `F:\Python311`（缺 whisperx），而项目真实环境是 `.venv`（uv 管理，whisperx 3.8.6 + torch 2.8.0+cu128，CUDA 可用）。

用户明确要求：

1. 先把这个结构性缺陷修好，不要每次重新排查环境；
2. 采用 `uv run` 作为项目运行环境的正统入口（uv 既管安装 `uv sync`，也管运行定位 `uv run`）；
3. 修好后在新窗口复测，后续继续「凯西大神教你」视频翻译流水线（run → 翻译 → generate → verify）。

## 核心目标

- 建立确定性命令入口：所有 CLI / Python / 测试命令统一走 `uv run`，不再依赖 PATH 解析。
- 修正 AGENTS.md、README.md、Makefile 中依赖裸命令的入口，固化「环境定位」红线。
- 验证 whisperx OK、CUDA 指向 `.venv`，并复测中文视频文件名在正确入口下是否仍乱码。
- 交付后可直接继续视频翻译流水线。

## 根因（已核实）

1. 用户 PATH 中 `F:\Python311\Scripts`、`F:\Python311` 排在前面，项目 `.venv\Scripts` 不在 PATH。
2. 裸命令解析：`python` → `F:\Python311\python.exe`；`video-translate` → `F:\Python311\Scripts\video-translate.exe`；`uv` → `F:\Python311\Scripts\uv.exe`。
3. `make` 在此机器不存在（`CommandNotFoundException`），`make setup/doctor` 入口失效。
4. Makefile 写死 `PY := python`，`doctor`/`setup` 后半段/`test` 均裸调用，命中旧环境。
5. README.md、AGENTS.md 大量使用裸 `video-translate` 命令。
6. TOOLCHAIN.md 已写清正确入口但执行层未遵守。

## 解决方案：以 `uv run` 为唯一正统运行入口

核心原则：**命令入口不再依赖 PATH 解析**。`uv run` 在项目目录下会自动发现并激活 `.venv`，完全无视 PATH 里的旧 python（已验证 `cd 项目根; uv run video-translate doctor` 正确输出 whisperx OK、cuda 指向 `.venv`）。

- 主 CLI 入口：`cd <repo>` 后 `uv run video-translate ...`
- Python 工具入口：`uv run python ...`
- 测试入口：`uv run pytest ...`
- 开新窗口仅需 `cd` 到项目根 + `uv run` 前缀，无需激活、无需记 `.venv` 绝对路径。

### 修改文件

```
f:/workbuddy/github/video-translate/
├── Makefile       # [MODIFY] PY/PIP 改用 uv run，doctor/setup/test 不再裸调 python
├── AGENTS.md      # [MODIFY] 红线表新增「环境定位」条目 + Phase 0 入口改 uv run
└── README.md      # [MODIFY] 裸命令示例统一改 uv run + 开头加环境定位说明
```

### 各文件修改要点

- **AGENTS.md**：红线表新增「环境定位」行——禁止裸 `python`/`video-translate`/`make`，统一 `uv run` 前缀；Phase 0 的 `make setup/doctor` 改为 `uv run video-translate setup/doctor`，并注明 `cd` 到项目根。
- **README.md**：命令示例统一改为 `uv run video-translate ...`，并在「快速开始」处加一句「先 `cd` 到项目根，命令统一加 `uv run` 前缀」。
- **Makefile**：`PY` 改为 `uv run python`，`doctor`/`test`/`setup` 后半段改用 `uv run`，保持 Makefile 正确性（即使当前机器 make 不可用）。

### 验证与后续

- 复测 `uv run video-translate doctor`：确认 whisperx OK、cuda 指向 `.venv`。
- 复测 `uv run video-translate doctor --video "videos/大神凯西教你用Meta眼镜拍Vlog.mp4"`：确认中文文件名不再乱码。
- 环境定位修好后继续视频翻译流水线（whisperx 对齐已启用，转写后必须重译）。

### 可选加固（需用户确认后自行执行，不自动改系统）

- uv 独立化：官方 installer `irm https://astral.sh/uv/install.ps1 | iex` 将 uv 安装到用户级目录，脱离 `F:\Python311`。
- 清理用户 PATH 中 `F:\Python311\Scripts`、`F:\Python311`（前提是 uv 已独立化）。
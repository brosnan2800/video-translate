# ADR-023 — 依赖管理工具链切换 pip → uv（可复现安装）

- **Status**: Accepted（已落地，E1）
- **Date**: 2026-08-25
- **关联**: `MAJOR_VERSION_PLAN.md` §3.2 R1-R7、E1、`TOOLCHAIN.md` §2.5、`docs/TOOLING.md` §1、ADR-014（CUDA wheel 依赖镜像索引）
- **落地**: `uv.lock`（已提交）、`Makefile setup` 主路径 `uv sync`（pip 兜底）、`pyproject.toml` `[tool.uv.index]` / `[tool.uv.sources]`

## 背景

`pyproject.toml` 早期只声明 `dependencies`，仓库**无 lockfile**。每次换机 / 重建环境
都用 `pip install -e .` 现场解析版本，导致两类红线事故反复出现：

1. **依赖装错环境**：本机 PATH 里其它项目的包被误解析进 venv；
2. **装成 CPU 版**：CUDA wheel 若不显式走 cu124 索引，pip 默认装 `cpu` 轮子，
   与 E4（CUDA venv 优先）直接矛盾。

`MAJOR_VERSION_PLAN.md` §3.2 已把「依赖进顶层 + lockfile 成对提交」固化为 R1/R3，
但**如何生成并固化 lockfile** 需要一次工具链决策。

## 决策

**依赖管理主工具链从 pip 切换为 uv，并提交 `uv.lock` 作为唯一事实来源。**

1. `Makefile` 的 `setup` 目标主路径 = `uv sync --extra dev`；未安装 `uv` 时打印安装
   指引并回退 `pip install -e .`（兜底，不阻塞首次体验）。
2. 提交 `uv.lock`，确保 `.gitignore` **不**排除它；任何 `pyproject` 依赖变更后必须
   重跑 `uv lock` 并**同 commit** 提交（R3）。
3. CUDA wheel 只在 `[tool.uv.index]`（清华 cu124 镜像）解析，绝不裸装（R2）；
   `[tool.uv.sources]` 按平台 marker 选 wheel（Win/Linux → cu124，macOS → cpu）。
4. 运行时依赖一律进顶层 `dependencies`，不藏 extra（R1）；dev 工具进
   `[optional-dependencies].dev`。

## 理由

- **可复现性**：lockfile 锁死传递依赖版本，换机安装结果逐字节一致，从根上消灭
  「装错环境 / 装成 CPU 版」两类事故（这正是 §3.2 规则的落地载体）。
- **速度与确定性**：uv 按 lockfile 走缓存，`update` 语义天然可「快速同步」，
  对齐 Voice-Pro 的 `update.bat` 最佳实践（`docs/RESEARCH-voice-pro.md` P0）。
- **与 E4 协同**：uv 的 cu124 索引保证 venv 内 torch 是 GPU 版，E4 才能靠
  `venv torch/lib` 自动探测到 CUDA 运行时。

## 后果

- 正面：环境 100% 可复现；`uv sync` 幂等、可离线复用缓存；新 clone 一次成功。
- 负面 / 注意：`uv` 是运行时新增工具链（非 Python 依赖）；团队需接受
  `uv.lock` 大文件进仓库。pip 兜底仅用于无 uv 的极端环境，路径可能漂移，不推荐长期依赖。
- 规范入口：依赖变更标准动作见 `docs/TOOLING.md` §1.3，与 AGENTS.md §1 红线、
  MAJOR_VERSION_PLAN §3.2 R1-R7 三处互为引用，修订须同步。

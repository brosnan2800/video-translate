# ADR-029: 命令入口确定化 — 统一 `uv run`

- 状态: 接受（实现）
- 日期: 2026-08-29
- 关联: Spec 23、Spec 20（环境就绪）、TOOLCHAIN.md、AGENTS.md Phase 0
- 上游: 一线排障实录（环境定位漂移：裸命令命中 `F:\Python311` 旧环境）

## 背景 / 问题

项目运行环境恒为 `<repo>/.venv`（uv 管理，whisperx 3.8.6 + torch 2.8.0+cu128）。
但实际执行中频繁出现：

1. **PATH 污染**：`F:\Python311\Scripts`、`F:\Python311` 排前，`.venv\Scripts`
   不在 PATH → 裸 `python` 命中 `F:\Python311\python.exe`、裸 `video-translate`
   命中 `F:\Python311\Scripts\video-translate.exe`（缺 whisperx）、裸 `uv` 也
   命中 `F:\Python311\Scripts\uv.exe`。
2. **`make` 不存在**：Windows 本机 `make` 不可用，原 `make setup` / `make doctor` 入口
   入口失效。
3. **文档/执行层用裸命令**：Makefile `PY := python`、README/AGENTS 大量裸
   `video-translate`，依赖 PATH 解析，必然漂移。
4. **后果**：`doctor` 误报 whisperx 缺失、环境被反复误判为"未安装"，排障成本高。

## 决策

### 决策 1 — `uv run` 为唯一正统运行入口

`uv` 同时承担安装（`uv sync`，uv.lock 固化）与运行定位（`uv run`，自动发现
并激活项目 `.venv`）。所有命令统一 `uv run` 前缀，不依赖 PATH、不依赖
`make`、不依赖手动激活。已实测验证：`cd <repo>; uv run video-translate doctor`
输出 whisperx `OK`、CUDA 指向 `.venv`。

### 决策 2 — `doctor` 入口自检（`resolve_command_entry`）

`toolchain.resolve_command_entry()` 判定入口来源：
`uv-run`（VIRTUAL_ENV 指向项目 `.venv`）/ `venv`（解释器在 `.venv` 内）/
`bare`（解释器在外部，漂移信号）。`doctor` 打印状态行，`bare` 附修复命令
（非致命）。路径比较经 `os.path.normcase`，跨平台不误判。

### 决策 3 — 不自动改系统 PATH（引导式清理）

清理用户 PATH 中的旧环境（`F:\Python311`）属破坏性系统改动，**不自动执行**；
仅在文档/交付说明中给出指引命令，由用户确认后自行操作。前置条件是先独立
安装 `uv`（否则清理后 `uv` 不可用）。

### 决策 4 — `uv` 为项目外一次性引导器

`uv` 装于用户级（Windows `irm https://astral.sh/uv/install.ps1 | iex` 至
`%USERPROFILE%\.local\bin`；macOS/Linux `curl -LsSf
https://astral.sh/uv/install.sh | sh`），不属于项目运行环境；不违背"环境随
项目走"原则（`.venv` / `models/` / `tools/ffmpeg` 仍在 `<repo>` 内，Spec 20）。

## 影响面

| 对象 | 变化 |
|---|---|
| `src/video_translate/toolchain.py` | 新增 `project_root` / `project_venv_dir` / `resolve_command_entry` |
| `src/video_translate/cli.py` | `doctor` 新增 `entry` 状态行（`uv-run`/`venv`/`bare`） |
| `tests/test_environment_entry.py` | 新增 TDD 覆盖 7 例 |
| `Makefile` | `PY`/`PIP` 改 `uv run`；`doctor`/`test`/`setup` 后半段经 `uv run` |
| `AGENTS.md` / `README.md` / `docs/specs/20` | 命令入口统一 `uv run`（Spec 23） |
| `docs/specs/23` | 新增规格（本决策的行为契约） |

## 不变量 / 铁律

- 运行环境恒为 `<repo>/.venv`；禁止裸 `python` / `video-translate` / `make`。
- `doctor` 全绿 = 就绪（Spec 20）；`entry` 非 `bare` 为就绪前置。
- macOS 兼容：`uv run` 跨平台一致，无 `.venv\Scripts` vs `.venv/bin` 分支需求；
  whisperx 对齐在 Mac 本就不支持，与本 ADR 无耦合。

## 测试

- `tests/test_environment_entry.py`：入口判定四分支 + 路径规范化。
- 回归：`tests/test_toolchain.py`、`tests/test_doctor.py` 全绿。

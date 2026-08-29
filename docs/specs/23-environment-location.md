# Spec 23: 环境定位与命令入口（确定性 `uv run` 入口）

- 状态: 批准（实现）
- 日期: 2026-08-29
- 关联: ADR-029、Spec 20（环境就绪）、TOOLCHAIN.md §入口、AGENTS.md Phase 0
- 修复对象: 「环境定位漂移」——裸 `python` / `video-translate` / `make` 命中
  PATH 里残留的旧全局环境（如 `F:\Python311`，缺 whisperx），而非项目 `.venv`

## 目标

把「命令从哪个环境运行」变成**确定性事实**：项目运行环境恒为
`<repo>/.venv`（uv 管理），所有命令统一经 `uv run` 启动（uv 既管安装
`uv sync`、又管运行定位 `uv run`），开新窗口只需 `cd <repo>` + `uv run`
前缀，不依赖 PATH 解析、不依赖 `make`、不依赖"记得激活 venv"。

## 根因回顾（为何需要本 Spec）

1. 用户 PATH 中 `F:\Python311\Scripts`、`F:\Python311` 排在前面，项目
   `.venv\Scripts` 不在 PATH → 裸 `python` / `video-translate` / `uv` 全部命中
   旧环境。
2. `make` 在 Windows 本机不存在 → `make setup` / `make doctor` 入口失效。
3. Makefile / README / AGENTS 曾用裸命令，依赖 PATH 解析 → 同漂移。
4. 已在旧环境验证 `uv run video-translate doctor` 能正确定位 `.venv`
   （whisperx `OK`、CUDA 指向 `.venv\...\torch\lib`）——证明 `uv run`
   是唯一正统入口。

## 行为契约

### 1. 唯一运行入口：`uv run`

| 场景 | 命令 |
|---|---|
| 主 CLI | `cd <repo> && uv run video-translate ...` |
| Python 工具 | `cd <repo> && uv run python ...` |
| 测试 | `cd <repo> && uv run pytest ...` |
| 一键就绪 | `cd <repo> && uv run video-translate setup`（等价 `uv sync` + 预拉模型） |
| 环境自检 | `cd <repo> && uv run video-translate doctor` |

- `uv run` 在项目根自动发现并激活 `<repo>/.venv`，无视 PATH 旧环境。
- 跨平台一致：Windows（`.venv\Scripts\`）与 macOS/Linux（`.venv/bin/`）
  均由 `uv run` 处理，文档无需区分平台路径。
- **禁止**裸 `python` / `video-translate` / `make`（PATH 残留旧环境时必然漂移）。

### 2. 入口自检：`doctor` 状态行

`doctor` 输出一行命令入口来源（ADR-029 / `toolchain.resolve_command_entry`）：

| entry | 含义 | doctor 呈现 |
|---|---|---|
| `uv-run` | `VIRTUAL_ENV` 指向项目 `.venv`（`uv run` / 激活均为正道） | `[OK ] entry : uv-run — project .venv (<interp>)` |
| `venv` | 解释器位于项目 `.venv` 内（无 `VIRTUAL_ENV` 标记） | `[OK ] entry : venv — project .venv (<interp>)` |
| `bare` | 解释器**不在**项目 `.venv`（旧环境 / 系统 python 遮蔽） | `[WARN] entry : BARE — interpreter NOT in project .venv` + 修复指引 |

- `bare` 为漂移信号：附修复命令 `cd <repo> && uv run video-translate ...`，
  不崩溃、不影响其余检查（非致命）。
- 判定对平台无关：路径比较经 `os.path.normcase` 规范化，Windows 大小写 /
  分隔符差异不误判。

### 3. `uv` 引导器（唯一项目外依赖）

- 环境随项目走（`.venv` / `models/` / `tools/ffmpeg` 全在 `<repo>` 内，
  Spec 20）；`uv` 本身是**一次性引导器**，类比 git，装于用户级，不属于
  项目运行环境。
- Windows：`irm https://astral.sh/uv/install.ps1 | iex`（默认
  `%USERPROFILE%\.local\bin`）；macOS/Linux：`curl -LsSf
  https://astral.sh/uv/install.sh | sh`。
- 注意：`uv` 若曾被 `pip install` 进旧 Python 环境（如 `F:\Python311\Scripts`），
  清理 PATH 旧环境前应先独立安装 `uv`（见 ADR-029 §影响）。

### 4. Makefile 语义（`uv run` 化，保证正确性）

- `PY` / `PIP` 改为 `uv run python` / `uv run python -m pip`。
- `doctor` / `test` / `setup` 后半段一律经 `uv run`，杜绝裸 `python` 漂移。
- `make` 在本机不存在时，等价入口为 `uv run`（本 Spec §1），二者不冲突。

## 范围与边界

- **IN**：入口约定（§1）、`doctor` entry 状态行（§2）、`uv` 引导说明（§3）、
  Makefile `uv run` 化（§4）。
- **OUT**：不改转写 / 翻译 / 生成业务逻辑；不自动修改系统 PATH（清理指引
  见 ADR-029 §影响，由用户确认后执行）；不改 `uv.lock` / `pyproject.toml`。

## 跨平台影响（macOS 复核）

- `uv run` 跨平台一致，Mac 同样适用；不再需要区分 `.venv\Scripts` 与
  `.venv/bin` 路径写法。
- whisperx 对齐在 macOS 本就不支持（`[gpu]` extra 已 `sys_platform !=
  'darwin'` 排除，`--align auto` 降级 `none`），与本 Spec 无耦合，行为不变。

## TDD 验收清单

- [x] `project_venv_dir()`：从模块位置推导 `<repo>/.venv`，与 CWD 无关。
- [x] `resolve_command_entry()` 四分支（tests/test_environment_entry.py）：
      VIRTUAL_ENV 指向项目 → `uv-run`；VIRTUAL_ENV 分隔符/大小写差异 → 仍
      `uv-run`；VIRTUAL_ENV 指向别处 + 解释器在项目 venv 内 → `venv`；
      解释器在外部（系统 python）→ `bare`。
- [x] `doctor` 输出 entry 状态行：`uv-run` 为 `[OK ]`，`bare` 为 `[WARN]` 并附
      修复命令。
- [x] 回归：`tests/test_toolchain.py` + `tests/test_doctor.py` 全绿。

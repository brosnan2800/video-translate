# Spec 23: 环境定位与命令入口（确定性 `uv run` 入口）

- 状态: 批准（实现）
- 日期: 2026-08-29
- 关联: ADR-029、Spec 20（环境就绪）、TOOLCHAIN.md §1.3、AGENTS.md Phase 0
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
2. `make` 在 Windows 本机不存在 → 依赖 `make setup` / `make doctor` 的入口失效（现统一为 `uv run ...`）。
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

- `uv run` 在项目根自动发现并激活 `<repo>/.venv`；但**仅对该命令已装进 `.venv`
  时才成立** —— 找不到时 uv **静默回退 PATH**（不报错），见 §2.1 的实测与守护。
- 跨平台一致：Windows（`.venv\Scripts\`）与 macOS/Linux（`.venv/bin/`）
  均由 `uv run` 处理，文档无需区分平台路径。
- **禁止**裸 `python` / `video-translate` / `make`（PATH 残留旧环境时必然漂移）。
- 本 Spec 只定**入口**；命令**语法**随宿主 shell 而定（bash vs PowerShell），
  规则见 `.codebuddy/rules/powershell-command-harness.mdc`（H0 环境判定 + H1-H5）。

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

### 2.1 测试入口硬自检（`require_project_venv`）

`doctor` 的 entry 自检只覆盖 **CLI 入口**；§1 表格里**测试**那一行（`uv run pytest`）
此前无人守，实测发生了静默漂移：

1. plain `uv sync`（**不带 `--extra dev`**，而 R3/E1 把它定为依赖变更后的**常规操作**）
   会剪掉 `[project.optional-dependencies].dev`，`pytest` 从 `.venv` 消失；
2. `uv run pytest` **静默回退 PATH**，命中系统
   `F:\Python311\Scripts\pytest.exe`；
3. 测试跑在**没有项目依赖**的解释器上，却照常报绿/报红 —— 例如新增依赖
   `youtube-transcript-api` 后，同一批用例在 venv 里过、在系统 Python 里
   `ModuleNotFoundError`，而**两次都"跑完了"**，结论完全相反。

**契约**：`toolchain.require_project_venv(action=...)` 在
`resolve_command_entry() == "bare"` 时抛 `EntryDriftError`，消息必须含**漂移的
解释器**、**项目 venv** 与**可执行的修复命令**（`uv sync --extra dev`）。
`tests/conftest.py` 的 `pytest_configure` 调用它并转成 `pytest.UsageError` ——
**测试会话直接不启动**，而不是拿另一个环境的结果当结论。

判定复用 `resolve_command_entry()`，不引入第二套逻辑；非 `bare`（`uv-run` /
`venv`）一律放行。CLI 侧仍走 §2 的非致命 `[WARN]`（用户可见即可），
**测试侧从严**（错误环境下的测试结果无意义）。

### 3. `uv` 引导器（唯一项目外依赖）

- 环境随项目走（`.venv` / `models/` / `tools/ffmpeg` 全在 `<repo>` 内，
  Spec 20）；`uv` 本身是**一次性引导器**，类比 git，装于用户级，不属于
  项目运行环境。
- Windows：`irm https://astral.sh/uv/install.ps1 | iex`（默认
  `%USERPROFILE%\.local\bin`）；macOS/Linux：`curl -LsSf
  https://astral.sh/uv/install.sh | sh`。
- 注意：`uv` 若曾被 `pip install` 进旧 Python 环境（如 `F:\Python311\Scripts`），
  清理 PATH 旧环境前应先独立安装 `uv`（见 ADR-029 §影响）。

### 4. Makefile 语义（**已废止 —— Makefile 已移除**）

> ⚠️ **本节描述的对象已不存在**：`Makefile` 已由 [ADR-030](../adr/030-control-plane.md) 移除
> （`docs/index.md` 亦已声明）。原文「`PY`/`PIP` 改 `uv run python`」「`make doctor` 的等价入口」
> 等条目**不再适用** —— 一切命令直接用 `uv run` 前缀（本 Spec §1），中间没有 `make` 这一层。
>
> 保留此节仅为解释**历史引用**：旧文档中出现的 `make setup` / `make doctor` / `make test`
> 一律等价为 `uv run video-translate setup` / `uv run video-translate doctor` / `uv run pytest`。

## 范围与边界

- **IN**：入口约定（§1）、`doctor` entry 状态行（§2）、测试入口硬自检（§2.1）、
  `uv` 引导说明（§3）；§4 为**历史说明**（Makefile 已由 ADR-030 移除）。
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
- [x] `require_project_venv()`（§2.1）：`uv-run` / `venv` 放行，`bare` 抛
      `EntryDriftError` 且消息含解释器、项目 venv 与 `uv sync --extra dev`
      修复命令；测试进程自身断言在项目 venv 内。
- [x] `tests/conftest.py::pytest_configure` 在非项目 venv 下拒绝启动会话
      （实测：系统 `pytest.exe` 直跑被拦下并打印修复指引）。
- [x] 回归：`tests/test_toolchain.py` + `tests/test_doctor.py` +
      `tests/test_environment_entry.py` 全绿。

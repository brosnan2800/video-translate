# 依赖与外部工具（Dependencies & Tools）

> 详细背景见 `TOOLCHAIN.md` 与 `MAJOR_VERSION_PLAN.md` §3.2（R1-R7）。本文件是硬约束摘要。

## 加一个 Python 依赖的规则

1. **先问：标准库 / ffmpeg / 现有依赖能不能做？** 能则不加。
2. 运行时依赖 → `pyproject` 顶层 `[project].dependencies`（参照 demucs 的先例：
   **禁止**把运行必需品藏进 extra 或只写 requirements.txt）。
3. 开发/测试依赖 → `[project.optional-dependencies].dev`（当前 pytest 就在这）。
4. GPU 专属 → `[gpu]` extra（仅 whisperx，macOS 排除；铁律：Mac 零新增依赖）。
5. 版本钉线不钉死：`torch~=2.8.0` 模式（跟随 whisperx 约束），除非有明确安全理由。
6. **`uv.lock` 与 `pyproject` 必须同一 commit 成对提交**；安装一律 `uv sync`
   （认 `[tool.uv.sources]`），**禁止裸 `pip install`**。
7. CUDA wheel 只走 `[tool.uv.index] pytorch-cu128`（`explicit = true` 保护 PyPI
   权威性——新索引条目必须保持 explicit，否则解析死锁）。

## 加一个外部二进制/模型资产的规则

- ffmpeg：`uv run video-translate setup --ffmpeg` 下载进 `tools/ffmpeg/`，
  `VT_FFMPEG_DIR` 经 `.env` 注入；**禁止**手工下载散落各盘。
- 模型：进项目根 `models/`（HF cache 重定向），**永不进 git**。
- 新二进制的代码接入模式（三段式，参照 ffmpeg 全家桶）：
  1. 命令构造纯函数（`build_*_cmd`，可单测）
  2. 执行层封装（subprocess + 错误语义 + 退出码）
  3. 输出解析纯函数（`parse_*`，合成 stderr 单测）
  4. 路径解析统一 `_resolve_binary`，禁止各处 `shutil.which`

## 禁止事项

- 禁止为绕过问题临时 `pip install` 到全局/venv 而不入 pyproject。
- 禁止在代码里硬编码绝对路径的 ffmpeg/模型路径。
- 禁止删除/绕过 `.env` 注入机制自行「全盘搜」工具。
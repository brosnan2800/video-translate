# ADR-024 — ffmpeg 自动下载便携版（消灭「全盘搜」）

- **Status**: Accepted（已落地，E2）
- **Date**: 2026-08-25
- **关联**: `MAJOR_VERSION_PLAN.md` E2、§3.2 R4、`TOOLCHAIN.md` §2.1、`docs/TOOLING.md` §2、ADR-023（uv 工具链）
- **落地**: `toolchain.py` 的 `ensure_ffmpeg(dest="tools/ffmpeg")`、`cli.py` `setup --ffmpeg`、`.env.local` 的 `VT_FFMPEG_DIR`、`doctor` 缺失提示

## 背景

旧协议 TOOLCHAIN.md §2.1 第 3 步用**「全盘搜 C:/D:/E:/F: 找 ffmpeg.exe 写回 .env」**
来定位 ffmpeg。这是全流程中不确定性最高、最容易被 Agent 执行成散落缓存的步骤：

- 搜索范围跨盘、耗时长、结果不可复现；
- 找到的可能是版本各异的手动安装包，行为漂移；
- 对小白 / 新 Agent 完全不可预知。

且 `make setup`（依赖+模型）**不覆盖 ffmpeg**，首次 `doctor` 常因 ffmpeg `[MISS]`
卡在环境就绪前。

## 决策

**引入 `ensure_ffmpeg(dest="tools/ffmpeg")`：缺失时按平台自动下载便携版，消灭「全盘搜」作为协议步骤。**

1. **先选平台再选源**（禁止跨平台复用同一包）：
   - Windows → gyan.dev release-full（zip）；
   - Linux → 静态构建（tar.xz）；
   - macOS → evermeet / 镜像（zip）。
   下载走既有 `proxy.detect_proxy` 机制。
2. `cli.py` `setup` 子命令新增 `--ffmpeg` 旗标：缺失时下载、解压至
   `tools/ffmpeg/`，并把 `VT_FFMPEG_DIR` 写入 `.env.local`（gitignore 内，机器私有）。
3. `doctor` 在 ffmpeg `[MISS]` 时输出提示行：`run: video-translate setup --ffmpeg`，
   让 Agent 看到即知跑哪条命令。
4. `TOOLCHAIN.md` 三步探测收敛为两步：① 系统 PATH → ② `setup --ffmpeg` 自动下载；
   「全盘搜」从文档删除，降级为人工兜底不再写入协议。

## 理由

- **确定性**：便携版解压到项目内 `tools/ffmpeg/`，与模型 `models/` 同理随项目、
  不进系统，版本可控、可复现。
- **小白友好**：首次 ffmpeg 缺失有确定性出路，不再依赖「恰好装过 / 恰好搜到」。
- **对齐最佳实践**：Voice-Pro `start.bat` 自拉便携 ffmpeg 已被验证（RESEARCH-voice-pro P0）。

## 后果

- 正面：环境就绪闭环（setup 全覆盖依赖+模型+ffmpeg）；`doctor` 全绿成为可达状态；
  R4（外部二进制统一 `setup` 下载、不进 git）落地。
- 负面 / 注意：首次下载依赖网络与镜像可用性；离线环境仍需手动 drop-in。
  `tools/` 进 `.gitignore`，二进制不进 git（R4）。
- 规范入口：`docs/TOOLING.md` §2；单测见 `tests/test_toolchain.py`（下载 mock / 解压 / `.env.local` 写入 / 幂等）。

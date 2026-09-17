# Spec 20 — 环境就绪：`setup` / `doctor` 行为规范（E1-E4）

Module: `toolchain.py` + `cli.py`（`setup` / `doctor` 子命令）+ `audio_profile.py`.
Decision ADR-023 / ADR-024 / ADR-025 / ADR-026 / ADR-029.
关联：`docs/TOOLING.md`、`TOOLCHAIN.md`、MAJOR_VERSION_PLAN.md §3.2 R1-R7、
Spec 23（命令入口确定化，统一 `uv run`）。

## Purpose

把「环境确定性工程」对外可观测的行为收敛为 `uv run video-translate setup`
（就绪）+ `uv run video-translate doctor`（校验）两个入口（ADR-029 / Spec 23：
命令一律经 `uv run` 启动，恒定位到项目 `.venv`，禁止裸 `make`/`python`），
保证新 clone / 小白 / 新 Agent 走**唯一确定路径**即可达到「依赖、ffmpeg、
模型、CUDA 全部就绪」的状态。**禁止自由发挥配环境**（AGENTS.md Phase 0 铁律）。

## 就绪态定义（doctor 全绿 = 就绪）

| 检查项 | 判定 | 不满足时的行为 |
|---|---|---|
| 依赖 | venv 可 `import`（uv sync 装好） | `uv run video-translate setup` 重装 |
| ffmpeg / ffprobe | `init_toolchain` 注入 PATH 后可调用（系统 PATH 或 `VT_FFMPEG_DIR`） | `[MISS]` → 提示 `run: video-translate setup --ffmpeg` |
| 模型缓存 | 项目根 `models/large-v3/model.bin` 存在 **且** ≥ 2 GiB（完整 ≈ 3.09 GB） | `[MISS]` → 提示 `run: video-translate setup` |
| CUDA 来源 | `doctor` 标注 `venv-torch` / `env` / `none` | `none` → CPU 降级（非致命） |
| 命令入口 | `doctor` 标注 `uv-run` / `venv`（ADR-029 / Spec 23） | `bare` → 提示 `cd <repo> && uv run video-translate ...`（非致命） |

## 接口契约

### `setup` 子命令（`uv run video-translate setup`）

- 主路径 `uv sync --extra dev`（pip 兜底，ADR-023）。
- 预拉模型到 `<repo>/models/`；下载前删除「存在但 < 2 GiB」的残缺 `model.bin`
  并重拉完整权重（自愈，ADR-025）。
- 新旗标 `--ffmpeg`：下载便携版到 `tools/ffmpeg/` 并写 `.env.local` 的
  `VT_FFMPEG_DIR`（ADR-024）；`ensure_ffmpeg` 按平台选源 + 走代理 + 幂等（已存在跳过）。

### `doctor` 子命令（`uv run video-translate doctor`）

> `Makefile` 已移除（[ADR-030](../adr/030-control-plane.md)）—— 原文「等价 `make doctor`」
> 的对应物已不存在，一律用 `uv run`。

- ffmpeg/ffprobe 缺失 → `[MISS]` + `run: uv run video-translate setup --ffmpeg`（**默认 exit 7**）。
- 模型 `[MISS]`（不存在或 < 2 GiB）→ `run: uv run video-translate setup`。
- CUDA 来源标注：`venv-torch` / `env` / `none`（ADR-026）。
- 全部 [OK] 即「就绪」，可进入转写 / 翻译 / 生成字幕。

### 环境变量注入（`init_toolchain`）

- ffmpeg 目录：系统 PATH → `.env.local` 的 `VT_FFMPEG_DIR`。
- CUDA 目录（ADR-026 顺序）：venv `torch/lib`（自动）→ `VT_CUDA_DIR`（显式覆盖，
  最高优先级）→ 无则 CPU 降级。

## 缓存指纹

任何影响转写/分离产物的参数（模型、VAD、`--align` 后端、人声分离、device/compute_type）
都必须纳入 chunk 缓存指纹（sha1），杜绝脏缓存复用（MAJOR_VERSION_PLAN §0.2 铁律 3，
ADR-014 / Spec 19 同约束）。

## 测试覆盖（TDD）

- `tests/test_toolchain.py`：ffmpeg 下载 mock / 解压 / `.env.local` 写入 / 幂等跳过；
  CUDA 解析顺序（显式 env 优先 / 自动探测 / 无 torch 回退）。
- `tests/test_cli_model_cache.py`：残缺缓存（< 2 GiB 假 model.bin）检测与自愈；
  模型加载失败以 `EXIT_MISSING_DEP(3)` 退出并打印修复命令。
- 以上测试**不真联网、不真下 3 GB**，用 mock / 假文件，避免污染 C 盘缓存。

# ADR-031 — 人声分离三线路由（Three-Lane Vocal Separation Routing）

- 状态：接受
- 日期：2026-08-30
- 关联：ADR-017（人声分离预处理）、ADR-021（恢复段幻觉守卫）、ADR-028（whisperx 对齐）、ADR-030（gap 人声分离）、Spec 19、Spec 22、Spec 24、Spec 25、`vocal_sep.py`、`gap_vocal_sep.py`、`cli.py`

## 背景

ADR-017 引入人声分离时，设备选择由 `_auto_device()` 决定：`torch.cuda.is_available()` 为真用 `cuda`，否则返回 `cpu`。这个"二选一"埋了两个坑。

**坑一：静默 CPU 降级。** 在 Windows 机器上，若 torch 被装成 CPU wheel（`pip install torch` 装到 `+cpu`），`torch.cuda.is_available()` 返回 False，Demucs 便悄悄落到 CPU。GPU 上几秒的窗口在 CPU 上要跑近一小时，表现**和死机完全一样**——终端只有一行输出，然后长时间无响应。

**坑二：为堵住坑一而做的一刀切，把三类机器混为一谈。** 上一轮迭代加了"无 CUDA 一律禁用 Demucs"的硬闸，堵住了静默降级，但判据是"有没有 CUDA"，于是三种本质不同的机器被塞进同一个"禁用"分支：

| 机器 | 事实 | 被一刀切的结果 |
|---|---|---|
| Windows/Linux + N 卡 | CUDA 是正道 | 正确（未就绪时禁用） |
| Apple Silicon（darwin + arm64） | **没有 CUDA，但 MPS 才是它的 GPU** | 被误当成"没显卡"永久禁用 |
| 纯 CPU（无 N 卡、非 arm64） | 确实不具备 GPU 能力 | 正确（禁用） |

**坑三：三处判据口径不一。** `cli._cuda_available()` 查 `nvidia-smi` 是否在 PATH、`vocal_sep._cuda_ready()` 查 `torch.cuda.is_available()`、`transcribe._cuda_available()` 两者兼顾。doctor 按前者报"CUDA yes"，运行时却按后者判定，于是"doctor 说有 CUDA，跑起来却说没有"的矛盾结论。

**最关键的风险（红线）**：用户的机器是 **NVIDIA 卡 + 8G 显存，硬件完全够，只是库没装/环境没初始化**。若路由只看 `torch.cuda.is_available()`，这台机器会被判成"无 CUDA"，进而可能被识别为纯 CPU 或 Apple Silicon 而转去 CPU/MPS 线。**这是绝对不允许的**——硬件是 N 卡就是 N 卡，环境没配好的正确反应是"提示去跑 `make setup`"，而不是降级。

## 决策

### 1. 三条独立运行线，互不 fall-through

| 线 | 判定 | 人声分离 | 对齐 |
|---|---|---|---|
| **CUDA 线** | `nvidia-smi` 在 PATH（Windows/Linux + N 卡） | CUDA 就绪则跑 `-d cuda`；未就绪则告警提示 `make setup` | whisperx 可用 |
| **Apple Silicon 线** | `darwin` + `arm64` | **待办（TODO）** — 告警"未实现"、跳过、回退原音轨、流程继续 | 不可用（`whisperx_available()` 在 darwin 返回 False） |
| **CPU 线** | 无 N 卡、非 arm64 | **不搞** — 告警"不支持"、跳过、回退原音轨、流程继续 | 不可用 |

三条线**完全独立**：Apple Silicon 线明确不降级到 CPU，CPU 线不假装自己能跑 GPU-only 能力。

**Apple Silicon 为何不直接接 MPS**：MPS（`torch.backends.mps`）与 CUDA 是 PyTorch 里两套独立后端，API 不同、行为不同，不能合并成一条线。更关键的是 Demucs 对 MPS 并非一等公民——社区实测 htdemucs（本项目默认模型）在 MPS 上**可能不比 CPU 快**，盲目接入等于引入一个新的"看起来在跑其实很慢"的坑。因此本期标为**待办**，把位置留空。

### 2. 单一路由函数，收敛所有判据

新增 `resolve_vsep_route()`（`vocal_sep.py`），返回不可变的 `VsepRoute`：

```python
@dataclass(frozen=True)
class VsepRoute:
    lane: str            # "cuda" | "apple_silicon" | "cpu"
    device: str | None   # "cuda" 或 None
    can_separate: bool   # 仅 lane=="cuda" 且 CUDA 就绪时为真
    message: str         # can_separate=False 时的告警文案
```

四个消费点（doctor 展示、`_vocal_sep_step` 早期闸、`separate_vocals` 硬闸、`recover_hard_gaps` 硬闸）**一律消费这个函数**，禁止任何地方再独立判断 CUDA。这同时解决坑三：doctor 与运行时共用同一真相源，不可能再给出矛盾结论。

结果带模块级缓存，避免 `_vocal_sep_step` 与 `separate_vocals` 各触发一次重量级的 `import torch`。

### 3. 判定顺序即红线保证

```
① 有 N 卡硬件？（nvidia-smi 在 PATH，不依赖 torch）
     └─ 是 → 锁死在 CUDA 线：CUDA 就绪？跑 : 告警"请跑 make setup"
② 否 → Apple Silicon？（darwin + arm64）
     └─ 是 → Apple Silicon 线（待办）
③ 否 → CPU 线（不搞）
```

**只要第一步命中，后面两步连碰都碰不到。** N 卡机器无论 torch 装没装对，永远只得到两个结果之一：`can_separate=True`（成功跑）或 `can_separate=False` + "CUDA 未就绪，请跑 make setup"（停住）。红线由判定顺序**结构性**保证，而不是靠某个 if 分支的自觉。

### 4. 删除 `_auto_device()` / `_cuda_ready()`

这两个函数是坑一和坑三的直接来源，收敛后无存在价值，一并删除，只保留作为路由内部探针的 `_torch_cuda_ready()`。

### 5. align / transcribe / config 不改动

CPU 线与 Apple Silicon 线的 `align` 自动降级 `none` **已是现状**：`transcribe._resolve_align_backend()` 的 auto 分支检查 `torch.cuda.is_available()`，`align.whisperx_available()` 在 darwin 直接返回 False。无需改动即满足"CPU 模式没有 whisperx"。

同理，`transcribe._cuda_available()`（转写设备判定）与 `cli._cuda_available()`（doctor 的 device/NVIDIA CUDA 显示）保留不动——本次只收敛**人声分离相关**的路由，blast radius 最小化。

### 6. 环境安装由 setup 自动完成，不在 run 里"停住等手动装"

`Makefile:40` 的 `make setup` 走 `uv sync`，而 `pyproject.toml` 的 `[tool.uv.sources]` 已按平台固化 torch wheel：Windows/Linux → `pytorch-cu128`（CUDA 版），macOS → `pytorch-cpu`。

因此 Windows/Linux 上 `torch.cuda.is_available()` 为 False 的**唯一原因**是环境没按规矩初始化，正确动作是提示"跑 `make setup`（uv sync 自动装 CUDA wheel）"，而不是让用户在 run 阶段手动 `pip install`。手动安装路径不在本期范围。

### 7. 非 CUDA 线收到 GPU-only flag 的行为

对 `--separate-vocals` / `--gap-vocal-sep` / `--align whisperx` 的显式请求，非 CUDA 线一律：**打印告警 → 跳过该能力 → 回退原音轨 → 流程继续**，退出码不变、不崩溃。

## 不变量

- **红线**：N 卡机器（nvidia-smi 存在）在任何情况下都不会路由到 Apple Silicon 线或 CPU 线，也绝不会以 CPU/MPS 设备执行 Demucs。
- **绝不静默降级**：`can_separate=False` 时必须打印 `route.message`，让用户看到"为什么没跑"。
- **CUDA 机器行为零变化**：CUDA 就绪时 `can_separate=True`、`device="cuda"`，与改造前路径完全一致。
- **不新增依赖**：不改 `pyproject.toml`、`align.py`、`transcribe.py`、`config.py`。
- **单一大模型串行**：Demucs 与 Whisper 的 8GB 显存调度顺序（ADR-017 §5）不变。

## 后果

- 新增 `docs/specs/25-vsep-routing.md` 与本文档：行为规范与架构理由。
- 修改 `src/video_translate/vocal_sep.py`：新增 `VsepRoute` / `resolve_vsep_route()` / 三个探针；`separate_vocals` 硬闸消费 route；删除 `_auto_device` / `_cuda_ready`。
- 修改 `src/video_translate/cli.py`：`_vocal_sep_step` 早期闸、doctor 的 demucs 行、`doctor --video` 的人声分离推荐行，全部改用 route。
- 修改 `src/video_translate/gap_vocal_sep.py`：`recover_hard_gaps` 硬闸改用 route。
- 新增 `tests/test_vsep_route.py`：三线路由单元测试。
- 修改 `tests/test_vocal_sep.py` / `test_gap_vocal_sep.py` / `test_doctor.py`：mock 适配新路由（其中 `test_gap_vocal_sep.py` 修的是上一轮硬闸引入、尚无 CUDA 机器上必然失败的测试）。

## 已知限制

- Apple Silicon 线是**待办**，不是"不支持"：MPS 接入需要实测 htdemucs 在 Apple Silicon 上的真实收益后再决定，位置已留空。
- N 卡检测依赖 `nvidia-smi` 在 PATH。若驱动装了但 PATH 不含该命令，会误判为 CPU 线（保守方向，不会误跑 Demucs）。
- Rosetta 环境下 `platform.machine()` 返回 `x86_64`（即使硬件是 Apple Silicon），此时归 CPU 线——这种情况下 torch 同样是 x86_64 构建、MPS 本就不可用，归类正确。

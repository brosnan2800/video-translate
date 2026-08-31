# Spec 25 — 人声分离三线路由（`vsep_route`）

模块：`src/video_translate/vocal_sep.py`（路由实现）。消费者：`cli.py`（`_vocal_sep_step`、doctor 展示、doctor `--video` 推荐行）、`vocal_sep.py`（`separate_vocals` 硬闸）、`gap_vocal_sep.py`（`recover_hard_gaps` 硬闸）。

规格对应 [ADR-031](adr/031-vsep-three-lane-routing.md)。

## 目的

把人声分离（Demucs）的设备选择从"有 CUDA 就跑、没有就禁用"的二选一，重构为**三条互不 fall-through 的独立运行线**，并用**单一路由函数**收敛此前散落三处、口径不一的 CUDA 判断。

核心要解决的问题：

1. **静默 CPU 降级**：torch 装成 CPU wheel 时 Demucs 悄悄落到 CPU，一个 GPU 上几秒的窗口在 CPU 上跑近一小时，表现与死机无异。
2. **一刀切误伤**：为堵住降级而把"无 CUDA"一律禁用，导致 Apple Silicon（有 MPS 但没有 CUDA）被当成"没显卡"永久禁用三类机器混为一谈。
3. **口径不一**：`cli._cuda_available()`（nvidia-smi）、`vocal_sep._cuda_ready()`（torch.cuda）、`transcribe._cuda_available()`（两者兼顾）判据不同，doctor 与运行时可给出矛盾结论。
4. **红线**：NVIDIA 卡 + 8G 显存、只是环境没初始化的机器，**绝不能**被判为纯 CPU 或 Apple Silicon 而转去 CPU/MPS。

## 模块接口

```python
@dataclass(frozen=True)
class VsepRoute:
    """人声分离路由结果（不可变）。

    lane          : "cuda" | "apple_silicon" | "cpu" — 硬件平台线
    device        : "cuda" 或 None — 传给 demucs CLI 的 -d 值；不可分离时为 None
    can_separate  : 是否允许执行 Demucs
    message       : can_separate=False 时的告警文案（can_separate=True 时为空串）
    """
    lane: str
    device: str | None
    can_separate: bool
    message: str
```

```python
def resolve_vsep_route() -> VsepRoute:
    """一次性判定人声分离走哪条线。

    判定顺序即红线保证：先判 N 卡硬件 → 再判 Apple Silicon → 最后 CPU。
    命中 N 卡后无论 torch 是否就绪都锁死在 CUDA 线，绝不落到 MPS/CPU。
    结果带模块级缓存（避免重复 import torch）。
    """
```

```python
def _is_nvidia_hardware() -> bool:
    """N 卡存在性：shutil.which("nvidia-smi") is not None。不依赖 torch。"""
```

```python
def _is_apple_silicon() -> bool:
    """sys.platform == "darwin" and platform.machine() == "arm64"。不依赖 torch。"""
```

```python
def _torch_cuda_ready() -> bool:
    """torch.cuda.is_available() and torch.cuda.device_count() > 0。
    torch 缺失 / import 失败 / 属性缺失一律返回 False，永不抛异常。
    """
```

## 判定矩阵

按顺序求值，**第一个命中即锁定**：

| 序 | 条件 | `lane` | `device` | `can_separate` | `message` |
|---|---|---|---|---|---|
| ① | N 卡 且 CUDA 就绪 | `cuda` | `"cuda"` | `True` | `""` |
| ② | N 卡 且 CUDA 未就绪 | `cuda` | `None` | `False` | 详见下方文案 C1 |
| ③ | 非 N 卡 且 darwin+arm64 | `apple_silicon` | `None` | `False` | 详见下方文案 A1 |
| ④ | 其余（无 N 卡、非 arm64） | `cpu` | `None` | `False` | 详见下方文案 P1 |

**红线由判定顺序结构性保证**：条件 ①② 命中后，③④ 永不求值。N 卡机器只可能在 ① 与 ② 之间取值，不存在落到 Apple Silicon / CPU 的路径。

### 告警文案（`message` 常量语义）

| 编号 | 场景 | 文案要点 |
|---|---|---|
| C1 | N 卡 + CUDA 未就绪 | 检测到 NVIDIA GPU 但 CUDA 未就绪 → 提示运行 `make setup`（uv sync 自动装 CUDA wheel）；强调**不会降级到 CPU** |
| A1 | Apple Silicon | Apple Silicon 人声分离为**待办（TODO）**，本期未实现；不会降级到 CPU |
| P1 | 纯 CPU | CPU 模式不支持人声分离（GPU-only 能力） |

文案必须点明"跳过分离、回退原音轨"，让用户知道流水线仍会继续。

## 消费者行为

### 1. `separate_vocals()`（vocal_sep.py 硬闸）

```python
route = resolve_vsep_route()
if not route.can_separate:
    progress(f"[vsep] ERROR: {route.message} …")
    return None          # 调用方 WARN + 回退原音轨
dev = route.device       # "cuda"
```

缓存命中**仍需在路由之前判定**：已分离的 `vocals.wav` 是 GPU 机器产出的既有产物，复用它不触发 Demucs，因此不受设备限制（ADR-031 不变量：缓存复用不受限）。

### 2. `_vocal_sep_step()`（cli.py 早期闸）

- `sep` 为假 → 直接返回 `False`（未请求）。
- `route.can_separate` 为假 → 打印 `[warn] …` + `route.message` + "回退原音轨"，返回 `False`。
- `demucs_available()` 为假 → 打印安装提示，返回 `False`。
- 否则调用 `separate_vocals()`。

### 3. `recover_hard_gaps()`（gap_vocal_sep.py 硬闸）

- 先用 `audio_source`（全局 vocals.wav）覆盖能覆盖的洞——**不触发 Demucs，不受设备限制**。
- 仍有剩余硬洞需要**本地 Demucs** 时：

```python
route = resolve_vsep_route()
if not route.can_separate:
    progress(f"[gap-vocal-sep] ERROR: {route.message} …")
    return vocals_map    # 保留已由全局 vocals 覆盖的部分，跳过本地分离
```

**只告警一次**（在循环之前），避免每个洞刷一行。

### 4. doctor 展示（cli.py）

按 `route.lane` 三线展示，而不是按"有没有 CUDA"二选一：

| lane | can_separate | doctor 输出要点 |
|---|---|---|
| `cuda` | True | `OK — CUDA GPU available (separate vocals --separate-vocals)` |
| `cuda` | False | `NVIDIA GPU detected but CUDA not ready — run: make setup` |
| `apple_silicon` | False | `Apple Silicon — vocal separation TODO (not implemented)` |
| `cpu` | False | `CPU-only — vocal separation unsupported (GPU-only)` |

`doctor --video` 的推荐行同理：**仅 `can_separate=True` 时才输出 `RECOMMENDED (--separate-vocals)`**；否则输出对应的禁用/待办说明。

### 5. 非 CUDA 线收到显式 GPU-only flag

`--separate-vocals` / `--gap-vocal-sep` / `--align whisperx` 在非 CUDA 线上：**打印告警 → 跳过该能力 → 回退原音轨 → 流程继续**，退出码不变、不崩溃。

## 不变量

1. **红线**：`nvidia-smi` 存在的机器，`lane` 恒为 `"cuda"`，`device` 只可能是 `"cuda"` 或 `None`——绝不出现 `"cpu"` / `"mps"`。
2. **绝不静默**：`can_separate=False` 时每个消费者都必须打印 `route.message`。
3. **缓存复用不受限**：命中缓存或复用全局 vocals.wav 不触发 Demucs，不受设备限制。
4. **CUDA 机器零变化**：`can_separate=True` 时路径与改造前完全一致（`device="cuda"`）。
5. **不新增依赖**：不改 `pyproject.toml` / `align.py` / `transcribe.py` / `config.py`。
6. **判定永不抛异常**：三个探针均做异常兜底，最坏情况退化为 CPU 线（保守方向）。

## 测试清单

`tests/test_vsep_route.py` 须覆盖（均通过 mock 平台与 torch 实现，不依赖真实硬件）：

1. N 卡 + CUDA 就绪 → `lane="cuda"`, `device="cuda"`, `can_separate=True`, `message=""`
2. N 卡 + CUDA 未就绪 → `lane="cuda"`, `device=None`, `can_separate=False`，文案含 setup 提示（**红线用例**）
3. darwin + arm64 → `lane="apple_silicon"`, `can_separate=False`，文案含"待办"
4. darwin + x86_64（Intel Mac）→ `lane="cpu"`（非 arm64 不归 Apple Silicon）
5. 无 N 卡 + linux → `lane="cpu"`, `can_separate=False`
6. torch 缺失 / import 失败 → 不抛异常，退化为保守结果
7. 缓存：连续两次调用只计算一次（探针调用计数不增长）

消费者测试：

- `tests/test_vocal_sep.py`：`separate_vocals` 在三条线下分别返回 `None` / 正常执行；缓存命中不受设备限制。
- `tests/test_gap_vocal_sep.py`：`recover_hard_gaps` 在三线下跳过本地 Demucs 且不刷屏；复用全局 vocals 不受设备限制。
- `tests/test_doctor.py`：doctor 在三线下输出对应行，退出码不变。

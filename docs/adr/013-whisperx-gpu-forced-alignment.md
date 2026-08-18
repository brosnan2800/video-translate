# ADR-013 — Phase 3：WhisperX 强制对齐（GPU 盒执行，Mac 不引入）

- 状态：接受（实现延后至 GPU 盒 / Windows 分支）
- 日期：2026-08-18
- 关联：ADR-008（stable-ts 拒收 → 路线 A）、ADR-011（VAD 选开）、ADR-012（声学真相修订）、Spec 18（verify 三 lane）、MAJOR_VERSION_PLAN T3（WhisperX/GPU）

## 背景
ADR-012 把「修声学层」明确拆成两件正交的事：

1. **Mac 可做的「检测 + 路由」**：`silencedetect` 独立参照 + `doctor` VAD 路由 + `verify` 声学 lane（已落地，Phase 1）。
2. **真正「修复」声学层**——强制对齐：把词级时间戳精度从 faster-whisper 的 DTW 后验（~82%）拉到 WhisperX 的 wav2vec2 强制对齐（~96%），根治 V4/V6 反复的时间戳漂移。

第 2 件在 Mac 上**不可行**，根因两条（均已在 ADR-008 / ADR-012 记录）：

- Mac 当前 Python 3.13 没有 stable-ts 2.x（带 `regroup` 的可用版）的 wheel；ADR-008 已据此回退到「路线 A：faster-whisper 原生 `word_timestamps` + 自写 `merge.py`」。
- WhisperX 依赖 **py3.12 + CUDA**（wav2vec2 对齐需要 GPU），Mac 无 CUDA；且项目锁 `faster-whisper==1.2.1` 守 ADR-001 的 CPU/int8 确定性哲学。

因此 Phase 3 的执行环境只能是 **GPU 盒（Windows，8GB RTX 3070 Ti，py3.12，CUDA 12.x）**，实现细节落在 `MAJOR_VERSION_PLAN.md` 的 **T3**。本 ADR 只固化「决策 + 边界 + 不变量」，**不重复计划的实现步骤**——它是 Phase 3 的决策留档，不是实现 spec。

## 决策
**Phase 3 = WhisperX 强制对齐，仅在 GPU 盒执行；Mac 路径永远只做「检测 + 路由」，不引入任何强制对齐依赖（守住 ADR-001 的 CPU/int8 确定性 + zero-new-dep 哲学）。**

边界：

- WhisperX 仅作为 `[windows]` extra 安装；Mac 安装（`pip install -e .`）零变化、零新依赖。
- CLI 新增 `--align {none,whisperx}`，**默认 `none`**（Mac 默认即 none，行为不变）。
- 对齐只**润词级 / 显示时间戳**，不改段落语义、顺序、文本（沿用 V4 时间戳不变量 + ADR-012 修订后不变量）。
- 缓存指纹必须含 `align` 后端（与 `device`/`compute_type` 同列，ADR-012 已要求含 device/compute_type），否则 `cpu/none` 产物会被 `cuda/whisperx` 复用、反之亦然。

## 理由
- **Mac 边界是硬约束不是偏好**：py3.13 无 stable-ts wheel、WhisperX 要 CUDA——两者都指向「修时间轴 ≠ Mac 目标」。继续在 Mac 上纠结漂移修复是方向性误判（ADR-012 已论证）。
- **精度收益确定**：wav2vec2 强制对齐 96% vs faster-whisper DTW 82%，是根治 V4/V6 漂移的唯一干净手段；但必须付出 GPU 盒的代价。
- **零回归风险**：`align=none`（默认）路径与现状字节级一致；新依赖完全隔离在 Windows extra，Mac golden 回归不受影响。

## 后果（与 MAJOR_VERSION_PLAN T3 对齐）
- `transcribe.py` 增加可选对齐钩子 `align_segments(segments, audio, language, align_backend="whisperx")`，仅当 `--align whisperx` 且库可用时调用，在 chunk 转写后、`merge.py` 处理前回写词级时间戳（T3.1）。
- CLI `transcribe` / `run` 加 `--align {none,whisperx}`（默认 none）（T3.2）。
- **优雅降级（关键）**：若用户显式 `--align whisperx` 但运行环境无该库（典型：**Mac**），CLI 必须**告警并自动回退 `none`**，绝不能崩溃或静默错用 DTW 时间戳——这与 ADR-012「Mac 只检测 + 路由」一致。
- 回退预案：若 WhisperX 与 `faster-whisper==1.2.1` 冲突，改用 `stable-ts`（py3.12 可装，对齐质量仍优于裸 faster-whisper），同样走 Windows-only extra（T3.5 / 计划 §4）。
- 依赖隔离：whisperx 仅 `[windows]` extra；若其自带更快版 faster-whisper 与 1.2.1 冲突，用 Windows 专属 venv，绝不影响 Mac。

## 已知限制
- 实现未开工（状态：接受 / 延后）。所有代码改动只在 Windows 分支 `feat/v5-cuda-windows`，Mac 主分支保持可用、不合并直至 Windows 验证通过（计划 §6）。
- 强制对齐的验收标准（计划 §5 T3）：鲍德温类漂移样本时间戳误差 < 150ms；该验收只能在 GPU 盒完成。
- Mac 上的声学层漂移只能靠 ADR-012 的「检测 + 路由」缓解（VAD 钉边界 + `verify` 声学 lane 报警），**无法根除**——根除必须 WhisperX / GPU。

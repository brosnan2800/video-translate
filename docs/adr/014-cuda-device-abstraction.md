# ADR-014 — CUDA 设备抽象（撤销 ADR-001 的硬编码禁令）

- **Status**: Accepted
- **Date**: 2026-08-19
- **关联**: ADR-001（CPU/int8 强制，本 ADR 撤销其硬编码部分）、ADR-013（WhisperX GPU 对齐，仍延后）、`MAJOR_VERSION_PLAN.md` T1

## 背景

ADR-001 把 `device="cpu"` / `compute_type="int8"` 硬编码为 `transcribe.py` 常量，
理由是 Mac 无 CUDA、8GB 卡跑 fp16 有 OOM 风险。ADR-001 的 "Revisit when" 条款
明确预留了「在专用 NVIDIA 盒上再加 opt-in `device=cuda` 路径」。

现在项目进入 Windows 部署阶段（`feat/v5-cuda-windows`，8GB RTX 3070 Ti），
正是 ADR-001 预留的触发条件。但 ADR-001 的硬编码常量导致**任何**尝试 CUDA 都必须
改源码，且 `fill_gaps.py` / `cli.py cmd_setup` 等多处直接 import 该常量，改动面散、
易漏。

## 决策

**引入 `device` / `compute_type` 两个可选参数 + `resolve_device()` 自动解析，
默认值 `"auto"`。在无 CUDA 的机器上，`"auto"` 解析为与历史完全一致的 `cpu/int8`，
Mac 产物字节级不变。**

具体：

1. 删除 `transcribe.py` 的写死常量 `DEVICE` / `COMPUTE_TYPE`，替换为
   `DEFAULT_DEVICE = "auto"` / `DEFAULT_COMPUTE_TYPE = "auto"` 与
   `resolve_device(device, compute_type) -> (device, compute_type)`：
   - `device="auto"` → 探测 `nvidia-smi`（或已安装的 `torch.cuda`）存在则 `cuda`，
     否则 `cpu`；
   - `compute_type="auto"` → cuda 时 `int8_float16`（8GB 防 OOM），cpu 时 `int8`。
2. `transcribe_video` / `transcribe_window` / `fill_gaps` 签名新增
   `device` / `compute_type`（默认 `None` → auto），透传给 `WhisperModel`。
3. `Config` 新增 `device` / `compute_type` 字段（默认 `auto`）+ env 映射
   `VT_DEVICE` / `VT_COMPUTE_TYPE`。
4. CLI 的 `transcribe` / `run` / `resegment` / `setup` 子命令加
   `--device {auto,cpu,cuda}` / `--compute-type {auto,float16,int8,int8_float16}`
   （默认 `auto`）；`cmd_doctor` 显示解析后的 device/compute_type。
5. **缓存指纹含解析后的 `device` + `compute_type`**（`transcribe_fingerprint`
   追加两参），杜绝 cpu 产物被 cuda 复用、反之亦然。

## 理由

- **零回归**：`resolve_device("auto")` 在无 CUDA 机器上恒返回 `cpu/int8`，与
  ADR-001 的历史输出字节级一致；Mac golden 回归不受影响。
- **默认仍是"安全"值**：`int8_float16` 而非 `float16`，守住 8GB 防 OOM 纪律。
- **ADR-001 的哲学保留**：默认不强制任何设备，只是把"写死常量"升级为"自动探测 +
  可显式覆盖"。CPU-only 机器的行为完全不变。

## 后果

- 正面：Windows GPU 盒开箱即用 CUDA（`device=auto` 自动命中）；Mac 完全无感；
  缓存指纹隔离 device 维度，杜绝跨设备缓存污染。
- 负面 / 注意：本 ADR **只**做设备抽象（T1），**不**引入 WhisperX 对齐（那是
  ADR-013 / T2，仍延后）。CUDA 路径的 OOM / cuDNN 冲突等风险仍在计划 §4，需在
  Windows 盒实测。
- `tools/dev/` 下的独立脚本（`step1_transcribe.py` 等）写死 `device="cpu"`，
  不属于主包、不随本 ADR 改动。

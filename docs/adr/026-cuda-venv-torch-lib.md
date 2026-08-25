# ADR-026 — CUDA DLL 解析顺序：venv 内 torch/lib 优先

- **Status**: Accepted（已落地，E4）
- **Date**: 2026-08-25
- **关联**: `MAJOR_VERSION_PLAN.md` E4、§3.2 R5、`TOOLCHAIN.md` §2.2、`docs/TOOLING.md` §4、**ADR-014**（CUDA 设备抽象，本 ADR 补充其 DLL 目录解析）
- **落地**: `toolchain.py` 的 `init_toolchain` CUDA 目录解析、`doctor` CUDA 来源标注、`.env.win.example`

## 背景

ADR-014（T1）引入了 `device`/`compute_type` 的 `auto` 解析，让 `device=auto` 在 GPU
机器上命中 `cuda`。**但 ADR-014 没有解决「CUDA DLL 目录从哪来」**——而这是 GPU 能否
真正跑起来的前提：

- 旧 `.env.win` 硬编码 `VT_CUDA_DIR=F:\win-pyvideotrans-v3.92\_internal\torch\lib`，
  即**借用外部项目 pyvideotrans 的 DLL**；
- 新机器若没装过 pyvideotrans，即使 `device=auto` 探测到 GPU，也因找不到
  `cublas64_12.dll` / `cudnn64_9.dll` 而崩溃（曾经的坑：`Could not load library
  cublas64_12.dll`）。

Voice-Pro 已验证：**PyTorch cu1xx wheel 自带完整 CUDA 运行时**（cublas/cudnn）。
本项目 venv 装了 `torchaudio>=2.5.1`（cu124），`.venv/Lib/site-packages/torch/lib`
里就有同一套 DLL。

## 决策

**CUDA DLL 目录解析顺序改为：venv 内 `torch/lib`（自动探测）→ `VT_CUDA_DIR` 显式覆盖 → 无则 CPU 降级。**

1. **venv 内 `torch/lib`（自动探测，默认）**：`import torch; torch.__file__` 定位
   venv 内 `site-packages/torch/lib`，注入为 CUDA 运行时目录。绝大多数新机器
   **不配任何环境变量**即可 GPU。
2. **`VT_CUDA_DIR` / `VT_TORCH_LIB_DIR`（显式覆盖，仍最高优先级）**：用户明确指定时
   不抢夺——仅当 venv 内 torch/lib 缺 DLL 的特殊场合手填。
3. **无则 CPU 降级**：都没有 → 静默 `cpu/int8` 降级，不崩溃。
4. `doctor` 输出 CUDA 来源标注（`venv-torch` / `env` / `none`）。
5. `.env.win.example` 移除 pyvideotrans 硬编码示例，注明「通常无需配置」。

## 理由

- **根治新机不可复现**：不再依赖「恰好装过 pyvideotrans」；CUDA 随 `make setup`
  （E1 的 cu124 wheel）一步到位。
- **与 ADR-014 协同**：ADR-014 解决「选 cuda 还是 cpu」，本 ADR 解决「cuda 时 DLL
  从哪来」，二者构成完整的 CUDA 设备抽象。
- **显式 > 自动**：遵守「用户明确指定时不抢夺」原则，覆盖路径仍可用。

## 后果

- 正面：Windows GPU 盒 `make setup` 后开箱即用 CUDA；`doctor` 来源可审计；
  Mac 无感（自动 CPU 降级，字节级不变）。
- 负面 / 注意：依赖 uv 装出 **cu124** wheel（ADR-023 的索引保证），若误装 cpu wheel
  则自动探测不到 CUDA；OOM / cuDNN 冲突风险仍在计划 §4，需 GPU 盒实测。
- 规范入口：`docs/TOOLING.md` §4；单测见 `tests/test_toolchain.py`（解析顺序：
  显式 env 优先 / 自动探测 / 无 torch 回退）。

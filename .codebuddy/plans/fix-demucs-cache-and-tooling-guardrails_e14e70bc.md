---
name: fix-demucs-cache-and-tooling-guardrails
overview: 修复 Demucs 模型缓存错误落盘（C 盘→项目内）、doctor 假绿灯、fill_gaps 离线开关顺序三个 bug，并建立"新增工具/依赖零 C 盘 + doctor 覆盖所有模型缓存检查"的 SDD/TDD 防再犯机制。
todos:
  - id: impact-analysis
    content: Use [skill:lsp-code-analysis] to locate all references to HF_HOME/TORCH_HOME/HF_HUB_CACHE/_bind_demucs_cache/_model_cached and confirm blast radius
    status: completed
  - id: adr-spec
    content: Write ADR-032 and Spec 26 documenting the three bugs' root causes, fix decisions, and anti-regression gate
    status: completed
    dependencies:
      - impact-analysis
  - id: tdd-tests
    content: Write failing tests first for cache-path binding, doctor htdemucs check, and fill_gaps offline ordering
    status: completed
    dependencies:
      - adr-spec
  - id: fix-implementation
    content: Fix vocal_sep.py cache binding, cli.py doctor check, and fill_gaps.py offline ordering to make tests pass
    status: completed
    dependencies:
      - tdd-tests
  - id: anti-regression-mechanism
    content: Generalize model-cache checker and harden MAJOR_VERSION_PLAN R6 plus TOOLCHAIN 6.2 anti-regression checklist
    status: completed
    dependencies:
      - fix-implementation
  - id: migrate-and-regression
    content: Migrate C-drive htdemucs model into project cache and run full pytest plus doctor verification
    status: completed
    dependencies:
      - fix-implementation
---

## 用户需求

制定一个 bug 修复计划，并建立可执行的防再犯机制，要求后续新增工具/依赖时严格按 SDD（Spec-Driven Development）+ TDD（Test-Driven Development）模式执行，杜绝"模型缓存落 C 盘、doctor 假绿灯、离线开关顺序错误"这类问题再犯。

## 已确认的三个 Bug

**Bug 1（根因）— Demucs 模型缓存路径绑定错误**

- 设计规范（TOOLCHAIN.md §2.4/§6、MAJOR_VERSION_PLAN.md R5）要求 htdemucs 模型落 `<repo>/models/torch/`，零 C 盘。
- 实现 `_bind_demucs_cache()` 只设 `TORCH_HOME`，但 `pyproject.toml` 钉的是 `demucs>=4.0.1`，demucs 4.x 改用 huggingface_hub 下载（honor `HF_HOME`/`HF_HUB_CACHE`），`TORCH_HOME` 无效。
- 结果：`models/torch/` 为空，模型实际落在 `C:\Users\yanglei\.cache\huggingface\hub\models--adefossez--HTDemucs\`。

**Bug 2 — doctor 假绿灯**

- doctor 只检查 demucs 包 import + CUDA lane，不检查 htdemucs 权重是否缓存、缓存位置、完整性。E3 的 `_model_cached` 只覆盖 large-v3。

**Bug 3 — fill_gaps 离线开关顺序错误**

- `recover_hard_gaps(...)` 在 `os.environ.setdefault("HF_HUB_OFFLINE", "1")` 之前执行，导致 gap-vocal-sep 加载 Demucs 时 huggingface_hub 联网 HEAD 检查 → 无代理时超时。

## 防再犯机制要求

- 落地为可执行 gate，不是只写文档。
- 新增模型/权重/工具时，必须有：项目内缓存落点 + doctor 缓存检查 + 单测覆盖。
- 严格遵循项目既有 SDD/TDD 惯例（每项改动落 ADR + Spec + 先写测试）。

## 技术栈

- 语言/框架：Python（现有项目，不改技术栈）
- 涉及模块：`vocal_sep.py`、`cli.py`、`fill_gaps.py`、`config.py`
- 测试：pytest（复用既有单测体系）
- 文档：ADR（`docs/adr/`）+ Spec（`docs/specs/`）+ 规范文档（`TOOLCHAIN.md`/`MAJOR_VERSION_PLAN.md`/`docs/TOOLING.md`）

## 实现方案

### Bug 1 修复：缓存路径绑定（核心决策）

推荐方案——引入项目内 HF 缓存目录，并在 `_bind_demucs_cache()` 中双绑定：

- 新增常量 `<repo>/models/hf`（或复用 `_DEMUCS_CACHE_DIR` 改为 `<repo>/models/torch` 之外的统一 HF 缓存）。
- `_bind_demucs_cache()` 同时设置：
- `HF_HUB_CACHE`（首选，精准控制 huggingface_hub 的 hub 缓存落盘，不影响 token 等其他 HF 状态）指向项目内；
- `TORCH_HOME`（保留，兼容 demucs 3.x 的 torch.hub 路径）。
- 执行阶段需验证当前 huggingface_hub 版本对 `HF_HUB_CACHE` 的支持；若不支持则回退用 `HF_HOME` 指向项目内，并保证与 `config.py` 的 `DEFAULT_HF_CACHE`/`hf_cache_dir`/doctor 的 `_hf_cache_dir()` 口径一致。
- 同步修正 vocal_sep.py 第 38-42 行过时注释（torch.hub → huggingface_hub 落盘差异）。

### Bug 2 修复：doctor 检查 htdemucs 模型

- 参照 `_model_cached`（cli.py 第 65 行起），新增 `_demucs_model_cached()`：检查项目内缓存目录存在 + `htdemucs.yaml` 存在 + 权重文件（`.safetensors` 或 `.pt`）大小 ≥ 保守下限（实测 safetensors ≈ 84MB，下限取 ≥ 50MB）。
- doctor 的 `checks` 列表新增一行 htdemucs 缓存状态；缺失时打印确定性修复命令（如 `make setup` 或 `setup --ffmpeg` 式指引）。

### Bug 3 修复：fill_gaps 离线顺序

- 将 `os.environ.setdefault("HF_HUB_OFFLINE", "1")`（fill_gaps.py 第 489 行）提前到 `recover_hard_gaps(...)`（第 475 行）之前，确保 gap-vocal-sep 加载 Demucs 时只用本地缓存。

### 模型迁移（一次性运维）

- 将 `C:\Users\yanglei\.cache\huggingface\hub\models--adefossez--HTDemucs` 迁移到新的项目内 HF 缓存目录（保持 `models--adefossez--HTDemucs` 结构），并在 `models/` 下形成确定性落点。

### 防再犯机制（SDD/TDD gate）

- 泛化模型缓存检查：把 `_model_cached` 抽象为支持任意模型目录 + 权重文件列表 + 大小下限的通用检查器，使未来新增模型零成本接入 doctor。
- 强化 MAJOR_VERSION_PLAN.md R6 准入清单，新增两项硬性检查：

1. 新增模型/权重必须落项目内（明确用 `TORCH_HOME` 还是 `HF_HOME`/`HF_HUB_CACHE` 绑定）；
2. 新增模型/权重必须同步新增 doctor 缓存检查 + 单测。

- TOOLCHAIN.md §6.2 补充本 bug 教训：torch.hub 与 huggingface_hub 落盘差异（`TORCH_HOME` vs `HF_HOME`/`HF_HUB_CACHE`）、离线开关必须在使用点之前设置。
- 新增回归单测锁定"零 C 盘落点"与"doctor 覆盖所有模型缓存"。

## 目录结构

```
f:/workbuddy/github/video-translate/
├── src/video_translate/
│   ├── vocal_sep.py        # [MODIFY] _bind_demucs_cache 双绑定 HF_HUB_CACHE/TORCH_HOME + 修正过时注释
│   ├── cli.py              # [MODIFY] 新增 _demucs_model_cached + doctor checks 增加 htdemucs 行 + 泛化模型缓存检查
│   ├── config.py           # [MODIFY] DEFAULT_HF_CACHE/hf_cache_dir 口径与项目内 HF 缓存目录对齐
│   └── fill_gaps.py        # [MODIFY] HF_HUB_OFFLINE 设置提前到 gap_vocal_sep 之前
├── tests/
│   ├── test_vocal_sep.py       # [MODIFY] 新增缓存落点绑定测试（HF_HUB_CACHE/TORCH_HOME 指向项目内）
│   ├── test_doctor.py          # [MODIFY] 新增 htdemucs 缓存检查测试（存在/缺失/残缺三态）
│   ├── test_gap_vocal_sep.py   # [MODIFY] 新增离线环境加载路径测试
│   └── test_fill_gaps_bare.py  # [MODIFY] 回归：HF_HUB_OFFLINE 在 gap_vocal_sep 前设置
├── docs/
│   ├── adr/032-demucs-model-cache-locality.md   # [NEW] 三 bug 根因与修复决策 + 防再犯 gate
│   └── specs/26-demucs-model-cache.md           # [NEW] doctor 检查行为 + 项目内缓存契约
├── MAJOR_VERSION_PLAN.md   # [MODIFY] R6 准入清单增加模型缓存落点 + doctor 覆盖两项
├── TOOLCHAIN.md            # [MODIFY] §2.4/§6.2 补充 torch.hub vs huggingface_hub 落盘差异教训
└── docs/TOOLING.md         # [MODIFY] E1-E4 专册同步 Demucs 模型缓存口径
```

## 关键接口（新增/修改）

- `_bind_demucs_cache()`：绑定 `HF_HUB_CACHE`（首选）与 `TORCH_HOME` 到项目内目录，幂等。
- `_demucs_model_cached() -> tuple[bool, str]`：返回（是否缓存完整，缓存路径或缺失原因），供 doctor 输出。
- `_model_cached(model_name, *, weight_files, min_bytes)`：由现有 large-v3 专用版本泛化而来，保持向后兼容。

## 推荐扩展

- **lsp-code-analysis**
- 用途：在影响分析阶段，精确追踪 `_bind_demucs_cache`、`_model_cached`、`HF_HOME`、`TORCH_HOME`、`HF_HUB_CACHE` 的全部定义/引用点，确认改动 blast radius（尤其确认改 HF 缓存绑定不会破坏 faster-whisper large-v3 的既有项目内加载）。
- 预期结果：产出一份引用清单与影响面结论，确保修复不引入回归。
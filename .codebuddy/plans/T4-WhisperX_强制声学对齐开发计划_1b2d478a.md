---
name: T4-WhisperX 强制声学对齐开发计划
overview: 按 SDD+TDD 模式落地 MAJOR_VERSION_PLAN §T4 / ADR-013：新增 `--align {none,whisperx}` 词级强制对齐（GPU 专享、Mac 优雅降级），只润词级时间戳不动文本与断句，对齐作为独立 pass 带独立缓存层，产出 ADR-028 + Spec 22 + 全绿单测 + 文档同步。
todos:
  - id: write-adr-spec
    content: 使用 [subagent:code-explorer] 摸清 words 消费链与调用点，撰写 ADR-028 + Spec 22
    status: pending
  - id: gpu-extra-deps
    content: pyproject 新增 gpu extra + uv.lock 同步 + doctor 探针与用例
    status: pending
    dependencies:
      - write-adr-spec
  - id: tdd-red-tests
    content: 编写 tests/test_align.py 红灯套件（降级/守卫/双轨缓存/指纹 golden）
    status: pending
    dependencies:
      - write-adr-spec
  - id: align-module
    content: 实现 align.py：可用性探测、align_segments 守卫回写、显存释放
    status: pending
    dependencies:
      - tdd-red-tests
  - id: transcribe-cli-wiring
    content: 使用 [skill:lsp-code-analysis] 校验插入点，贯通 transcribe 两阶段+双轨缓存+CLI/config/run 转发+Mac 降级
    status: pending
    dependencies:
      - align-module
  - id: regression-docs-acceptance
    content: 全量 pytest 回归 + 文档同步 + GPU 盒鲍德温样本验收（verify 声学 lane
    status: pending
    dependencies:
      - transcribe-cli-wiring
---

## Product Overview

T4 阶段开发计划：为视频字幕流水线引入**词级强制声学对齐**能力（可选升级项），解决极端语速下 faster-whisper DTW 词戳漂移（精度约 82% → 96%+），按项目既有 SDD+TDD 流程执行（先 ADR-028 + Spec 22 评审，再红灯测试，再实现，最后回归与验收）。

## Core Features

- 新增 `--align {none,whisperx}` 开关（transcribe / run 命令 + `VT_ALIGN` 环境变量 + toml 配置），**默认 none，行为与现状完全一致**（零回归）
- 仅 Windows/Linux GPU 环境生效；Mac 或未安装库时**显式告警并自动降级回 none**，绝不崩溃
- 对齐只回写**词级时间戳**（words[].start/end），文本内容、断句分组、段落结构绝对不变
- 缓存按对齐模式隔离：切换 `--align` 不误用脏缓存、不重复转写（复用既有转写缓存，只重跑轻量对齐）
- 分步执行守住 8GB 显存红线：转写与对齐不同时驻留 GPU
- 验收：鲍德温类漂移样本时间戳误差 < 150ms，用 `verify --video` 声学 lane 量化（GPU 盒执行）

## Tech Stack

- **既有栈（不动）**：Python + `faster-whisper==1.2.1`（钉死，转写核心不变）+ torch/torchaudio（核心依赖，GPU 盒 cu124 wheel）+ pytest（Mac 可全量 mock 开发）
- **新增**：`whisperx>=3.3` 进 `[project.optional-dependencies].gpu` extra（`sys_platform != 'darwin'` marker，Mac `uv sync` 零新增）；**只用其对齐子集**（`load_align_model` + `align`），绝不换转写核心
- **音频载入**：`torchaudio.load`（既有核心依赖）产出 whisperx 所需 16kHz mono float32，不引入新音频处理代码

## Implementation Approach

**两阶段执行 + 双轨缓存**（核心决策，基于代码事实）：

`transcribe_video`（transcribe.py L239-369）拆成两阶段：

1. **Phase 1（代码零改动）**：现有 chunk 循环照旧，chunk json 落盘于历史指纹 `fp_raw` 下——align 维度缺省不进 payload，历史哈希字节级兼容（沿用 `separate_vocals=False` 先例 L313-318）。
2. **Phase 2（新增，仅 align=whisperx 且库可用）**：释放 Whisper 模型 + `gc` + `torch.cuda.empty_cache()`（T2 样例 cli.py L468-478）后逐 chunk 处理：命中 `fp_align` 对齐缓存直接复用；否则载入 `fp_raw` 原始 chunk → `extract_chunk` 重切 wav（确定性同边界）→ `whisperx.align` → 守卫校验后回写词戳 → 存 `fp_align` 缓存。最终 merge 出 segments_en.json。

**关键决策理由**：

- **分步执行**：风险表 §6 的 8GB OOM 对策；Whisper（~3GB+）与 wav2vec2（0.4-1.3GB）不同驻留，峰值 VRAM = max(两者) ≈ 4-5GB
- **双轨缓存**：对齐不重转写——切换 align 模式复用 `fp_raw` 转写缓存只重跑轻量 pass（wav2vec2 对齐远快于转写）；`fp_align = transcribe_fingerprint(..., align_backend="whisperx")` 满足 ADR-013「指纹含 align 与 device/compute_type 同列」要求
- **断点续跑粒度**：对齐缓存逐 chunk 落盘，崩溃恢复不丢已转写 chunk（铁律 3）
- **音源一致性**：Phase 2 对齐音源 = `_extract_src`（原片或 vocals.wav），与 Whisper 实际听到的完全一致，`--separate-vocals` 组合语义正确

**不变量守卫（铁律 1，最关键的安全网）**：`align_segments` 包装层强制 1:1 映射回自有 segment/words 结构——逐段校验词数一致且词文本（大小写/标点不敏感）对齐，通过才回写词戳；任一不匹配 → 该段保留原 DTW 词戳并计数告警。段 start/end/text/分组绝不动，merge.py（`_emit` 词界收紧 L310-335、`_split_by_gap`/`_split_by_length`、`snap_drifted_words`）与 generate/translate **零改动**，words 时间戳全链路自动受益。

## Implementation Notes

- **指纹 golden**：align=none 时哈希与现状字节一致，用 `tests/test_transcribe_contract.py` 既有 golden 模式保护
- **语言解析链**：显式 `--lang` > Phase 1 首次转写时从 `_info.language` 落盘边车 `{base}.{fp_raw}.lang` > 无法确定（存量缓存全命中且未显式指定）→ 告警降级 none 并提示重转或显式传参
- **fill_gaps 交互**：audit/补洞（cli.py L548-557）发生在对齐之后，恢复段保留 DTW 词戳——已知限制记入 Spec 22（恢复段通常极短，`snap_drifted_words` 兜底）
- **依赖准入（R6 五项全过）**：跨平台降级 ✓（marker + 探测）/ 指纹 ✓（fp_align）/ 显存 ✓（分步）/ 等价评估 ✓（whisperx 封装了语言→模型映射与稳健分词，自研 wav2vec2+forced_align 成本高）/ lockfile ✓（R3：pyproject 变更同 commit 重跑 `uv lock`）
- **版本冲突预案**（风险表）：只用 whisperx 对齐 API 与转写核心无耦合；若依赖解析冲突 → 钉 whisperx 兼容版本 → 终极回退仅引 transformers wav2vec2 + `torchaudio.functional.forced_align` 自实现（ADR-028 记录降级阶梯）
- **日志**：对齐完成输出一行统计（chunk 数 / 词戳回写率 / 守卫回退数），不打印大 payload
- **CLI 降级路径**：`--align whisperx` 但 `whisperx_available()` 为假（典型 Mac）→ 打印告警 + 回退 none（ADR-013 强制）；`run` 命令经子进程转发透传（现有 L707-711 模式）

## Architecture Design

```mermaid
flowchart TD
    A[CLI: transcribe/run --align] --> B{align==whisperx 且 whisperx_available?}
    B -- 否/Mac -->|告警+降级| C[align=none 历史路径]
    B -- 是 --> D[Phase 1: chunk 转写循环<br/>缓存落盘 fp_raw 现状不变]
    D --> E[释放 Whisper + empty_cache]
    E --> F[Phase 2: 逐 chunk 对齐<br/>命中 fp_align 缓存直接用]
    F --> G{fp_raw chunk 存在?}
    G -- 是 --> H[重切 wav → whisperx.align → 守卫校验 → 回写词戳]
    G -- 否 --> H2[新转写 → 对齐 → 存双轨缓存]
    H --> I[存 fp_align 对齐缓存]
    H2 --> I
    I --> J[merge_chunks → segments_en.json]
    C --> K[merge → segments_en.json 历史路径]
    J --> L[merge.py 词界收紧/断句<br/>words 自动受益 零改动]
    K --> L
```

## Directory Structure

```
video-translate/（f:/workbuddy/github/video-translate）
├── docs/adr/028-whisperx-forced-alignment.md      # [NEW] 实现级 ADR：两阶段+双轨缓存、不变量守卫、R6 准入记录、版本冲突降级阶梯（ADR-013 战略决策的实现补齐）
├── docs/specs/22-forced-alignment.md              # [NEW] Spec：CLI 契约、缓存语义、降级矩阵、语言解析链、TDD 测试清单（照 Spec 21 格式）
├── src/video_translate/align.py                   # [NEW] 对齐模块（照 vocal_sep.py 模式）：whisperx_available 探测、align_segments 词戳回写+守卫、release_align_model 显存释放
├── src/video_translate/transcribe.py              # [MODIFY] transcribe_fingerprint 增 align_backend 维度（缺省省略键）；transcribe_video 增 Phase 2 对齐循环+双轨缓存查找+lang 边车+转写后 VRAM 释放
├── src/video_translate/cli.py                     # [MODIFY] transcribe/run 增 --align 旗标；cmd_run 子进程转发；不可用时告警降级；doctor 增 whisperx 探针行（照 demucs 样例 L252-259）
├── src/video_translate/config.py                  # [MODIFY] Config.align 字段（默认 none）+ env_map VT_ALIGN + toml [transcribe].align
├── pyproject.toml                                 # [MODIFY] [project.optional-dependencies] 增 gpu = ["whisperx>=3.3; sys_platform != 'darwin'"]
├── uv.lock                                        # [MODIFY] 同 commit 重跑（R3 铁律）
├── tests/test_align.py                            # [NEW] TDD 套件：可用性降级/守卫回退/双轨缓存/指纹 golden/CLI 旗标（照 test_vocal_sep.py 13 条 mock 模式）
├── tests/test_transcribe_contract.py              # [MODIFY] 追加指纹 golden（align=none 字节兼容）与 Phase 2 缓存复用用例
├── tests/test_doctor.py                           # [MODIFY] 追加 doctor whisperx 探针用例
├── TOOLCHAIN.md                                   # [MODIFY] gpu extra 安装说明 + 依赖矩阵 T4 行
├── README.md                                      # [MODIFY] --align 参数文档
└── MAJOR_VERSION_PLAN.md                          # [MODIFY] T4 状态推进 + ADR-028/Spec 22 引用登记
```

## Key Code Structures

```python
# src/video_translate/align.py — 核心接口（照 vocal_sep.py 模式）
ALIGN_BACKENDS = ("none", "whisperx")

def whisperx_available() -> bool:
    """import 探测，任何异常返回 False，绝不崩溃。"""

def align_segments(
    segments: list[dict[str, Any]],   # chunk 局部时间轴段落（含 words，DTW 词戳）
    audio: str,                        # chunk wav 路径（torchaudio.load → 16k mono float32）
    language: str,
    *, align_backend: str = "whisperx",
) -> tuple[list[dict[str, Any]], "AlignStats"]:
    """whisperx.load_align_model + whisperx.align → 守卫校验 1:1 → 仅回写 words[].start/end。
    词数/词文本不匹配的段落保留原 DTW 词戳并计入 stats；语言无对齐模型 → 抛可捕获降级信号。"""

def release_align_model() -> None:
    """del 对齐模型 + gc.collect() + torch.cuda.empty_cache()（T2 样例）。"""
```

```python
# src/video_translate/transcribe.py — 指纹与函数签名增量
def transcribe_fingerprint(..., align_backend: str = "none") -> str:
    # align_backend != "none" 时 payload["align"] = align_backend；缺省省略键 → 历史哈希不变

def transcribe_video(..., align_backend: str = "none") -> str:
    # Phase 1 循环原样；Phase 2（仅 whisperx 可用且非 none）：fp_raw 复用转写 + fp_align 对齐缓存
```

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose：撰写 ADR-028/Spec 22 前，系统性摸清 `words[]` 全链路消费方（merge/generate/verify/backfill）、`run → transcribe` 子进程转发链、以及既有 chunk 缓存续跑语义，确保决策文档基于完整事实
- Expected outcome：产出 words 消费方清单与调用链摘要，ADR-028 的「零改动论证」与 Spec 22 测试清单有据可依

### Skill

- **lsp-code-analysis**
- Purpose：实现贯通阶段（transcribe_video Phase 2 插入、CLI 接线）前，用语义导航确认 `transcribe_video` 全部调用点（cli.py cmd_transcribe/cmd_run/resegment）、`_seg_to_dict` 词戳结构定义与调用层级，避免遗漏插入点
- Expected outcome：所有调用点与插入位置清单，Phase 2 改动不破坏 resegment 等旁路调用方
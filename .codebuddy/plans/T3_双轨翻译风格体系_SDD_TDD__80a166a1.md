---
name: T3 双轨翻译风格体系（SDD+TDD）
overview: 按 SDD+TDD 模式落地 T3 双轨翻译风格体系：先写 ADR-027 + Spec 21 定义架构决策与对外行为，再以测试先行实现 --style {film,literal,bilingual_study} 三轨 Persona 矩阵与双轨字幕输出，全程向后兼容现有单轨工作流。
todos:
  - id: write-adr-027
    content: 撰写 ADR-027 翻译风格三轨决策文档（persona 矩阵、优先级、兼容性、缓存指纹）
    status: completed
  - id: write-spec-21
    content: 撰写 Spec 21 翻译风格行为规范（CLI 契约、task v3、文件命名矩阵、测试清单）
    status: completed
    dependencies:
      - write-adr-027
  - id: write-tests
    content: TDD 红灯：编写 style 解析、task 注入、双轨输出命名三类测试
    status: completed
    dependencies:
      - write-spec-21
  - id: implement-core
    content: 实现 config.py STYLE_PERSONAS 矩阵与 translate.py task v3 style 注入
    status: completed
    dependencies:
      - write-tests
  - id: implement-cli-generate
    content: 实现 cli.py --style 参数贯通与 generate.py 双轨后缀输出
    status: completed
    dependencies:
      - implement-core
  - id: regression-docs
    content: 全量 pytest 回归 + --skip transcribe 双轨冒烟 + AGENTS/README/specs 文档同步
    status: completed
    dependencies:
      - implement-cli-generate
---

## 用户需求

按照项目 SDD + TDD 铁律，为 **T3 双轨翻译风格体系**（MAJOR_VERSION_PLAN.md 里程碑 4 第一步，E1-E4 已完成后的下一个任务）先行产出架构文档并制定实施计划，供用户审阅后再动手实现。

## 产品概述

为视频双语字幕翻译引入**三轨翻译风格**，解决“翻译风格单一”问题：

- **film（影视二创，默认）**：信达雅 + 口语感，短句节奏优先，文化梗意译，限制单行字数
- **literal（忠实直译）**：严谨对齐原文主谓宾，保留学术/专业修饰与逻辑从句，专有名词严格忠实
- **bilingual_study（双语精读）**：直译为主，生僻词/熟词生义以括号追加注记

## 核心功能

1. CLI 新增 `--style {film,literal,bilingual_study}`（含 `VT_STYLE` 环境变量与 toml `[translate] style` 配置），注入 `translate_task.json` 的 `persona` 与 `guidelines`
2. 支持双轨输出：`--style film,literal` 一次生成两套独立字幕（`<base>.film.bilingual.srt` 与 `<base>.literal.bilingual.srt`），供创作者对比选优
3. 严格向后兼容：默认单轨（film）时，task/zh/srt 文件名与现有工作流完全一致，不破坏剪映导入与既有测试

## 交付边界

- 本计划第一阶段产出 **ADR-027 + Spec 21** 两份 SDD 文档（含全部设计决策与测试清单）
- 随后按 TDD 顺序实现并全量回归，文档随代码同步提交

## Tech Stack

- **纯 Python 标准实现**：无新第三方依赖（R6 准入清单天然满足），跨平台
- 修改范围：`config.py`（风格矩阵与解析）、`translate.py`（task schema v3）、`cli.py`（参数接入）、`generate.py`（双轨输出命名）
- 测试：`pytest`，遵循现有 mock/临时文件模式，不联网

## 一、ADR-027 核心决策（`docs/adr/027-translation-style-tracks.md` [NEW]）

按 ADR-026 文档格式（Status/Date/关联/落地 → 背景 → 决策 → 理由 → 后果）：

1. **三轨 Persona 矩阵**：`STYLE_PERSONAS: dict[str, StyleDef]`，每个条目含 `persona`（人设文本）与 `guidelines`（差异化翻译守则）。默认 `film`（等价当前 `DEFAULT_PERSONA` + 现行 guidelines，字节级兼容）
2. **style 解析顺序**：CLI `--style` > `VT_STYLE` > toml `[translate].style` > 默认 `film`（完全复用 `resolve_config` 既有三级覆盖模式，Spec 06）
3. **与既有 persona 的优先级**：显式 `--persona` / `VT_PERSONA` **覆盖** style 基底（用户自定义优先，运行时打印一行提示“custom persona overrides style=film”）；`source` / `glossary` 继续按 `build_persona` 既有顺序叠加，不受 style 影响
4. **双轨输出语义**：`--style` 支持逗号分隔多值（如 `film,literal`）：

- 多 style 时：task 文件按风格分文件（`<base>.film.translate_task.json`、`<base>.literal.translate_task.json`），Agent 各译一份（`<base>.film.zh_segments.json`），Exit 6 挂起时打印每个 task 的指令块
- 单 style（默认）：文件名保持现状（`<base>.translate_task.json`），**零破坏向后兼容**

5. **task schema version 2 → 3**：新增顶层 `style` 字段（可审计），agent 端无破坏性变更（多余字段向后读安全）
6. **缓存指纹**：style 只影响翻译文本，不影响转写产物（音频 → segments_en），**不进 chunk 缓存指纹**（铁律 3 不触发），`--skip transcribe` 复用缓存换风格重译完全合法
7. **声学不变量**：翻译风格只改文本，绝不触碰时间戳（铁律 1），`verify` 三 Lane 无需感知 style

## 二、Spec 21 接口契约（`docs/specs/21-translation-styles.md` [NEW]）

按 Spec 20 文档格式（Module/Decision → Purpose → 接口契约 → 缓存指纹 → 测试覆盖）：

| 命令 | 新参数 | 行为 |
| --- | --- | --- |
| `run` / `translate` / `backfill` | `--style {film,literal,bilingual_study}`（可逗号多值，默认走 config） | 经 `resolve_config` 注入 `prepare_translate_task(style=...)` |
| `generate` | `--style <name>`（可选） | 输出文件名加后缀：`<base>.<style>.bilingual.srt` 等 4 产物；`generate_opts.json` 记录 style；不传则文件名与现状完全一致 |


**文件命名矩阵**（Spec 核心）：

| 场景 | translate_task | zh_segments | 字幕输出 |
| --- | --- | --- | --- |
| 单轨（默认 film） | `<base>.translate_task.json` | `<base>.zh_segments.json` | `<base>.bilingual.srt` |
| 双轨 `--style film,literal` | `<base>.film.translate_task.json` 等 | `<base>.film.zh_segments.json` 等 | `<base>.film.bilingual.srt` 等 |


**双轨状态机**（扩展 AGENTS.md Phase 1/2）：`run --style film,literal` → Exit 6 挂起并列出 N 个 task → Agent 逐个翻译 → 对每个风格分别执行 `generate --style <name>`；`run --skip transcribe --style literal` 可随时为已转写视频补一条风格轨。

## 三、实现方案（TDD 顺序）

### Step 1 — SDD 文档先行

撰写 ADR-027 与 Spec 21（上述内容），无代码变更。

### Step 2 — 测试先行（红）

- `tests/test_config.py` 追加：style 默认 `film`；`VT_STYLE` 环境变量；toml `[translate] style`；CLI 覆盖优先级；非法值 `ValueError`
- `tests/test_translate_style.py` [NEW]：`STYLE_PERSONAS` 三键存在且 persona/guidelines 非空；`prepare_translate_task(style="literal")` 写入 `version==3` + `style=="literal"` + literal persona；三轨 guidelines 互不相同；无 style 参数时 persona 与现行 `DEFAULT_PERSONA` 字节一致（兼容 golden）
- `tests/test_generate.py` 追加：`generate_subtitles(style="film")` 产出 `<base>.film.bilingual.srt` 等 4 文件；无 style 产物名不变（golden）；`_vN` 递增与 `_prune_old_versions` 对带 style 后缀的 stem 正常工作
- CLI 集成：`run --style literal` 参数贯通至 task 文件（mock 转写）

### Step 3 — 实现（绿）

- `src/video_translate/config.py`：`STYLE_PERSONAS` 矩阵（含现行 persona/guidelines 收编为 `film` 轨）；`Config.style = "film"`；`env_map` 增加 `VT_STYLE`；`_coerce_env` 校验合法值；显式 persona 时提示覆盖
- `src/video_translate/translate.py`：`TRANSLATION_GUIDELINES` 重构为按 style 取用；`prepare_translate_task(..., style="film")` 写 `version: 3` + `style` 字段，persona 取 `STYLE_PERSONAS[style].persona`（显式传入的 persona 参数优先）
- `src/video_translate/cli.py`：`run`/`translate`/`backfill` 加 `--style`；`cmd_run` 多 style 时循环出 task 并扩展 `_RUN_AWAITING_AGENT_INSTRUCTIONS` 打印每轨指令；`generate` 加 `--style` 传递至 `generate_subtitles`
- `src/video_translate/generate.py`：`generate_subtitles(..., style=None)`；`style` 非空时 `out_base = f"{base}.{style}"`（复用 `_resolve_out_base`/版本递增/prune，正则以完整 out_base 为锚天然兼容）；`generate_opts.json` 记录 style

### Step 4 — 回归与文档同步

- 全量 `pytest` 绿；用现有已译视频（如 jimmy）做一次 `--skip transcribe --style literal` 冒烟验证双轨产物
- 文档同步：`AGENTS.md`（Phase 1/2 状态机补 style 用法）、`docs/specs/00-overview.md`、`03-translate.md`、`04-generate-srt.md`、`09-agent-translate.md`、`README.md`、`MAJOR_VERSION_PLAN.md`（T3 标记完成 + §7 验收对照）

## 四、性能与风险控制

- **零热路径开销**：style 仅在 task 生成与文件命名阶段生效，转写/翻译/verify 主流程无额外 I/O
- **爆炸半径**：默认路径字节级不变（golden 测试保护）；唯一行为分支在显式传 `--style` 时才激活
- **无依赖变更**：不触碰 `pyproject.toml`/`uv.lock`（R3 不触发）；不进 git 的二进制资产（R4/R5 不触发）

## 五、目录结构

```
video-translate/
├── docs/adr/027-translation-style-tracks.md    # [NEW] ADR：三轨风格决策
├── docs/specs/21-translation-styles.md          # [NEW] Spec：--style 行为契约
├── src/video_translate/
│   ├── config.py                                # [MODIFY] STYLE_PERSONAS + Config.style + VT_STYLE
│   ├── translate.py                             # [MODIFY] task v3 + style 注入
│   ├── cli.py                                   # [MODIFY] --style 参数 + 多轨挂起指令
│   └── generate.py                              # [MODIFY] style 后缀输出
├── tests/
│   ├── test_translate_style.py                  # [NEW] 三轨 task 注入测试
│   ├── test_config.py                           # [MODIFY] 追加 style 解析用例
│   └── test_generate.py                         # [MODIFY] 追加双轨命名用例
├── AGENTS.md / README.md / MAJOR_VERSION_PLAN.md / docs/specs/{00,03,04,09}*.md  # [MODIFY] 文档同步
└── docs/HISTORY.md                              # [MODIFY] 记录 T3 落地
```
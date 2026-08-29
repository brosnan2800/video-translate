# ADR-027 — 双轨翻译风格体系（Translation Style Tracks）

- **Status**: Accepted
- **Date**: 2026-08-28
- **关联**: Spec 21（`docs/specs/21-translation-styles.md`）；MAJOR_VERSION_PLAN.md §T3；Spec 06（配置解析层级）；ADR-012（声学时间戳不变量）
- **落地**: T3 第一步，纯逻辑 + Prompt 体系，无新依赖（R6 准入清单天然满足）

---

## 背景

当前 `translate` 链路只支持单一 `DEFAULT_PERSONA`（影视口语意译基调，见 `config.py`
L30-33）。所有视频——无论电影、学术公开课还是技术文档——共用同一套人设与翻译守则，
导致：

- 学术 / 法律 / 技术类内容被"意译"掉限定条件，信息失真；
- 需要逐句精读的材料缺乏术语注记；
- 创作者无法在同一转写结果上对比"意译 vs 直译"的取舍。

MAJOR_VERSION_PLAN.md §T3 要求引入**三轨翻译风格**，并以 SDD + TDD 模式先于实现落地。

---

## 决策

### 1. 三轨 Persona 矩阵

引入 `STYLE_PERSONAS: dict[str, StyleDef]`，每个条目含 `persona`（人设文本）与
`guidelines`（差异化翻译守则）：

| style | 定位 | persona 基调 | guidelines 重点 |
|---|---|---|---|
| `film` | 影视二创（**默认**） | 资深中英字幕译者，信达雅 + 口语感，文化梗意译 | 短句节奏、保留语气情绪、限单行字数、诗歌/歌词靠 `source` 引导 |
| `literal` | 忠实直译 | 严谨技术译者，主谓宾对齐原文 | 保留学术修饰与逻辑从句、专有名词严格忠实、不增删限定 |
| `bilingual_study` | 双语精读 | 教学型译者，直译为主 | 生僻词 / 熟词生义以括号追加注记、句式结构可回映原文 |

**默认 `film` 等价于当前 `DEFAULT_PERSONA` + 现行 `TRANSLATION_GUIDELINES`**（字节级兼容，
见 Spec 21 测试清单 golden 项）。`DEFAULT_PERSONA` 保留为 `film` 轨的基础，逐步收编进矩阵。

### 2. style 解析顺序

完全复用 `resolve_config` 既有三级覆盖（Spec 06）：

```
CLI --style  >  VT_STYLE  >  toml [translate].style  >  默认 film
```

### 3. 与既有 persona 的优先级

- 显式 `--persona` / `VT_PERSONA` **覆盖** style 基底（用户自定义优先）；
  运行时打印一行提示：`custom persona overrides style=<film|...>`。
- `source` / `glossary` 继续按 `build_persona` 既有顺序叠加，**不受 style 影响**。
- **诗歌 / 歌词**：归入 `film` 轨，靠 `source` 字段（如"诗歌，需押韵与意象还原"）引导
  Agent；**不单列 `poetic` 预设**（按需扩展原则，待样本驱动再评估）。

### 4. 双轨输出语义

`--style` 支持逗号分隔多值（如 `film,literal`）：

- **多 style**：task 文件按风格分文件
  （`<base>.film.translate_task.json` / `<base>.literal.translate_task.json`），
  Agent 各译一份（`<base>.film.zh_segments.json`），Exit 6 挂起时 `_RUN_AWAITING_AGENT_INSTRUCTIONS`
  打印每个 task 的指令块。
- **单 style（默认 film）**：文件名保持现状（`<base>.translate_task.json`），
  **零破坏向后兼容**。

### 5. task schema version 2 → 3

`prepare_translate_task` 写入产物新增顶层 `style` 字段（可审计）。Agent 端为只读消费，
多余字段向后读安全，无破坏性变更。

### 6. 缓存指纹

style 只影响**翻译文本**，不影响转写产物（音频 → `segments_en`）。因此**不进 chunk 缓存指纹**
（铁律 3 不触发），`--skip transcribe` 复用缓存换风格重译完全合法。

### 7. 声学不变量

翻译风格只改文本，绝不触碰时间戳（铁律 1）。`verify` 三 Lane（声学 / 内容 / 表现）
无需感知 style，行为不变。

---

## 理由

- **矩阵而非自由文本**：预设可测试、可审计、跨机一致，契合项目"环境确定性"主线。
- **默认 film 字节兼容**：把现有 `DEFAULT_PERSONA` 直接收编为 `film` 轨，避免改既有视频工作流。
- **显式切换 literal**：默认流畅、按需保真；不引入"自动路由"（音频画像不足以区分学术 vs 影视，
  自动路由会引入误判风险，违背确定性原则）。
- **诗歌并入 film**：出现频率低，`source` 机制已能承载风格提示，避免过度预设。

---

## 后果

- **正向**：创作者可在同一转写上对比多风格字幕；学术/技术内容信息保真提升。
- **负向 / 需注意**：
  - 双轨会令 Agent 翻译工作量翻倍（N 个 task 各译一遍），需在 Exit 6 指令中明确列出。
  - `generate` 需按 style 区分输出文件名（`<base>.<style>.bilingual.srt`），AGENTS.md
    Phase 3 状态机需补双轨说明（见 Spec 21 / 文档同步清单）。
  - 文档口径（README / AGENTS / specs）须随实现同步更新，避免口径漂移。

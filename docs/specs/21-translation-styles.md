# Spec 21 — 翻译风格体系（Translation Style Tracks）

- **Module**: `video_translate.config` / `video_translate.translate` / `video_translate.cli` / `video_translate.generate`
- **Decision**: ADR-027（双轨翻译风格体系）
- **关联**: MAJOR_VERSION_PLAN.md §T3；Spec 06（配置解析）；Spec 17/18（内容 Lane 验证）

---

## Purpose

为视频双语字幕引入三档**翻译风格**，让同一转写结果可按需产出不同取向的字幕：

- `film`（默认）：影视二创，信达雅 + 口语感，适合脱口秀 / 采访 / 短视频 / 诗歌靠 `source` 引导
- `literal`：忠实直译，适合学术 / 法律 / 技术文档（保真优先于流畅）
- `bilingual_study`：双语精读，直译为主 + 生僻词括号注记

CLI 通过 `--style` 接入（含 `VT_STYLE` 环境变量与 toml `[translate].style`），注入
`translate_task.json` 的 `persona` 与 `guidelines`；支持 `--style film,literal` 一次生成双轨。

---

## 接口契约

### 配置层（`config.py`）

```python
@dataclass
class StyleDef:
    persona: str
    guidelines: list[str]

STYLE_PERSONAS: dict[str, StyleDef] = {
    "film": StyleDef(persona=DEFAULT_PERSONA, guidelines=[...]),
    "literal": StyleDef(persona=..., guidelines=[...]),
    "bilingual_study": StyleDef(persona=..., guidelines=[...]),
}

VALID_STYLES = ("film", "literal", "bilingual_study")

@dataclass
class Config:
    style: str = "film"          # 新增；经 resolve_config 解析
    # ...既有字段不变...

# env_map 新增：
env_map = {
    "VT_STYLE": "style",
    # ...既有映射不变...
}
```

- `resolve_config` 覆盖顺序：`--style` > `VT_STYLE` > toml `[translate].style` > `"film"`
- 非法值（不在 `VALID_STYLES`）→ `_coerce_env` / CLI choices 报 `ValueError` / argparse error
- 逗号多值（`film,literal`）解析为列表；单值解析为单元素列表
- 显式 `persona`（CLI `--persona` 或 `VT_PERSONA`）时，`STYLE_PERSONAS` 基底被覆盖，
  运行时打印：`custom persona overrides style=<style>`

### 转写任务层（`translate.py`）

```python
def prepare_translate_task(
    *,
    segments, base, outdir, model=None, lang=None,
    source=None, glossary=None, persona=None,  # persona 仍可选（覆盖基底）
    style: str = "film",
    ...
) -> dict:
    """写 <base>[.<style>].translate_task.json，version=3，含 style 字段。"""
```

- `version` 由 `2` 升为 `3`；新增顶层 `"style": style`
- `persona` 取值：`persona`（显式） > `STYLE_PERSONAS[style].persona`
- `guidelines` 取值：`STYLE_PERSONAS[style].guidelines`；`source` / `glossary` 仍按既有顺序叠加
- **多 style 时**：为列表中每个 style 各写一份 task 文件
  （`<base>.film.translate_task.json` 等），文件名带 `.{style}` 后缀

### CLI 层（`cli.py`）

| 命令 | 新参数 | 行为 |
|---|---|---|
| `run` / `translate` | `--style {film,literal,bilingual_study}`（可逗号多值，默认走 config） | 经 `resolve_config` → `prepare_translate_task(style=...)` |
| `backfill` | `--style <name>` | 透传 style 至 translate_task |
| `generate` | `--style <name>`（可选） | 输出文件名加 `.<style>` 后缀；不传则文件名与现状一致 |

- `--style` 用 `choices=VALID_STYLES` + `nargs` 兼容逗号多值（内部 split）
- `run --style film,literal`：Exit 6 挂起时 `_RUN_AWAITING_AGENT_INSTRUCTIONS`
  逐个 task 打印指令块（含 `agent-zh` 写回文件名 `<base>.<style>.zh_segments.json`）
- `run --skip transcribe --style literal`：复用转写缓存，仅为已转写视频补一条风格轨

### 生成层（`generate.py`）

```python
def generate_subtitles(segments_path, zh_path, outdir, base=..., style=None, ...):
    out_base = f"{base}.{style}" if style else base
    # 复用 _resolve_out_base / _vN 递增 / _prune_old_versions（以完整 out_base 为锚）
```

- `style` 非空 → 4 产物为 `<base>.<style>.bilingual.srt` / `.zh.srt` / `.en.srt` / `.txt`
- `style` 为空（默认）→ 文件名与现状**完全一致**（向后兼容 golden）
- `generate_opts.json` 增加 `"style"` 字段记录

---

## 文件命名矩阵

| 场景 | translate_task | zh_segments | 字幕输出 |
|---|---|---|---|
| 单轨（默认 film） | `<base>.translate_task.json` | `<base>.zh_segments.json` | `<base>.bilingual.srt` |
| 双轨 `--style film,literal` | `<base>.film.translate_task.json`<br>`<base>.literal.translate_task.json` | `<base>.film.zh_segments.json`<br>`<base>.literal.zh_segments.json` | `<base>.film.bilingual.srt`<br>`<base>.literal.bilingual.srt` |

---

## 缓存指纹

style **不进** chunk 缓存指纹（`chunk_N.json` / `segments_raw.json` 仅覆盖转写产物）。
`--skip transcribe` 换风格重译合法，且不触发指纹失效（铁律 3）。

---

## 测试覆盖（TDD 清单）

- `tests/test_config.py`（追加）：
  - 默认 `style == "film"`
  - `VT_STYLE` 环境变量生效
  - toml `[translate] style` 生效
  - CLI `--style` 覆盖 env/toml
  - 非法 style → `ValueError` / argparse error
- `tests/test_translate_style.py`（NEW）：
  - `STYLE_PERSONAS` 含 `film` / `literal` / `bilingual_study` 三键，persona 与 guidelines 非空
  - 三轨 `guidelines` 互不相同
  - `prepare_translate_task(style="literal")` 写 `version==3` 且 `style=="literal"` 且 literal persona
  - **无 style 参数时** task 的 persona 与现行 `DEFAULT_PERSONA` **字节一致**（golden，防回归）
  - 多 style 生成 N 份带后缀 task 文件
- `tests/test_generate.py`（追加）：
  - `generate_subtitles(style="film")` 产出 `<base>.film.bilingual.srt` 等 4 文件
  - 无 `style` → 产物名与现状一致（golden）
  - `_vN` 递增与 `_prune_old_versions` 对带 style 后缀 stem 正常
- CLI 集成测试：`run --style literal` 参数贯通至 task 文件（mock 转写）

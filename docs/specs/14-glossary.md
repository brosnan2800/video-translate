# Spec 14 — 术语表（glossary）

## 目的
为多集 / 系列视频提供**译名一致性**能力：把角色名、专有名词的"建议译法"注入翻译任务，避免同一名字在不同片段被翻成不同中文。

## 格式
- `txt`：一行一映射，支持 `源 => 译` 或 `源: 译`（忽略空行与 `#` 注释）。
- `json`：`{ "源": "译", ... }`。

## 加载与注入
- 新增 `src/video_translate/glossary.py`：`load_glossary(path) -> str | None`，读取并返回**格式化后的上下文串**（如"`术语表（翻译时请优先采用以下译名）：\n- John Malkovich => 约翰·马尔科维奇`"）。
- `translate.py` 的 `prepare_translate_task` 增加 `glossary` 形参：task JSON 增加 `glossary` 字段，并**拼入 persona 上下文**。
- 决策依据：柔性注入而非强制替换（见 ADR-010）——保留信达雅 + 口语感，仅给出"建议译名"，不破坏翻译灵性。

## 配置来源（优先级：CLI > env > TOML > default）
- `config.py` 增 `glossary: str | None = None`。
- env：`VT_GLOSSARY`。
- TOML：`[translate] glossary = "路径"`。
- CLI：`--glossary PATH`。
- **仅显式提供生效，不做项目级默认路径**（避免隐式行为）。

## 关联
- 测试：`tests/test_glossary.py`（txt/json/empty/格式化）、`tests/test_translate_contract.py`（`test_prepare_task_includes_glossary`）、`tests/test_config.py`（配置来源）。

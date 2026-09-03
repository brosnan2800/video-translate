# V3 状态：完成与未完成

> ⚠️ **冻结于 V3（2026-07）。** 当前版本 **4.0.0**。V4+ 变更见 `README.md`「What's new in V4」章节。
>
> 版本：**3.0.0**（2026-07，基于 V2 工程底座）
> 开发方式：SDD（先写 spec/ADR 再写码）+ TDD（测试先红后绿，golden 字节级回归）
> 路线决策：V3 原计划引入 stable-ts（路线 B），Spike 实测 py3.13 不兼容后**回退路线 A**
> （faster-whisper 原生 `word_timestamps=True` + 自写 `split_by_gap`/`split_by_length`），
> **零新增依赖**。详见 `docs/adr/008-stable-ts-spike-rejected.md`。

---

## 一、V3 目标落地状态

| # | 目标 | 状态 | 落点 |
|---|------|------|------|
| 1 | 词级时间戳贯穿流水线 | ✅ 完成 | `transcribe.py` 开 `word_timestamps`；`segment.words` 贯穿 merge→split→generate |
| 2 | 断行（42 字，剪映行宽） | ✅ 完成 | `merge.py: split_long_cues` / `_split_by_length`，默认开，`--no-split` 关，`--merge-max-chars` 覆盖 |
| 3 | 静音保留 / issue #001 从源头修 | ✅ 完成 | `merge.py: _split_by_gap`（真实静音 >1.0s 即在词边界断开）；单测 `test_timing_issue001.py` 覆盖 |
| 4 | 时间戳不重算（更准） | ✅ 完成 | `_emit` 收紧到首词 start/末词 end；generate 取词边界；`--gap` 仅裁尾随静音 |
| 5 | 术语表 | ✅ 完成 | `glossary.py` + `translate.py` 注入 persona；`--glossary` / `VT_GLOSSARY` / TOML `[translate] glossary` |
| 6 | doctor 探测 Google 端点 | ✅ 完成 | `proxy.py: _probe_google_endpoint`；`cli.py: cmd_doctor` 探测；默认不硬失败，`--strict` 返回退出码 7 |
| 7 | `--gap` 兜底钳制 | ✅ 完成 | `generate.py: build_outputs(gap=0.2)` 两遍钳制，不造间隙 |
| 8 | 文档：spec 12–16 + ADR 008–010（中文） | ✅ 完成 | `docs/specs/12–16`、`docs/adr/008–010` |
| 9 | 单一双语 README | ✅ 完成 | `README.md` 合并英+中并补 V3；删除 `README.zh.md` |
| 10 | 原理级设计文档 | ✅ 完成 | `docs/design/`（translation-design.md + references.md + README.md） |
| 11 | 测试覆盖（TDD） | ✅ 完成 | 新增/改 9 个测试文件，快测 **140 passed**（2 个 `@slow` 默认跳过） |
| 12 | 版本号 → 3.0.0 | ✅ 完成 | `__init__.py` + `pyproject.toml` |

---

## 二、改动文件清单

**源码（路线 A，零新增依赖）**
- `src/video_translate/transcribe.py` — 开 `word_timestamps`，段带 `words`
- `src/video_translate/merge.py` — `_emit` 收紧词边界；新增 `_split_by_gap` /
  `_split_by_length` / `split_long_cues`（merge 后断行 + 静音拆段）
- `src/video_translate/generate.py` — `build_outputs(gap=...)` 取词边界 + 两遍 `--gap` 钳制
- `src/video_translate/glossary.py` — 新增，加载 txt/json 术语表
- `src/video_translate/config.py` — 新增 `glossary` 字段 + `VT_GLOSSARY`
- `src/video_translate/translate.py` — `prepare_translate_task` 注入 glossary 到 persona
- `src/video_translate/proxy.py` — 新增 `_probe_google_endpoint`
- `src/video_translate/cli.py` — `--no-split` / `--merge-max-chars` / `--gap` /
  `--glossary` / doctor `--strict`；退出码 7

**文档**
- `docs/specs/12-word-level-timestamps.md`、`13-cue-splitting.md`、
  `14-glossary.md`、`15-timing-silence.md`
- `docs/adr/008-stable-ts-spike-rejected.md`、`009-silence-preservation.md`、`010-glossary.md`（中文）
- `docs/specs/07-gotchas.md` — 追加"words 必须贯穿 merge"、"split 后段数变→zh 必须重译"
- `docs/specs/11-cli-v2.md` — 补 V3 CLI 开关与退出码 7
- `AGENTS.md` — 追加 V3 additions（doctor 探测、word、split、silence、glossary）
- `README.md` — 单一双语文件（合并旧 README.zh.md，删除后者）
- `docs/design/` — 原理级设计文档（本文档体系）
- `docs/V3-STATUS.md` — 本文件

**测试（新增/改）**
- 新增：`test_glossary.py`、`test_doctor.py`、`test_timing_issue001.py`
- 改：`test_config.py`（glossary / merge_max_chars）、`test_transcribe_contract.py`
  （words / resume 携带）、`test_merge.py`（emit 携带词 / 收紧 / split / gap）、
  `test_translate_contract.py`（glossary 注入）、`test_merge_golden.py`
  （无词 golden split 为 no-op）、`test_generate_golden.py`（词边界 / gap 钳制 /
  无词回退）
- golden：V2 归档为 `.v2.*`（8 文件）；4 个生成输出黄金用 `gap=0.2` 重生成

---

## 三、已知限制（留给 V4）

1. **黄金段级数据仍为 V2（无词）**：`docs/golden/` 的 `segments_en.json` /
   `segments_raw.json` / `merged_segments.json` / `zh_segments.json` 仍是 V2 的
   199 段无词数据。V3 词级路径靠**单元测试**（合成词时间戳）覆盖，未做端到端
   词级 golden 回归。若要端到端 golden，需用真实视频重跑 V3 流水线并重译（split
   后段数变，旧 zh 失效，必须重译）。
2. **跨 chunk 接缝处不做词级细分**：每个 `chunk_N.json` 独立存词，merge 只在段级
   拼接。chunk 边界本就是静音切分点，影响极小，但严格说接缝处静音不在词级处理。
3. **句末标点对中文`。`覆盖是近似**：`merge` 用 `[.!?]\s*$` 判句末，中文句号`。`
   通常不在其列（faster-whisper 对中文也常输出 `.`）。极端情况下可能误判句界。
4. **真实停顿 < `DEFAULT_SPLIT_GAP`(1.0s) 但人耳明显的残余**：`_split_by_gap` 不
   触发，只能靠 `--gap` 兜底或更细调参（调太小会把正常换气切断）。
5. **`--gap` 默认 0.2s 是经验值**：对极密对话可能略挤，对长停顿场景仍留白——可按
   视频风格用 `--gap` 调整。
6. **术语表是软提示**：强制替换（保口语感/信达雅）未做；若需硬统一专名，V4 可加
   可选"强制替换"模式。

---

## 四、发布核对

- [x] 代码 + 测试 + 文档同批提交
- [x] 版本号 3.0.0
- [x] 快测全绿（140 passed；`@slow` e2e 需模型+视频，默认跳过）
- [x] README 单一双语、删除 README.zh.md
- [ ] git tag v3.0.0
- [ ] 绕过 7890 代理 push（含 `--tags`）
- [ ] 核对 P0-1（README 同步）已上远程

> 注：P0-1 此前已确认在远程（`git rev-list origin/master..master = 0`），本次提交
> 后再次核对即可。

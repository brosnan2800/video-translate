# ADR-037 — 产物目录布局（workdir 单一基准）

Date: 2026-09-11
Status: Accepted
Companion: ADR-035（数据契约总线）, ADR-030（控制平面）, ADR-031（P0→P1 批准门）

> 本文定义流水线产物的**目录布局契约**：所有阶段产物统一收进
> `<outdir>/<base>/` 子目录，由契约层 `workdir()` 作为唯一落点基准。在 task
> plan 中可按「执行 ADR-037 D1~D6」引用。

---

## 1. Context（背景）

### 1.1 现状：交付物与中间产物分裂在两个层级

- `_default_outdir(input_path)` = `str(Path(input_path).parent)`（视频所在目录）。
- **中间产物**（`<base>.segments_en.json` / `segments_raw.json` / `zh_segments.json` /
  `vt_state.json` / `review.log` / `if.<fp>.chunk_*.json` 缓存 / `vocals.wav`）**平铺在
  `outdir`**，靠 `<base>.` 前缀区分。
- **最终字幕四件套**（bilingual / zh / en / `.txt` + `generate_opts.json`）**已经**单独进
  `outdir/<base>/`——`generate._resolve_out_base` 默认 `flat=False` 自建子目录，并做 `_vN`
  版本递增防剪映缓存碰撞（见 AGENTS.md 红线）。
- `pipeline._find_srt` 同时兼容 flat 与子目录两种位置——「分裂」是代码显式兼容的现状。

### 1.2 痛点

多个视频的产物混在同一目录靠 `<base>.` 前缀认人；尤其长视频的 chunk 缓存（十几个
`if.<fp>.chunk_N.json`）与 `vocals.wav` 与工作产物、视频本体混作一团，翻产物困难。

### 1.3 用户决策（已确认，C1）

- 布局方案：单个视频的全部产物统一进 `<outdir>/<base>/`。
- `_default_outdir` **保持**「视频所在目录」语义不变（保住 CLI `--outdir` 契约与
  `status` 多视频发现能力）。
- 旧平铺产物：**保留不动、不再读取、不迁移、不自动删除**（用户明确不再翻旧项目）。

---

## 2. Decision（决策）

引入契约层纯函数 `workdir(outdir, base) -> <outdir>/<base>/`，作为**所有产物落点的唯一
基准**：

**D1（路径单一来源，强化 ADR-035 D3）**：所有阶段产物（中间产物 / 缓存 / state / 最终字幕）
一律经 `workdir()` 拼装。**禁止任何模块自造 `os.path.join(outdir, base, ...)` 或
`Path(outdir) / base`**——这正是重演「模块自造产物路径」事故根因的入口。

**D2（outdir 语义不变）**：`outdir` 仍是「视频所在目录」（默认）或用户 `--outdir`；
`workdir` 是其派生层，不可在调用点重算或覆盖。

**D3（消除双层嵌套）**：`generate._resolve_out_base` 的 `sub = os.path.join(outdir, base)`
收敛为直接使用 `workdir()`——workdir 已含 base，禁止再套一层（否则产生 `videos/if/if/`）。
保留 `_vN` 版本递增与 `flat=True` legacy 分支。

**D4（多视频发现）**：`pipeline.discover_bases` 由扫描 `outdir/*.segments_en.json` 改为扫描
`outdir/*/<base>.segments_en.json`（子目录），`status --outdir videos` 仍能在一个 `outdir`
下发现全部已转写视频。

**D5（旧产物零处理）**：实现中**不**引入回退查找（新位置找不到旧位置再来一次）、**不**新增
migrate 命令、**不**删除旧文件。旧平铺产物与新的 workdir 布局互不干扰。

**D6（state 永不做 gate，强化 ADR-035 D5）**：`state.py` 的 stage 推断
（`infer_stage` / `rebuild_state` / `state_needs_rebuild`）同步改用 `workdir` 找产物，
但**不得**让任何闸门依赖 state 文件。

---

## 3. 影响面（改造点）

| 模块 | 位置 | 改动 |
|---|---|---|
| `artifacts.py` | 271-274 `artifact_path` | 基准改 `workdir`；新增 `workdir()` |
| `state.py` | 43 / 171 / 194 / 212 | `Path(outdir)` → `workdir` |
| `transcribe.py` | 244 / 358 / 383 / 405 / 447 / 474 | chunk / wav / lang sidecar / segments → `workdir` |
| `cli.py` | 1297-1300 / 1374 / 1506 / 1696 / 1698 / 1583 | task / pending / backfill / vocals 调用 → `workdir`；写盘前 `makedirs(workdir)` |
| `vocal_sep.py` | 125 / 315 / 468 / 473-474 | `vocals_wav_path` / `mkdir` / 窗口 wav → `workdir` |
| `align.py` | 90 `_align_cache_path` | → `workdir` |
| `fill_gaps.py` | 429 `review_cache_path` | → `workdir` |
| `translate.py` | 259 多风格 task | → `workdir` |
| `generate.py` | 137 `_resolve_out_base` | 收敛 `sub`，保留 `_vN` 与 `flat` |
| `pipeline.py` | 97 `_find_srt` / 294 `discover_bases` | 解析改 `workdir`；glob 扫子目录 |

**测试**：16 个涉及 `outdir` 的测试文件逐一修正路径假设；新增
`tests/test_artifact_directory_layout.py`；同步 `tests/test_pipeline_field_contract.py`（S4）。

**文档**：AGENTS.md（`--outdir` 示例改 `videos/`、产物路径说明）、README、docs/specs/00-overview、
docs/TRANSLATION-WORKFLOW、ADR-035 交叉引用。

---

## 4. 测试策略

- 布局契约测试断言：`workdir(outdir, base) == os.path.join(outdir, base)`；`artifact_path`
  落 `workdir`；`generate` 不产生 `if/if/` 双层。
- 全量回归 `uv run pytest`：确保 `flat=True` legacy 分支（golden）与 `flat=False` 新布局双轨通过。

---

## 5. 验证（完工判据）

1. 全新视频跑完整 `pipeline`：`videos/if.mp4` 的全部产物（含 chunk 缓存）收进 `videos/if/`，
   `videos/` 下只剩 `if.mp4`。
2. `uv run video-translate status --outdir videos` 能发现 `videos/if/` 下的已转写视频。
3. `uv run pytest` 全绿；`generate` 输出位于 `videos/if/`（无 `if/if/`）。

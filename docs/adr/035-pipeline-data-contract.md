# ADR-035 — 流水线数据契约总线（声明式 artifacts 表 + 五条铁律）

Date: 2026-09-02
Status: Accepted
Companion: ADR-030（控制平面）, ADR-032（决策点协议）, ADR-033（pipeline 收口/T8）,
ADR-034（音频路由/T9）, MAJOR_VERSION_PLAN T8/T9/T10.

> 本文是流水线**数据流转的唯一事实来源**：所有阶段间传输的数据（命名 / 文件 / 字段 /
> 生产者 / 消费者 / 可否重算 / 必须穿透的字段）全部登记在一张声明式 `artifacts`
> 契约表里。在 task plan 中可按「执行 ADR-035 §M1/M2/M3」引用。

---

## 1. Context（背景）

### 1.1 触发：两类已发生的数据流转事故

**事故一：duration 断线（ADR-034 §1.2 已记录）**
`analyze_audio` 不 probe duration、`_resolve_routing` 调用不传 → `continuous` 算不出 →
`adaptive_vad` / `separate_vocals` 自动推荐**永远 False**（rationale「duration unknown
-> continuous-noise detection skipped」）。上游算了/有，下游收不到，功能静默退化。

**事故二：merge 丢置信度字段（本 ADR 直接触发）**
`merge.py` 三个合并函数（`merge_segments` / `merge_short_cues` / `_merge_adjacent`）
把两段并成一段时**新建 dict 只留 start/end/text/words**，丢弃 `no_speech_prob` /
`avg_logprob` / `compression_ratio`。后果：

- review 的 `MISSING` 判定依赖信号 A（置信度字段），合并后时间轴上信号 A 全是
  `unknown` → **G1（已上线）与 G3（规划中）在生产里休眠**；
- verify 的 `LOW_CONFIDENCE` 道（`verify.py` 读同一批字段）同样被削弱；
- `review.py` 头部注释自述「main-pass segments frequently lose their confidence
  fields during merge, so A is often unknown」——**下游在补偿，而不是修根**。

### 1.2 结构性根因（为什么这类问题会反复发生）

流水线是「JSON 文件即接口」的架构：每个阶段读一个 JSON → 变换 → 写另一个 JSON。
四个结构性根因：

1. **无 schema/契约**：字段漏传不报错，只静默退化。
2. **白名单式重建**：merge 手写 `{start,end,text,words}` 新 dict，任何新增字段
   默认被丢——未来再加元数据，照样丢。
3. **下游补偿而非修根**：review 绕开信号 A（G2 改纯 B 驱动）、fill_gaps 为缺字段
   加 C2 兜底、恢复段手工重搬置信度（与 transcribe 重复实现，必然漂移）。
4. **无跨阶段契约测试**：字段丢失只能靠真实数据撞见，测试抓不到。

### 1.3 声学事实被重复计算

`silencedetect` 静音区间 / `duration` 这份声学事实在链上被独立重算 ≥3 次：
preflight（`doctor --video` 画像）→ transcribe（`cli.py` 转写内又 `analyze_audio`）
→ verify（volumedetect 分类未覆盖窗）。三次探测三处漂移风险，且 preflight 的画像
原始数据（duration / silence_intervals）从未落盘供下游复用——只落了「推荐结论」。

---

## 2. Decision（架构决策）

### 2.1 声明式契约表（`artifacts.py`）

沿用项目既有 idiom（`pipeline_def.STAGES` / `capabilities.CAPS` 的「声明式纯数据表 +
引擎解释」），新增 `artifacts.py`：

```python
class Artifact(TypedDict):
    id: str                       # 唯一规范名（字段名/文件后缀/decision key 由此取）
    file: str                     # 路径模板，如 "{base}.segments_raw.json"
    fields: tuple[str, ...]       # 已知字段
    carry: tuple[str, ...]        # 必须穿透下游变换的字段
    produced_by: str              # 唯一生产者 stage id
    consumed_by: tuple[str, ...]  # 消费者 stage id
    recompute: bool               # False = 禁止下游重算
```

契约表登记 §3.1 全部数据。它是**可执行的接口文档**：命名、校验、定位全部查表，
不做会漂移的散文。

### 2.2 五条铁律（可被测试强制）

1. **单一生产**：一个数据只准 `produced_by` 阶段写，其余阶段只读；禁止下游重算
   `recompute=False` 的数据。
2. **变换 copy-then-override**：任何阶段改写数据（如 merge），未声明的字段默认
   保留；`carry` 清单里的元数据**永不丢失**（Z2 下由回查指针保证，见 §2.3）。
3. **命名单一来源**：字段名 / 文件后缀 / 参数名 / stage id / decision key 全部从
   表取，禁止模块自造字符串。
4. **边界校验**：阶段边界断言「我要读的 artifact 存在且含必需字段」，缺了当场报错
   （CI 契约测试硬失败；运行时 loud warn，`--strict` 下 exit）。
5. **state 永不做 gate**（沿用 ADR-030 契约）：校验负责发现问题，生产闸门只看
   segments/zh 产物文件，state 坏了不阻断管道。

### 2.3 Z2 非破坏段存储

- `segments_raw.json`（whisper 原始段，全字段）是**唯一事实来源、不可变**。
- merge 不再重建段 dict：产出的 `segments_en.json` 每项新增
  `_raw_indices: [3, 4, 5]` 回查指针；text/words 仍是合并投影（对
  translate/generate 透明，段数/文本/分组语义不变）。
- 置信度（no_speech_prob / avg_logprob / compression_ratio）**永不聚合、永不
  覆盖**：review / verify 按 `_raw_indices` 回查 raw 逐段取值——原始三条各有各的
  值，全部保留（合并前的可疑段线索不再丢失）。
- fill_gaps 恢复段自带置信度（`transcribe._seg_to_dict` 同款），是对 raw 的
  **追加**而非改写。

### 2.4 state 升 schema v2

`vt_state.json` 是**全局/运行级参数的唯一落点**：

- v2 新增 `audio_profile` 数据段：`duration` / `mean_db` / `max_db` /
  `silence_fraction` / `silence_intervals`——preflight 算一次落盘，
  transcribe / verify **读 state，禁止重算**。
- 旧 schema 兼容迁移：`load` 对 v1 文件按现有 artifacts 重建补齐，仍不阻断。
- decisions（含 origin 裁决）、阶段状态、segments_sha 指纹链语义不变。

### 2.5 数据分工与编码规范

- **数据分工**：全局/运行级参数（duration、静音区间、routing、阶段状态）→
  `vt_state.json`；每段字幕级参数（置信度三字段）→ 跟段绑死在
  `segments_raw.json`。
- **编码规范 SDD + TDD**（收敛进 `.codebuddy/rules/`；
  `AGENTS.md` 只承载翻译 Agent 协议，与编码规范无关）：
  - **SDD（Spec-Driven Development）**：先写 Spec/ADR 定契约，再实现——本 ADR
    即 M1–M3 的契约。
  - **TDD（Test-Driven Development）**：先写测试再实现；新模块（artifacts.py）
    测试先行，契约测试 `test_pipeline_field_contract.py` 是 M3 的验收闸。

---

## 3. 详细设计

### 3.1 数据清单（登记进契约表）

| # | artifact id | 文件/落点 | 关键字段 | produced_by | consumed_by | recompute |
|---|---|---|---|---|---|---|
| 1 | `audio_profile` | `vt_state.json` | duration / mean_db / max_db / silence_fraction / silence_intervals | preflight | transcribe, verify | **False** |
| 2 | `routing` | `vt_state.json` decisions | style / vad / adaptive_vad / separate_vocals / vad_threshold | preflight(决策点) | transcribe | False |
| 3 | `segments_raw` | `{base}.segments_raw.json` | start / end / text / words / **no_speech_prob / avg_logprob / compression_ratio** | transcribe | merge, review, verify | False |
| 4 | `segments` | `{base}.segments_en.json` | start / end / text / words / **_raw_indices** | merge | review, translate, generate, verify | False |
| 5 | `review` | `{base}.review.json` | 审查判定 / g1,g2,g3 窗口 / unrecoverable | fill_gaps | 人工复盘 | False |
| 6 | `zh` | `{base}.zh_segments.json` | index → zh | translate(agent/google/llm) | generate, verify | False |
| 7 | `srt` | `{base}*.srt` | 四个成品 | generate | 交付 | False |
| 8 | `detected_lang` | `{base}.{hash}.detected_lang.json` | lang | transcribe | translate, generate | False |
| 9 | `audio_source` | `vt_state.json` | vocals.wav 路径 + vsep backend/model/input_hash | transcribe(T2) | fill_gaps, verify | False |
| 10 | `verify_result` | `vt_state.json` stages.verify | issues / attempts / align 漂移 | verify | 人工/agent 修复 | False |
| 11 | 环境/配置快照 | `vt_state.json` | model / device / compute_type / threads / toolchain 路径 / CLI 旗标 | 各阶段 | 排查/指纹 | — |

**额外系统性项**（一并收编）：

- `audio_source`（vocals.wav 路径）目前靠 `separate_fingerprint` 反推，改为显式
  记录进 state（表条目 9）。
- 缓存指纹规则统一：transcribe chunk 指纹 / review digest / vocal_sep fingerprint
  各自为政 → 契约表统一声明「哪些参数进指纹、哪些字段仅穿透（carry 不进指纹）」。

### 3.2 契约校验器

- `validate_artifact(artifact_id, data) -> list[str]`：字段存在性检查，缺
  `carry` 字段 = 违约。
- `enforce_contract(...)`：阶段边界调用；`--strict` 下抛 `GateFail`（exit 8），
  默认 loud warn 并落盘。
- 校验结果与 `[audit]` 审计行同时写 `videos/<base>.review.log`（用户翻文件不看
  stdout；日志只写摘要，不写大 payload）。

### 3.3 缓存影响（显式声明，一次性代价）

`segments_en.json` 增加 `_raw_indices` 会改变内容哈希 → segments_sha / review
digest 变化 → 旧 review 缓存与下游指纹自动失效（一次性）。契约表显式声明哪些
字段仅穿透不进指纹，避免未来再发生同类意外失效。

---

## 4. 迁移计划（M1–M3，每步独立可交付、可回滚）

### M1 — 登记现状（零行为变化）

- 建 `artifacts.py`（契约表 + 校验器 + 命名常量）。
- 现有散落的文件名/字段名/决策 key 全部登记；各模块引用改查表。
- **验收**：全量测试绿，行为零变化（纯登记）。

### M2 — 消灭重算

- `state.py` 升 schema v2（`audio_profile` 数据段 + 兼容迁移）。
- preflight（`doctor --video`）把 duration / silence_intervals 落盘 state。
- `cli.py` 转写内重算 `analyze_audio` 改为读 state；`fill_gaps` 的
  `_probe_silences_or_none` 降级为 state 缺失时的兜底（非常规路径）。
- `vocal_sep` 的 audio_source 路径显式记录进 state。
- **验收**：silencedetect 全链只算一次（transcribe/verify 命中 state）；测试绿。

### M3 — 非破坏段存储（Z2）

- `merge.py` 三处改产 `_raw_indices` 视图（不再新建只留 4 字段的 dict）。
- `review.py` / `verify.py` 按 `_raw_indices` 回查 raw 置信度。
- 新增 `tests/test_pipeline_field_contract.py`：真实段 fixtures，断言
  transcribe→merge→fill_gaps 全链置信度字段存活且可回查。
- **验收**：G1 在合并后时间轴能拿到信号 A（单测覆盖）；全量 pytest 绿。

---

## 5. Consequences

### 5.1 涉及文件

`src/video_translate/artifacts.py`（新）、`state.py`、`pipeline_def.py`、
`pipeline.py`、`merge.py`、`fill_gaps.py`、`review.py`、`verify.py`、
`translate.py`（如消费段字段则小改）、`cli.py`、`audio_profile.py`、
`vocal_sep.py`、`.codebuddy/rules/`、`AGENTS.md`、
`MAJOR_VERSION_PLAN.md`、`docs/TRANSLATION-WORKFLOW.md`、
`tests/test_pipeline_field_contract.py`（新）。

### 5.2 性能

silencedetect / duration 全链只算一次，省去 transcribe / verify 的重复 ffmpeg
探测；回查置信度为按索引 O(1) 读取，无额外解码。

### 5.3 缓存 / golden

`segments_en.json` 加 `_raw_indices` → 旧 review 缓存 / segments_sha 一次性失效；
golden 单轨 bare 本地重跑确认不回归。

---

## 6. 验收标准（汇总）

- 契约表覆盖 §3.1 全部数据且命名唯一（M1）；
- silencedetect 全链只算一次（M2）；
- merge 不再丢任何字段，契约测试绿（M3）；
- G1 能在合并后时间轴拿到信号 A；
- 全量 pytest 绿；
- 文档同步：MAJOR_VERSION_PLAN T10（P0，T8 前）+ T8/T9 引用 +
  TRANSLATION-WORKFLOW 按契约表描述 + `.codebuddy/rules/`
  harness 重写 + AGENTS.md 按使用方式微调；
- 审计/校验结果落盘 `videos/<base>.review.log`（不依赖 stdout）。

---

## 7. 风险与回退

| 风险 | 触发 | 对策 |
|---|---|---|
| `_raw_indices` 改变 segments_en 内容哈希 → 缓存失效 | M3 落地 | 一次性代价；契约表显式声明指纹输入 |
| 下游模块直接读段字段未适配视图 | M3 | 先做语义级引用影响分析，列出全部消费点逐个适配 |
| state v2 旧文件不兼容 | M2 | load 兼容迁移 + 按 artifacts 重建，永不阻断 |
| M1 改查表引入行为漂移 | M1 | 零行为变化验收（全测试绿 + golden 对比） |
| 契约校验误伤既有流程 | M2/M3 | 默认 loud warn 不阻断，`--strict` 才 exit；先观察再收紧 |

---

## 8. 系统架构定位（与使用方式的关系）

- **程序唯一入口**：`pipeline`（`run` / `generate` / `verify` 为原语）。
- **两种使用方式**：
  - 有翻译 agent：用户说「我要翻译这个视频」→ agent 调 `pipeline` 走流程，
    agent 只负责翻译 + 语义回读 + 指向入口；
  - 无 agent：用户/LLM 直接调 `pipeline`。
- Agent 协议见 `AGENTS.md`（仅翻译相关）；编码规范（SDD/TDD + 总线铁律 + 架构 +
  强约束）见 `.codebuddy/rules/`。

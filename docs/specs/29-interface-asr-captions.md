# Spec 29 — 接口型 ASR 方案（`captions` 子命令）

- 状态：批准（**已实现**）
- 日期：2026-09-17
- 关联：[ADR-042](../adr/042-youtube-captions-as-asr-source.md)（决策）、[ADR-038](../adr/038-asr-layer-extraction.md)（ASR 层抽离 / Provider 边界）、[ADR-035](../adr/035-pipeline-data-contract.md)（数据契约总线）、[ADR-003](../adr/003-http-proxy-only.md)（仅 HTTP 代理）、Spec 01（数据 schema）、Spec 02（transcribe 行为 · 对照）、Spec 18（verify 三 lane）、Spec 23（环境入口）

## 范围

- **IN**：`captions <url>` 子命令；字幕轨选取（人工 CC > 自动 ASR）；**滚动片段句子化** + 时间戳插值；产出与转写同契约的 `segments_en.json`；`asr_source` 来源标记；本地缓存；代理接线；`verify` 的 `acoustic-unavailable` 处置。
- **OUT**：下载音频/视频（ADR-042 D3）；词级补全（D4）；说话人识别；章节切分；元数据/封面入库（均在 ADR-042 §未来）；`yt-dlp` 相关（归 P3）。

## 不变量（load-bearing）

1. **产出同契约**：`<base>.segments_en.json` 的字段与形状与 `transcribe` 产出**完全一致**（Spec 01 / ADR-035 契约表为准）。② 层不得因来源不同而改变读取方式。

### 实现形态（落地时的选择，记录以免被误读）

抓取被实现为 **ADR-038 的第二个 Provider**（`ytcaptions.YtCaptionProvider`，满足 `ASRProvider` 协议），由 **`asr.run_asr` 统一编排**完成后处理 —— 而不是在 `cmd_captions` 里手写一套。

这样做的收益：合并 / 全大写归一化等后处理**与 Whisper 路径共用同一段代码**（`run_asr` 是唯一编排者），两条来源之间不存在"第二套整理逻辑"。补洞 / review / G3 通过 `AsrRequest` 的开关关掉（`audit=False, review=False, g3=False, snap_drift=False`）—— 它们全部建立在音频参照上（D3）。

2. **不静默回退**：无可用字幕轨 → **报错退出**，绝不回退本地转写（ADR-042 D2）。
3. **不下载音视频**：任何情况下都不得为"补词级/跑对齐"而下载音频（ADR-042 D3/D4）。
4. **代理仅 HTTP**：沿用 ADR-003；SOCKS 一律拒绝（`setup_http_proxy` 会 pop SOCKS 变量）。
5. **失败不写半成品**：任何失败路径都不得留下可被误认为完整的 `segments_en.json`。

## 命令契约

```
uv run video-translate captions <url-or-id> [选项]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `<url-or-id>` | 必填 | 接受完整 URL / `youtu.be/` 短链 / `/embed/` / `/shorts/` / 纯 11 位 video id |
| `--outdir` | 视频所在目录语义 → 与 `transcribe` 一致（默认 `videos`） | 产物目录 |
| `--base` | 由 video id 派生 | 产物 basename。**必须可指定**：URL 派生的名字对人类不友好 |
| `--lang` | `en` | 字幕语言（可多值按优先级，如 `--lang zh,en`） |
| `--list` | off | **只列**可用字幕轨（语言 / 人工 or 自动），**不落盘** |
| `--no-auto` | off | 排除自动生成字幕（只有人工轨可用时才成功） |
| `--refresh` | off | 忽略缓存，强制重取 |
| `--proxy` / `--no-proxy` | 见下 | 与既有子命令一致 |

**收尾行为必须对齐 `transcribe`**：写 segments → 落 state（含 `segments_sha`）→ 打印 NEXT 块（`_print_pipeline_next`），使 P1→P2 之间**不存在第二套协议**。若具备条件，与 `run --engine agent` 同样返回 `EXIT_AWAITING_AGENT(6)` 进入翻译停点。

### 代理（复用现有机制，已核实）

- 解析沿用 `proxy.detect_proxy(cli_proxy=..., cli_no_proxy=...)`：`--no-proxy` → `--proxy` → `VT_PROXY` → `HTTPS_PROXY`/`HTTP_PROXY` → TCP 探测 `127.0.0.1:7890` → `None`（直连）。
- **必须显式传给库**：`youtube-transcript-api` 用构造参数 `proxy_config`（`GenericProxyConfig(http_url=..., https_url=...)`）配置代理，README **未承诺**读取 `HTTP_PROXY` 环境变量 —— 因此不能只依赖 `setup_http_proxy` 的副作用，要把解析结果显式构造进去。
- `None` → 不传 `proxy_config`（直连）。国内直连 YouTube 通常不通，会落到 `RequestBlocked`/`IpBlocked` → 按 D8 给"请配置 HTTP 代理"的确定性指引（与 `doctor` 的 Google 探测同族）。
- **不做**：SOCKS（ADR-003）、住宅代理轮换、浏览器 Cookie（ADR-042 D8-L3）。

## 字幕获取与失败语义

- **语言选取**：按 `--lang` 列表顺序在**人工轨**中找；找不到再在**自动轨**中找（`--no-auto` 时跳过这一步）；都无 → 报错。
- **错误分类**（对应 ADR-042 D8 的"可诊断"要求）：

| 情形 | 输出 |
|---|---|
| 视频完全没有字幕轨 | `[captions] no transcript available for <id>` + 提示"该视频无字幕，需走本地转写（`run`）" |
| 请求语言不存在 | 列出**可用语言**，提示改用 `--lang` |
| 视频不可用（删除/私有/地区锁） | 明确说明，勿误判为网络问题 |
| 被限流 / IP 被拒（`RequestBlocked` / `IpBlocked`） | 提示配置 HTTP 代理（`--proxy` / `VT_PROXY`），并说明云 IP 更易被拒 |
| 反爬拦截（换 client 仍失败） | 说明 L1 已重试，建议过段时间或换出口 IP |

- **不做重试**（ADR-042 D8 实现时已据实修正）：原设想的「换 client identity 重试」**不可实现** —— 核实发现该库只有一套硬编码 client（`_settings.INNERTUBE_CONTEXT` = ANDROID），没有可轮换的 web/ios 身份；且 IP 封禁型失败针对的是**出口 IP**，同 client 重试无意义。失败即按上表分类给指引。

## 句子化（本 Spec 的核心算法）

YouTube 自动轨是**滚动片段**（常 2–3 词一条、相邻条文本重叠）。产出 `RawSegment[]` 前必须重组。
滚动有**两侧**表现，都要处理：文本侧（相邻条内容重复）与**时间侧**（见步骤 3）。

### 步骤

1. **去重叠拼接**：把 snippet 序列拼成连续文本时，对相邻片段做**最长后缀—前缀匹配**消除滚动重复。
   - 例：`["...the quick", "quick brown fox", "brown fox jumps..."]` → 去掉 `"quick"` / `"brown fox"` 的重复部分。
   - 匹配需**大小写与标点归一化后**比较，但**输出保留原文**（不做改写）。
2. **按句末标点切句**：`。 ？ ！ … . ? !`（含全角）。
   - **CJK 感知**：中日文里 `.` 也常出现在缩写/小数中，不能简单按 ASCII 句点切；参考实现（`baoyu-youtube-transcript`）的做法是结合前后字符类别判断。
   - **超限兜底**：切出的块若超**软上限**（`0.6 × DEFAULT_MAX_DUR` 与 `2 × DEFAULT_MAX_CHARS`，
     取**先到者**）则再切，切点优先落在**片段边界**，防止"整篇一句"。
     实现上对**所有**块生效（不限"无标点"），给出的是**粗粒度上界**（84 字符 / 4.8s）。

     > **与合并层的分工**：42 字符（剪映单行上限）的**精切**在合并层
     > `merge.split_long_cues`。它原先是**纯词级**切分器，对无词段无效（Spec 13 原文的
     > 「无 words 理论上不会发生」在本方案下失效）；已补**文本级退化路径**
     > `_split_wordless`（Spec 13）。故本层只需管"防整篇一句"的**粗上界**，不必兼任行宽切分。
3. **时间戳插值**：片段有**有效窗口** —— `duration` 是**显示时长**，会越过下一条的
   `start`（滚动轨的**时间侧**表现）。故片段 k 的有效窗口取 **`[s_k, s_{k+1})`**，
   末条用自身 `start + duration`；非滚动数据（`s_{k+1} >= e_k`）保持原窗口不变。
   句子在其跨越的窗口区间内按**字符长度比例**线性插值取 `start` / `end`
   （`ytcaptions.effective_windows` + `char_times`）。
   - ⚠️ **反面教训**：若按 `[start, start + duration]` 取窗，则同一条片段里的**第二句**
     会被前一句占满窗口、再被下面的「互不重叠」钳制挤成 **0.01s 不可见微段**
     （实测 Shorts 视频复现）。
   - **约束**：产出的段必须 `start < end`、**严格非递减**、且**互不重叠**（重叠部分归前一段）。
   - 这些时间戳**不是声学真值**（已接受），但必须**单调、连续、与文本长度成比例**。

### 参数

| 常量 | 建议值 | 理由 |
|---|---|---|
| 无标点软上限（时长）| `0.6 × merge.DEFAULT_MAX_DUR` | 与现有合并阈值同源，不引入第二套数字 |
| 无标点软上限（字符）| `2 × merge.DEFAULT_MAX_CHARS` | 同上（`42 → 84`）|

> 句子化**不做 42 字符切分** —— 那是 ② 层的交付约束（剪映显示上限），由既有 `merge` 的 split 阶段负责。本层只负责"把碎片还原成句子"。

## 来源标记（`asr_source`）

新增 artifact 条目，落在 `workdir(outdir, base)`：

**`<base>.asr_source.json`**：
```json
{
  "kind": "youtube-captions",
  "track": "manual",          // manual | auto
  "lang": "en",
  "video_id": "dQw4w9WgXcQ",
  "has_audio_reference": false
}
```

- `kind` 的取值域：`"youtube-captions"`（本方案） | `"faster-whisper"`（本地转写，由 `transcribe` 补写）。
- **`has_audio_reference`** 是下游唯一需要的判据（`verify` 据此决定声学 lane 的可执行性），显式落盘而非让下游去猜 `kind`。
- 同一份信息同时写入 `vt_state.json`（供 `status` / 位置解析使用）。
- **为什么必须有**：两种来源的 `segments_en.json` 从字节上无法区分（ADR-042 D4/D6）。

## verify 的处置（ADR-042 D7）

`cli.cmd_verify` 的「缺 `--video` 即 refuse（exit 2）」分支改为：

- 若 `asr_source.has_audio_reference == false` → **不拒绝运行**，而是产出 issue `acoustic-unavailable`，说明「**依赖音频参照的声学子检查未执行**（静音跨越 / 首 cue 过早 / 漏检 uncovered）」；
- 该 issue **计入 flag** ⇒ strict（默认）下 `exit 8`；要放行须 `--no-strict`（**显式弃权**）；
- **相邻重叠（`find_adjacent_overlaps`）照常执行** —— 它是纯 cue 几何，不需要音频；
- 内容 lane / 表现 lane 照常；
- **不新增退出码**，**不引入 "partial pass" 概念**。

`verify.py` 侧**无需改动**（`verify_acoustic` / `find_uncovered_speech` 对空参照已有 `if not silences: return []` 的优雅降级）。

## 缓存（ADR-042 D9）

- 首次成功取到后落盘原始 snippet 数据 + `asr_source`；同视频（同语言）再次调用**不产生网络请求**。
- 失效条件：请求语言变化、`--refresh`。
- 缓存位置：`workdir(outdir, base)` 内，随产物目录自然失效。

## TDD 清单

- **句子化（纯函数，无网络）**：
  - 去重叠：滚动重复的前后缀被正确消除，输出保留原文大小写/标点；
  - 切句：ASCII 与 CJK 两类标点；缩写/小数不误切；
  - 超限兜底：超长块被按软上限切开（字符 / 时长**取先到者**），切点落在片段边界；
  - 时间戳：`start < end`、严格非递减、互不重叠；字长比例关系成立；
  - **滚动窗口**：`duration` 越过下一条 `start` 时有效窗口止于下一条的 `start`；
    同一条片段含多句时按字符比例分摊窗口，**不产生 0.01s 微段**（回归守卫）。
- **选取逻辑**：人工轨优先；`--no-auto` 时自动轨被排除；无轨 → 报错（不回落转写）；
  **只有自动轨时必须回退成功** —— 库的 `find_manually_created_transcript` /
  `find_generated_transcript` 无匹配时**抛 `NoTranscriptFound` 而非返回 `None`**，
  必须逐类接住；否则「只有自动轨」这一最常见情形会被误判成"整片无字幕"
  （而 `--list` 明明列得出轨道）。
- **错误映射**：五类上游异常各映射到确定性指引（mock 库异常）。
- **代理**：`detect_proxy` 结果被构造成 `GenericProxyConfig`（非 None 时）；`--no-proxy` → 不传；SOCKS 被拒。
- **契约**：产出通过 `validate_artifact("segments_raw", ...)`；`asr_source.json` 字段齐备。
- **接线**：`captions` 收尾后 `resolve_position` 指向 `translate`；state 的 `segments_sha` 已落盘（stale-translation 闸放行）。
- **verify**：`has_audio_reference=false` 时产出 `acoustic-unavailable`（strict 下 exit 8 / `--no-strict` 下 exit 0）；相邻重叠检查仍执行；`verify.py` 零改动。

## 验收标准

1. 全量 `uv run pytest` 绿，用例数只增不减。
2. `captions <url>` 产出与转写同契约的 `segments_en.json`，`status`/`pipeline` 直接指向 `translate`。
3. 无字幕 → 报错且不留半成品；不静默回退本地转写。
4. 同视频重复调用零网络。
5. `verify` 在无音频来源下给出 `acoustic-unavailable`，绝不静默通过。
6. 新增依赖 `youtube-transcript-api` 进 `pyproject` 顶层 + `uv.lock` 成对提交（R3）。

# ADR-036: fill_gaps recovery 起点回溯 —— hard-cut prefix collapse 修复

- 状态：接受
- 日期：2026-09-03
- 关联：ADR-016（recall recovery net）、ADR-020（tail-echo 守卫）、ADR-021（fill_gaps 恢复段守卫）、ADR-031（恢复段质量硬化）、`fill_gaps.py` `_probe` / `_probe_long_hole` / `_coverage_in_hole` / `_probe_pads_for_window`；`docs/specs/16-fill-gaps.md`

## 背景

`emily-blunt.mp4` 实测暴露了 fill_gaps recovery 的一个根源性缺口：W2（821.44–832.74s）与 W3（894.46–905.76s）各缺失约 10s 对白，recovery 只掏回洞尾碎片，洞内对白（"Chris Walken…"、"I met Christopher Walken on the set of Suicide Kings." 等）全部丢失。

### 根因（git 调查坐实）

- recovery 解码窗口从"洞起点"（gs，即主转写覆盖边界）开始，`pad` 仅 ±0.5s。当 gs 落在前一句**半句处**（本例中旁白 "Time to find out." 的尾巴），窗口起点仍陷于前句内部，触发 Whisper prefix collapse —— 解码器咬住前句碎片后就预测 end-of-transcript，剩余洞内对白被吞。
- 手动细子窗口强制解码证明：将 W2 解码起点前拉到 815s（旁白完整开头，回溯约 6s）即可完整转出洞内对白。即**关键在起点够前，而非窗口长短**。
- git 历史确认：ADR-033/034/035 未改动 `transcribe.py` 转写参数，也未改动 fill_gaps 的 recovery 解码起点逻辑。`_PROBE_PADS` / `_SUBWIN` / `_probe` 从 v5 (704cec1) 到当前 master 完全一致。本案**不是 ADR 33/34/35 引入的代码回归**，而是 v5 起就埋下的设计缺陷（recovery 起点假设"洞起点附近就是真实对白起点"，未覆盖"洞起点扎在前句内部"的硬切点）。

### `_PROBE_PADS` 历史设计原因（必须保留）

`_PROBE_PADS = (0.2, 0.0, 0.5)` 的初衷是防止 pad 太大把**前一句尾巴**拉进窗口起点触发 prefix collapse（真实案例：860.5→889.2s 窗口，pad=0.5 只产出 "than usual."，pad=0.2 产出 7 段真实对白）。本 ADR 扩展该序列而非替换它。

## 决策

### D1 — 扩展 `_PROBE_PADS` 为大跨度回溯序列（P0）

`_PROBE_PADS = (0.2, 0.0, 0.5, 2.0, 4.0, 6.0)`。新增的 2/4/6s pad 让 `_probe` 能尝试"回溯到前一句完整开头"。`_MULTI_PROBE_MIN_WINDOW = 4.0` 不变：短洞（<4s）仍只试小 pad（`_PROBE_PADS[:1]`），避免过度回溯拖入邻居句。`_PROBE_PADS[0] = 0.2` 不变，所有引用 `_PROBE_PADS[0]` 的调用点（长洞子窗、G1/G2 切片）行为不变。

### D2 — coverage 打分只统计洞内区间（P0）

新增纯函数 `_coverage_in_hole(cand, gs, ge)`，只累加候选段与 `[gs, ge]` 重叠部分的长度。`_probe` 改用它替代原 `sum(c["end"]-c["start"])`。否则大 pad 拉回的前句尾巴会被计入 coverage，导致早停（`_PROBE_GOOD_COVERAGE * hole_len`）在洞内对白尚未恢复时就虚高触发，恢复失败。早停阈值相应改为 `_PROBE_GOOD_COVERAGE * (hole_ge - hole_gs)`。

### D3 — 长洞首窗前推并用多 pad 解码（P1）

`_probe_long_hole` 的第一个 sub-window 起点前推到 `max(0, gs - _PROBE_PADS[-1])`，并改用 `_probe(...)`（多 pad + 洞内 coverage）而非单 pad `_decode_once`，保证超宽洞头部同样避开半句起点。其余 sub-window 保持单小 pad。

### D4 — 守卫层全部复用，不改动（P0）

`_is_recovered_hallucination` / `_dedupe_seams` / `_is_echo` 不动。大 pad 拉回的前句尾巴由 `_is_echo`（文本相似度）与 `_is_recovered_hallucination` 信号 A（重叠）自然拦截，不会作为回声插入时间轴。下游 verify / 控制平面对 `_recovered` 等字段的依赖不变。

### D5 — 拒绝的备选方案

- **回退 ADR-033/034/035**：git 证明它们未引入回归，回退不会修复本案，只丢功能。
- **缩小窗口到 8s**：关键在起点不在窗长；8s 仍可能落在半句上（本例 W2/W3 仅 11.3s）。
- **改 `condition_on_previous_text=True`**：实测证明不改变固定 240s chunk 边界的 collapse 行为，且会引入跨 chunk 回声循环。
- **引入 VAD/语义分割定位"前句开头"**：增加复杂度与 I/O，多 pad 旋转已能用现有 scoring 机制解决。

## 测试

- `tests/test_fill_gaps_prefix_collapse.py`：
  - `_coverage_in_hole` 只算洞内覆盖、洞外片段不计；
  - `_probe_pads_for_window` 短洞单 pad、宽洞全 pad；
  - mock Whisper 模拟"起点决定 prefix collapse"：洞起点落半句时，大 pad 恢复洞内对白，前句回声被拦截（5 passed）。
- 端到端：在 `emily-blunt.mp4` 上重跑 `fill_gaps`，确认 W2/W3 完整恢复且未引入回声/幻觉（见状态：待端到端验证）。

## 实测残留事故（Walken 13:41–13:52）

- 状态：残留未修
- 视频：`videos/Nobody Can Handle Christopher Walken's STRANGE Hum.mp4`（双语精读风格，`separate_vocals=false`）
- 几何数据：洞 `821.44–832.74s`（13:41.4–13:52.7），约 **11.3s**。邻接段：
  - `[293] 820.64–821.44` "Time to find out."（原产，紧邻洞头）
  - `[294] 832.74–835.06` "inside you now, you now,"（`_recovered=True`，洞尾恢复起点）
- 现象：`segments_en.json` 与 `.review.json` 的 `merged` **完全一致**，`[293]` 之后直接 `[294]`，821.44–832.74 **整段无字幕**（zh 100% 覆盖，故非翻译漏翻，而是**识别/补洞漏识别**）。
- `review.log`：`long-hole 821.4->850.5s: recovered 5 seg(s)` —— 长洞被切成子窗解码，仅恢复 832.74+，**洞头 821.44–832.74 仍空**。

### 根因

1. **单趟 recovery 不重扫残留洞（结构性根因：必要但不充分）**：`_probe_long_hole` 把长洞切成 12s 子窗，首子窗前推 6s 到 `815.4` 解码；whisper 在转完 "Time to find out."（821.44 结束）后，首子窗产出只从 832.74 起，**子窗内 821.44–832.74 的残留缺口没有第二次解码机会** —— recovery 只跑一趟（hole 扫描在恢复前做一次），且下游 G1/G2/G3 遍历的是 **segment 而非 gap**（`review_segments` 逐段判定，从不扫描段间空隙），残留洞头连被 review 标 MISSING 的机会都没有。
   ⚠️ **该根因只解释「一旦没产出就没人管」，不解释「为什么 whisper 在 821.44–832.74 没产出」**。后者才决定 F1 能否救回本窗口（见下）。
2. **review 缓存未捕获解码逻辑变更（缓存失效盲区）**：`review_digest` 仅基于时间轴 hash + 部分参数（`cache_params`），**不含 `_PROBE_PADS` / `_coverage_in_hole` 等恢复解码逻辑**。ADR-036 改的是解码逻辑而非参数，因此改后重跑 `fill_gaps` 会**命中旧缓存**、返回修复前那份「部分恢复」的时间轴，修复对**已有视频不生效**。本视频 `.review.json`（2026-09-03 12:59）即此情形 —— 修复代码未触及本次产物。

### 待定因：whisper 为何在洞头没产出（决定 F1 是否有用）

| 假设 | 含义 | F1（重扫残留洞）能否救回 |
|---|---|---|
| **(a) 该窗口是音乐/环境音，非语音** | 电影转场、配乐、音效；whisper 判定正确 | **无效**，重扫也没字 |
| **(b) 该窗口有语音，但被坍塌/掩蔽** | prefix collapse、音乐掩蔽、上下文干扰 | **可能有效**（若裸跑可救）；若需 demucs 才能救，则须把残留洞头也喂给 G3 |

### 佐证（仅证「非静音」，不证「有语音」）

- `audio_profile.probe_window_silence_fraction(821.44, 832.74) = 0.28` → 约 **72% 非静音**，ADR-012 的「整洞落于静音区间即跳过」**正确地没有跳过**该洞（洞确实被解码了）。
- ⚠️ **非静音 ≠ 语音**：音乐/环境音同样携带能量，故不能据此断言「确有字幕」。（此前把该证据写作「确为真实内容」属过度断言，已修正。）
- 旁证（推测，未坐实）：recovered 文本 "inside you now..." 自 832.74 起为电影台词，821.44–832.74 可能是该片段的前奏/转场配乐。

### 单次验证

剪 `821.44–832.74` 单独解码，两条分支对照：

- ① whisper 裸跑（`vad_filter=False`，`no_speech_threshold=0.0`）
- ② demucs 分离人声后 whisper

判据：①有字 → (b)，F1 有效；仅②有字 → (b) 且需把残留洞头喂给 G3；①②均无字 → (a)，**非 bug**，属音乐/非语音留白。

#### 实测结果（结论：**(b) 成立**）

剪 `821.44–832.74`（11.30s）单独解码，三分支对照（`large-v3`，`vad_filter=False`，`no_speech_threshold=0.0`）：

**① whisper 裸跑 —— 有字，且为真实语音：**

```
821.44-826.74  nsp=0.919  | There's one guy who could do and I think we all would watch that guy is Chris Walken. Oh god. Yes
827.38-829.28  nsp=0.919  | Chris will be up there going I'm
830.30-832.52  nsp=0.919  | Inside you so deep
```

文本与下一恢复段 `[294] 832.74 "inside you now, you now,"` 语义首尾相接（*"Inside you so deep" → "inside you now"*），确为真实对白 —— **假设 (a) 排除**。

**② demucs 分离人声后 whisper —— 文本与①逐字一致**（nsp=0.903，时间戳偏差 ≤0.04s）：该窗口**并非 BGM 掩蔽**，裸跑即可解出，故 F1′（洞头喂 G3/demucs）对本窗口**不必要**。

**③ 伴奏轨（other）对照 —— 仅产出 `"Thank you."`（829.70–832.50，nsp=0.673，alp=-0.521）**，正是 ADR-021 记录的经典幻觉样本；反证①/②文本来自人声轨而非噪声。

> ⚠️ 本节「独立成窗」用的是**人造窄窗**，结论方向对但论据不充分；真正的坐实见下节（用**正确的去重池**重跑）。

### 缓存假设不成立（时间戳证伪）

曾推测「产物是 ADR-036 修复前生成的 / 重跑命中旧缓存」，**已证伪**：

- `src/video_translate/fill_gaps.py` 最后修改 **12:00:39**
- 转写 chunk **12:54–12:56**、`review.log` / `review.json` / `segments_en.json` **12:59:43**

即补洞运行发生在修复之后，**用的就是带大 pad 的代码**，且日志为完整逐洞输出（非 `cache hit` 早退）。用户确认跑前已清空 `videos/` 下历史文件。**缓存不是本事故原因。**

### 决定性实验：用正确的去重池复刻 `_probe`（坐实主因 = 守卫）

上一轮 pad 复刻得出「守卫不是阻塞点」，是**去重池用错**导致的伪结论：真实运行时 `fill_gaps` 传入的 `dedupe_pool` 是**补洞前的输入段（446）**，而非 `segments_en.json`（补洞后 483，含 `_recovered` 段）。用 446 池重跑 `_probe_long_hole` 首子窗（`s0 = 821.44 - 6.0 = 815.44`，`s1 = 833.44`）：

| pad | 解码窗口 | 洞头语音 | 守卫后 | nsp |
|---|---|---|---|---|
| 0.2 / 0.0 / 0.5 / 2.0 / 6.0 | 各档 | 无（只吐旁白） | **0 段**，全 echo / 坍塌 | — |
| **4.0** | **811.44–837.44** | **有，3 段** | **0 段 —— 全被守卫丢** | **0.892** |

pad=4.0 的原始产出（`_is_echo` 放行，**`_is_recovered_hallucination` 信号 C 全部拦截**）：

```
DROPPED:echo   811.44-816.28 | We all know Walken's voice is an impressionist
DROPPED:echo   816.28-820.36 | and Jay Moore took their shot at his iconic vo
DROPPED:echo   820.52-821.44 | Time to find out.
DROPPED:guard  821.44-826.76 | There's one guy who could do and I think we al   nsp=0.892  <== HEAD
DROPPED:guard  827.30-829.28 | Chris will be up there going I'm                 nsp=0.892  <== HEAD
DROPPED:guard  830.22-837.06 | Inside you so deep inside you now and you now    nsp=0.892  <== HEAD
=> kept 0
```

**这精确复现了真实运行**：`kept 0` → coverage 0 → 首子窗返回空 → 日志中 `recovered[0]` 落在子窗 1 的 `'inside you now, you now,'`（832.74），洞头永久留空。

### 根因收敛（唯一）

**`_is_recovered_hallucination` 信号 C 误杀**：`no_speech_prob ≥ 0.6` 即判幻觉。而本窗口 whisper **一边吐出正确文本、一边自报 89.2% 非语音** —— 因为 recovery 是 `no_speech_threshold=0.0` **强制解码**，模型即便判定「非语音」也会照样解码；在低质量/特殊嗓音素材上，该自判**系统性失真**。

这正坐实了用户的直觉：**拼接素材质量参差 + Walken 式特殊嗓音 ⇒ whisper 的 no-speech 自判不可信 ⇒ 守卫据此误杀正确恢复。**

#### 事故几何数据（pad=4.0 解码 `[811.44, 837.44]` 的洞头 3 段）

| 段 | 时间窗 | 词数 | 零时长词 | nsp | avg_logprob | 现状 |
|---|---|---|---|---|---|---|
| 1 | 821.44–826.76 | **21** | 0 | 0.892 | **−0.2345** | 信号 C 丢弃 |
| 2 | 827.30–829.28 | **7** | 0 | 0.892 | −0.2345 | 信号 C 丢弃 |
| 3 | 830.22–837.06 | **16** | 0 | 0.892 | −0.2345 | 信号 C 丢弃 |

要点：
- **`avg_logprob = −0.2345 > −1.0` ⇒ 信号 C2 不参与误杀**，故只需修正信号 C。
- **零时长词 = 0 ⇒ 信号 D 未触发**；语速正常、无重叠 ⇒ A/B 未触发。**唯一杀手是 C。**
- 同一次解码中旁白段 nsp = 0.0094 而洞头 nsp = 0.892 ⇒ nsp 是**段级**而非窗口级（修正早前「窗口级」的表述）；其失真发生在**特定素材**上，而非整窗。

### F3 决策（契约）

**D6 — 信号 C 按段长分级：只对短段生效。**

```python
if nsp is not None and nsp >= no_speech_thr and nw < _NSP_SHORT_WORDS:   # 6
    return True
```

- **短段（< 6 词）**：维持原判据不变。全部已记录幻觉均为 1–4 词（`We'll be right back.` 4 词 nsp=0.766 / `Thank you.` 2 词 nsp=0.75 / `Get it.` 2 词 nsp=0.373），nsp 对它们仍是最强且必要的信号。
- **长段（≥ 6 词）**：不再单凭 nsp 判幻觉。能强制解出**成段连贯文本**本身即反证幻觉（whisper 幻觉以短促套话为特征）；且 A（重叠）/ B（语速）/ D（零时长词）与 `_is_echo` 仍覆盖其余情形。这与守卫既定的「保守、避免误杀真实恢复语音」取向一致。
- **阈值空隙**：事故三段词数 21 / 7 / 16 均 ≥ 6，`We'll be right back.` 4 词 < 6 —— 6 落在两者之间。

**明确不动**：信号 A / B / D 与信号 C2（`avg_logprob`）均不改。本事故 `alp = −0.2345 > −1.0`，C2 未参与误杀，无证据支持改动（最小变更原则）。

**残余风险**：≥ 6 词且 nsp 高的长段幻觉将不再被 C 拦截，仅由 A/B/D/echo 兜底。可接受 —— 已记录的幻觉样本无一属此类；且守卫本就倾向「宁可放过」。

### 附带发现：解码对窗口起点极度敏感

同一段素材，解码窗口仅差 **0.04s**，whisper 表现天差地别：

| 解码窗口 | 产出 | nsp | 结果 |
|---|---|---|---|
| `[811.40, 837.40]` | 12 段（洞头切成 6 段） | **0.006** | 守卫放行，**可救回** |
| `[811.44, 837.44]` | 6 段（洞头 3 段） | **0.892** | 守卫全丢，**救不回** |

即：**洞头能否恢复，取决于解码窗口起点那几十毫秒的运气** —— 恢复结果不稳定。这也解释了为何「其它类似情况都解决了，只剩这块儿」。

### 后续（据正确池实验最终修正）

- **F3（P0，唯一必要修复）**：修正 `_is_recovered_hallucination` 信号 C。窗口级 `no_speech_prob` 在强制解码（阈值 0.0）语境下**不可作为段级幻觉判据**。方向：
  1. 按段长分级阈值（长句放宽）—— 本事故首段 20+ 词；
  2. 或要求「nsp 高 **且**（段短 或 零时长词多）」才判幻觉；
  3. 保留对 `We'll be right back.`（0.766）/ `Thank you.`（0.799）等**短幻觉**的拦截回归。
- **~~F1（重扫残留洞）~~ 取消**：洞头已被 pad=4.0 解出，问题在守卫，不在扫描次数。
- **~~F1′（洞头喂 G3）~~ 取消**：非 BGM（demucs 结果逐字一致）。
- **F2（降级为独立改进项）**：缓存键不含解码逻辑指纹仍是真实缺陷（改 `_PROBE_PADS` 后已有视频不会自动重算），但**不是本事故成因**。
### F3 已落地 + 定向验证（真实音频）

**实现**：`fill_gaps._is_recovered_hallucination` 信号 C 改为
`nsp >= no_speech_thr and nw < _NSP_SHORT_WORDS`（新增模块常量 `= 6`），并补 D6 注释；**A / B / D / C2 一律未动**（最小变更）。

**回归**（`tests/test_fill_gaps_recovered_guard.py` 新增 5 例：事故 21/7/16 词三段 + 5 词下界 + 6 词上界；用例注释记录事故来源）：

- TDD 红灯：4 failed / 17 passed → 绿灯：**21 passed**
- 全量套件：**619 passed, 12 skipped, 3 deselected** —— 既有拦截（含 `We'll be right back.` 4 词 nsp=0.766）全部保持

**定向端到端**（真实音频 + 补洞前 446 段池，pad=4.0 解码 `[811.44, 837.44]`）：

| | 修复前 | 修复后 |
|---|---|---|
| kept | **0 / 6** | **3 / 6** |
| 洞内 coverage | **0** | **14.14 s** |
| 洞头 821.44–832.74 | 三段全丢 | **三段全保留** |

保留段：`821.44–826.76`（21 词）/ `827.30–829.28`（7 词）/ `830.22–837.06`（16 词）；三条旁白回声仍被 `_is_echo` 全部拦截，未引入重复字幕。coverage 14.14 s < 早停阈值 17.42 s，pad=4.0 仍为最优档。

**待办**：删 `<base>.review.json` 重跑本视频 `fill_gaps` 落盘；新增段需补译并重跑 generate/verify。

## 实测残留事故（Walken 10:02–10:09 大 pad 尾句回声）—— ADR-036b

- 状态：已修（P0 守卫补丁 + 定向回归）
- 关联：`_is_recovered_hallucination` 信号 E（新增）、`tests/test_fill_gaps_prefix_echo.py`

### 现象（用户报：10:05–10:09「前句后半 == 后句整句」重复）

`segments_en.json` 扫描发现 5 段 `recovered` 段与邻居**重叠 1.40–2.00s**，且文本重述邻居尾巴（+延续句）：

| 段 | 时间窗 | maxOverlap(邻居) | 重述邻居 |
|---|---|---|---|
| [91] | 280.55–284.49 | 1.53 ([92]) | "something you're gonna have to take with you to…" |
| [180] | 531.32–537.10 | 1.58 ([183]) | "Is there any dessert or what are we going to do now?" |
| [206] | 602.04–609.08 | 2.00 ([205]) | "and the jelly would go into the donut and it was very" |
| [258] | 738.04–746.58 | 1.45 ([257]) | "basically there's no" |
| [305] | 893.06–897.88 | 1.40 ([304]) | "And let's just say the encounter didn't disappoint." |

其中 [206] 即用户所报：`[205]` 末 "…would go into the donut and it was very" 被大 pad 解码窗整句拖回，重发为 `[206]` 开头并续接 "exciting and while some skits age"，导致 SRT 出现「前句后半 == 后句整句」的重复字幕。

### 根因（ADR-036 D1 + D6 的意外耦合）

- D1 把 `_PROBE_PADS` 扩到 `0.2/0.0/0.5/2.0/4.0/6.0`：大 pad 把解码起点推到前句开头以躲 prefix collapse。
- 但当 whisper **未** collapse 时，大 pad 把整句前句拖进窗，作为"恢复段"重发 → 该段**重叠前句 >1s** 且重述其尾巴。
- D6 把信号 C 放宽为**只拦短段**（`nw < 6`）；信号 A 只拦 `nw <= 4` 的重叠。这 5 段均为**长尾句**（词数 ≥6、重叠段词数大），两类信号都不触发 → 漏网插入时间轴。
- `_is_echo` 也漏过：重发段是"前句尾巴 + 新延续句"，与邻居非严格包含/高 jaccard（如 [206] jaccard([205],[206])≈0.5 < 0.6），故未被文本相似度拦下。

### 判别器（真实数据坐实，零误杀）

扫全部 3 个已处理视频的 `recovered` 段 `maxOverlap`：
- 真实恢复段（含 Walken 821–837s 救回的 3 段）重叠邻居 **≤ 0.50s**（最大 0.50）；
- 尾句回声段重叠 **≥ 1.40s**（最小 1.40）。

`0.50` 与 `1.40` 间存在干净空隙 → `_ECHO_OVERLAP = 1.0` 卡在中间，既拦住全部回声、又不误杀任何真实恢复。

### 决策（D7 / 信号 E，最小变更）

`fill_gaps._is_recovered_hallucination` 新增信号 E（仅洞补路径 `check_overlap=True` 生效；collapse/G1/G2 走 `check_overlap=False`，其窗口本就重叠父段，不受影响）：

```python
_ECHO_OVERLAP = 1.0
if check_overlap and _overlap_with_any(cand, segments) > _ECHO_OVERLAP:
    return True
```

语义：恢复段重叠任一已确认段 **> 1.0s** 即判为"重解已确认音频"的回声（非补洞）。真实补洞段起于洞边界、重叠 < 0.5s，天然不触发。

### 回归（TDD，事故几何数据）

`tests/test_fill_gaps_prefix_echo.py`（注释记录 5 例来源）：
- 单元：信号 E 拦截全部 5 例（长段、低 nsp 也拦）；`check_overlap=False` 路径不误杀；真实 ≤0.50s 重叠 / 恰好 1.00s 边界不误杀。
- 集成（mock Whisper）：洞 604.04–609.98，大 pad 重发前句尾巴 → 信号 E 丢弃；小 pad 掏出干净洞内对白 → 保留。断言无 `doughnut`（回声）泄漏、原段时间戳不变。
- 全量套件：本修复引入 **0 新增失败**（另有 4 例 `test_acoustic_verify` / `test_pipeline_field_contract` 失败属 merge/raw-index 子系统既有问题，与本改动无关，已 `git stash` 验证）。

### 待办（P1，需用户确认后执行）

修复已落代码层；**既有 Walken 产物仍含这 5 段回声**（review 缓存键不含守卫逻辑，ADR-036 §后续 F2 已记为真实缺陷）。清除步骤：
1. 删 `videos/Nobody Can Handle Christopher Walken's STRANGE Hum.review.json`（ invalidate 旧缓存）；
2. 重跑 `fill_gaps` → 新守卫丢弃 5 段回声、小 pad 干净补洞；
3. 因段数变化需**重译**（zh 索引对齐）→ 重跑 `generate` / `verify`（走 pipeline 翻译停点）。

#### P1 执行记录（用户确认 "走"，2026-09-03）

- 步骤 1 完成：删 `…Walken…review.json`。
- 步骤 2 完成：`uv run video-translate run …Walken….mp4 --skip translate --skip generate`
  （chunk_0..5 + whisperx 缓存复用，未重转写；重 merge + 重 fill_gaps）。
  - 校验：重扫 `segments_en.json`，**`recovered` 段与邻居重叠 >1.0s 的数量 = 0**（修复前 5）。
    原回声索引 [91]/[180]/[206]/[258]/[305] 现为完全不同、无 `_recovered` 标记、无重叠的干净段。
  - 真实恢复全部保留：802–811s `milk. This is tragic news…`、821–850s 长洞 8 段救援、
    894–906s `hi to him and the first thing…`（原 [305] 回声已消失）。
  - 总段数 484 → **481**（重 merge 边界收紧，3 段差异）；`translate_task.json` 已按 481 段重出。
- 步骤 3（翻译 + generate/verify）：**翻译 agent 职责（C3/C4，编码助手不执行）**。
  pipeline 已正确停在 `AWAITING_AGENT`（exit 6），待 agent 重译 481 段后再 `generate`/`verify`，
  新 SRT 即无重复。旧 `zh_segments.json`（484）已与现 segments 错位，generate 闸门会拦截（segments_sha 陈旧）。



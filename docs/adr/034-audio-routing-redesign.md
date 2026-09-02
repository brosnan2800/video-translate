# ADR-034 — 音频路由重构：默认裸跑 + 数据驱动闭环（S2）

Date: 2026-09-02
Status: Accepted
Companion: ADR-011/012（VAD/声学铁律）, ADR-015（adaptive-vad）, ADR-016（fill_gaps）
, ADR-017（vocal_sep）, ADR-020（hallucination）, ADR-021（recovered hallucination）
, ADR-032（决策点协议）, T8/ADR-033（pipeline 收口）, MAJOR_VERSION_PLAN T9.

> 本文是音频路由重构的**唯一事实来源**，含整体计划 + 详细设计 + 三期计划。
> 在 task plan 中可按章节引用：「执行 ADR-034 §6 一期计划」。

---

## 1. Context（背景）

### 1.1 触发：5:52 漏音根因

`Nobody Can Handle Christopher Walken's STRANGE Hum.mp4`（460 段）5:52 附近笑声下的
真音被系统性漏译。`vt_state.json` 落盘路由：

```jsonc
"routing": { "value": { "vad": true, "adaptive_vad": false, "vad_threshold": 0.1 } }
```

**根因**（ADR-015 §Context 已记录此现象）：全局 `--vad` 把「笑声+真音」连成一片 speech
segment，Silero VAD 把笑声当 segment，**ejecting the masked speech**（真音被挤出）。
这不是阈值问题（`0.1` 已是最灵敏档），是「对笑声 chunk 用 vad」这件事本身错。把
`0.1` 再调低只会把更多笑声当语音吞进去，反而更乱。

用户直觉诊断完全成立：「真音被相邻的笑声吸收/忽略了，不是阈值问题」。

### 1.2 画像机制缺陷（作为通用流水线依据薄弱）

`audio_profile.py` 的全局画像机制对**混合音频**视频失准：

- **整片 mean/max 标量**对混合音频失准：本片 `mean=-24.8` 判 low → 一刀切
  `vad=0.1`，但 mean 低只因为多数时间安静+偶尔笑声，与「轻语需要 recall」无关。
- **silencedetect `-30dB` 写死魔法阈值**，非针对「语音 vs 噪声」。
- **duration 缺失**（`analyze_audio` 不 probe，`_resolve_routing` 调用不传）→
  `continuous` 算不出 → `adaptive_vad`/`separate_vocals` 自动推荐**永远 False**
  （rationale「duration unknown -> continuous-noise detection skipped」）。
- `profile_recommendation` 的 `separate_vocals` 触发条件（`continuous = sf<0.10`）
  与「是否真需 Demucs」几乎无因果。

### 1.3 adaptive-vad 不是通用方案

即便 duration 接线，`--adaptive-vad` 只把「整片一刀切」降到「**固定 240s chunk 一刀切**」
（`route_vad_chunk` 对整 chunk 只出一个 bool）。chunk 内混合（clean+laughter 同块）
仍会漏，依据仍是脆弱 `silence_fraction < 0.10`。对通用流水线不可靠，且依赖 chunk 边界运气。

### 1.4 separate-vocals 的能力边界

Demucs `htdemucs` 是**音乐源分离模型**（vocals/drums/bass/other）：
- **强项**：音乐/伴奏/稳态 BGM 掩盖真音（ADR-015 亲口 true root fix for fully-buried speech）。
- **边界外**：笑声/欢呼/掌声与说话分离（笑声是人声，留 vocals，分不开）；多说话人重叠；
  短窗 <5s 质量差；CPU 10x 实时、8GB OOM 风险、磁盘占 vocals.wav。

**关键**：对「笑声掩盖真音」（用户 5:52 痛点），demucs 天然无能为力——这不是重不重的问题，
是能力边界外。笑声场景的对症方案是 G1（静音边界切分）+ G2（fill_gaps 扩展），不是 S4/demucs。

### 1.5 E1 验证结论（2026-09-02）

用 `videos/IF.mp4`（Kipling "If—" 诗朗诵，干净单一声学基线）跑 vad vs bare 对比
（`--align none` 看原始段边界）：

| 维度 | vad | bare | 判定 |
|---|---|---|---|
| 最终段数 | 33 | 33 | ✓ 一致（merge+split 消化原始差异） |
| word 级时间戳 | — | — | **< 0.2s，几乎逐字一致** |
| 段级 start/end 漂移 | — | — | 0.02–0.26s，**全部 < 0.3s** |
| 静音段幻觉 | 无 | drop seg#24 'Thank you.'（静音孤立） | 被 drop_hallucination 拦截 ✓ |
| 文本质量 | 各有优劣 | bare 不退化 | — |

**E1 判定**：漂移可控 → 画像门控保留的唯一理由（怕干净视频裸跑漂移）**不成立**
→ **走 S2（全裸跑 + 闭环），画像门控废弃**。省掉 S3 的画像门控工程。

诚实限定：单一干净视频样本，但对「干净单一声学」视频结论方向清晰；混合视频本就走闭环。

---

## 2. Decision（架构决策）

### 2.1 默认裸跑

通用流水线默认 `use_vad=False`（`no_speech_threshold=0.0` 已是默认，保留真音不被
eject）。`recommend_vad` 不再对 ok 画像返回 `--vad`——画像降级为参考信息，不驱动路由。

### 2.2 post-transcribe 双信号 review（塞进 fill_gaps）

转写后做双信号 review，识别「该有人声但被漏译/被合并」的可疑窗口。**塞进 fill_gaps**
（不新增状态机阶段，不动 ADR-030/T8 两停点）。

- **信号 A（Whisper 自报，二维）**：`no_speech_prob`（高 ≥0.6 → 弃段漏译嫌疑）/
  `avg_logprob`（低 < -1.0 → 幻觉嫌疑）。
- **信号 B（独立参照）**：原视频 silencedetect 真实静音区间 + segment 时间戳对齐。
  找「有声学能量但 Whisper 没出/低质」窗口（漏译）与「静音但 Whisper 出文本」窗口（幻觉）。
- **B 主导**：B=有能量 + A 异常 → 漏译嫌疑（重处理）；B=静音 + Whisper 出文本 →
  幻觉嫌疑（丢弃）。A 二维只决定「异常与否」，具体指向看 B。

### 2.3 梯度重处理 G1/G2/G3

| 梯度 | 手段 | 救什么 | 前提 |
|---|---|---|---|
| **G1** | 按 silencedetect 静音边界把可疑窗切细重喂 Whisper（局部 resegment） | 笑声后/笑声变小后的真音 | 窗内有静音气口 |
| **G2** | fill_gaps 扩展：补「段内有能量但 word 无覆盖的子窗」+ ADR-021 守卫防补出幻觉 | 被合并成一段、无段间空隙的漏译 | word 时间戳 + 能量检测 |
| **G3** | 局部 separate-vocals（四组进入条件 + 能量复核自校验） | 强 BGM/音乐掩盖真音 | demucs 能处理的掩盖物 |

**G3 不兜笑声**：demucs 分不开笑声，G3 场景预筛（组2）会挡掉笑声窗，标记「当前架构救不了」。

### 2.4 画像降级

画像降级为 doctor 打印的参考信息，**不驱动路由**。`duration` 接线仍做（给 G3 场景
预筛 + doctor 画像完整用），但不做「单一 vs 混合」门控。

---

## 3. 详细设计

### 3.1 双信号 review 判定表

| B（声学参照） | A 状态 | 判定 | 处理 |
|---|---|---|---|
| **有能量**（该有人声） | no_speech 高 **或** avg_logprob 低（含两者同高） | 漏译嫌疑（含「真音被漏+幻觉填充」混合） | 重处理（G1/G2/G3）拿真文本替换 |
| **静音**（不该有人声） | Whisper 出了文本（avg_logprob 高或低） | 幻觉嫌疑 | 丢弃/标记 |
| 有能量 | no_speech 低 且 avg_logprob 高 | 干净真音 | 保留 |

`no_speech_prob` 高 + `avg_logprob` 低 同时存在（噪声段硬编低质幻觉典型指纹）：B=有能量
→ 漏译嫌疑（重处理）；B=静音 → 幻觉嫌疑（丢弃）。判定清楚，B 是主裁判。

### 3.2 G1 vad 重切

机制：对 review 标的可疑窗，用 silencedetect 扫窗内静音边界，按边界切子段，重喂 Whisper。
切出的真音子段无笑声前缀污染，Whisper 能正确解码。

**救不了的情况**：窗口内无静音边界（笑声和真音时间完全重叠）→ G1 切不动，交给 G2/G3。

### 3.3 G2 fill_gaps 扩展

当前 fill_gaps（ADR-016）只补「相邻段间空隙」。盲区：Whisper 把「笑声+真音」合并成同一
segment 时无段间空隙，fill_gaps 不触发。

G2 扩展：补「**段内有能量但 word 无覆盖的子窗**」：
1. 对每个 segment `[s,e]`，拿 word 时间戳 + 原视频 silencedetect。
2. 找 segment 内有非静音能量、但该 segment 的 words 没覆盖的子区间 `[s1,e1]`。
3. 切 `[s1,e1]` 子窗，用 `TEMPERATURE_FALLBACK` 重喂 Whisper 拿补回 words。
4. 补回段过 `_is_recovered_hallucination` 守卫（ADR-021：重叠>0.12s/语速>8wps/低置信度丢弃）。

### 3.4 G3 局部 separate-vocals（四组进入条件）

**组1 硬前置**（全满足）：
- 该窗已通过双信号 review 标为可疑漏译（A∩B 命中）。
- G1 已跑且仍空。
- G2 已跑且仍空。
- 窗长 ≥5s（短窗合并邻近可疑段一起分离，demucs <5s 质量差）。

**组2 场景适配**（demucs 能处理的掩盖物）：
- (c) 画像预筛：该窗所在 chunk 画像标 strong-BGM/continuous-noise → 通过预筛。
- (a) demucs 试跑 + 能量复核：试跑 demucs，看该窗 vocals 轨能量 vs other 轨能量。other 高
  （有 BGM 被剥）→ demucs 起作用，继续；vocals 没变（笑声留 vocals，没东西可剥）→ 跳过。
- 双门：(c) 预筛挡掉明显笑声窗省 demucs，(a) 确认防画像误判。

**组3 性能预算**：所有进入 G3 的窗口总时长 < 视频时长 30% **且** < 10 分钟（取小）。
超限只取最可疑 N 窗，其余报告。

**组4 自校验退出**：demucs 重解码后再过双信号 review 复核。A∩B 不再命中 → 救回；
仍命中 → 标记「当前架构救不了」（可能完全重叠笑声+真音，需未来语音分类模型）。

### 3.5 demucs 能力边界（防误用）

| 场景 | demucs | whisperX | Silero VAD |
|---|---|---|---|
| 音乐/伴奏 ↔ 人声 | ✅ 强项 | ❌ diarization 不分音乐 | ❌ 音乐当 speech |
| 笑声/欢呼 ↔ 说话 | ❌ 笑声是人声留 vocals | ❌ 不区分笑声 | ❌ 笑声当 speech |
| 多说话人重叠 | ❌ 不按说话人分 | ⚠️ diarization 区分说话人 | ❌ |
| 稳态环境噪声 ↔ 人声 | ✅ | — | ⚠️ |

whisperX 是**词级强制对齐 + 说话人聚类**，不是「多人声分离」。它救不回漏译文本（对齐的是
已有文本），也不分离笑声。G3 用 demucs，whisperX 在 G1 切出的段做词级时间戳精修（T4 默认）。

---

## 4. 与 T8/ADR-032 关系 + 铁律5同步清单

### 4.1 架构正交声明

- **S2 改音频路由**（transcribe + fill_gaps 内部逻辑）。
- **T8/ADR-033 改流程编排**（pipeline 子命令 + 决策点）。
- 两者不重叠。S2 的 review 塞进 fill_gaps（不新增状态机阶段），不破坏 T8 的「决策点+
  翻译」两停点设计。T8 后做时 S2 的 review 已在 fill_gaps 里，pipeline 引擎自然驱动。

### 4.2 铁律5路由表同步清单（一期落地时同步）

S2 改 `recommend_vad`（ok 画像不再返回 `--vad`），以下文档要同步：

| 文档 | 同步内容 |
|---|---|
| `MAJOR_VERSION_PLAN.md` §0.2 铁律5 | 「正常电平 → `--vad` 锚静音」改成「正常电平 → bare+闭环」 |
| `ADR-011` | VAD 自动路由表更新（ok → bare） |
| `ADR-012` | 声学铁律不变（review 用原视频 silencedetect 独立参照，与 verify lane 一致） |
| `docs/TRANSLATION-WORKFLOW.md` (ADR-032) §2.2 | `recommend_vad` 纯函数描述同步 |

### 4.3 时序

S2 一期先做（默认 bare + duration 接线），不阻塞 T8。T8 在路线图里 T5-T8 顺次、
尚未落地；S2 先做，T8 后做时 review 逻辑已在 fill_gaps 里。

---

## 5. Consequences

### 5.1 涉及文件

- `src/video_translate/audio_profile.py`：`recommend_vad` 改（ok → bare）；`analyze_audio`
  接 duration probe（一期）；`profile_recommendation` 描述同步。
- `src/video_translate/transcribe.py`：默认 `use_vad` 路径已是 bare，确认；G1 局部 resegment
  接入（二期）。
- `src/video_translate/fill_gaps.py`：G2 段内漏译补洞扩展（二期）；post-transcribe review
  塞入（二期）。
- `src/video_translate/vocal_sep.py`：G3 局部 separate + 能量复核（三期）。
- `docs/`：ADR-011/012/032 + MAJOR_VERSION_PLAN §0.2 同步（一期）。
- `tests/`：每期 TDD 先行。

### 5.2 缓存指纹

- post-transcribe review + G1/G2 重处理做成**独立缓存层**（像 align 那样不进 transcribe
  指纹），裸跑那趟缓存稳定，破坏面小。
- G3 局部 separate 的 vocals.wav 缓存按窗（不入全局 chunk fingerprint）。

### 5.3 golden 迁移

默认切 bare 后历史 chunk cache 失效（fingerprint 变）。MAJOR_VERSION_PLAN §0.2 铁律6
已说 golden 停止仓库跟踪、本地手动确认。**单轨**（只维护 bare golden，vad 路径放弃）——
因 S2 废弃门控，vad 路径不再存在需回归。

### 5.4 测试计划

- 一期：`recommend_vad` 默认 bare 单测；duration 接线单测（mock ffprobe）；铁律5文档同步。
- 二期：双信号 review 纯函数单测（合成 no_speech_prob/avg_logprob/silence 信号）；
  G1 resegment 契约（mock）；G2 段内漏译补洞 + ADR-021 守卫单测。
- 三期：G3 进入条件四组单测；demucs 能量复核 mock；性能预算上限单测。
- 集成：`@pytest.mark.slow`，在 IF.mp4 + Walken 片上跑，验证 5:52 救回。

---

## 6. 三期计划

> 每期独立可交付、可验证。task plan 可按「执行 ADR-034 §6.X」引用。

### 6.1 一期（止血）

**目标**：通用流水线默认不再系统性 eject 笑声真音。

**动作**：
1. `recommend_vad` 改：ok 画像不再返回 `--vad`（返回 bare）；`profile_recommendation`
   不再推 vad（降级为参考）。
2. `analyze_audio` 接 duration probe（顺带 probe，给 G3 预筛 + 画像完整用）。
3. 铁律5同步：MAJOR_VERSION_PLAN §0.2 + ADR-011/012/032 §2.2 文档同步。
4. golden 本地重跑确认（单轨 bare）。

**涉及文件**：`audio_profile.py`、`MAJOR_VERSION_PLAN.md`、`docs/adr/011/012`、
`docs/TRANSLATION-WORKFLOW.md`、`tests/test_profile_recommendation.py`。

**验收**：
- `recommend_vad(ok_profile)` 返回 bare（单测绿）。
- 默认 `run` 不带 flag 走 bare（不再全局 vad）。
- IF.mp4 重跑 segments 与 E1 bare 版本一致（漂移 <0.3s）。
- Walken 片重跑后 5:52 附近不再被全局 vad eject（虽一期还没 review 救回，但不再
  系统性丢弃——裸跑 no_speech=0.0 保留真音）。
- 全量 pytest 绿。

### 6.2 二期（闭环）

**目标**：笑声后/笑声变小后的真音自动救回。

**动作**：
1. post-transcribe review 模块（双信号 A 二维 + B 主导，塞进 fill_gaps）。
2. G1 vad 重切（按 silencedetect 边界局部 resegment）。
3. G2 fill_gaps 扩展（段内有能量无 word 覆盖的子窗补洞 + ADR-021 守卫）。
4. review + G1/G2 做成独立缓存层（不进 transcribe 指纹）。

**涉及文件**：`fill_gaps.py`、`transcribe.py`（G1 resegment 接入）、新 review 逻辑、
`tests/`。

**验收**：
- 双信号 review 纯函数单测绿（合成信号驱动）。
- Walken 片 5:52 附近笑声后真音被 G1/G2 救回（人工听 vocals 确认）。
- G2 补回段过 `_is_recovered_hallucination` 守卫，不补出幻觉。
- 独立缓存层生效：重跑只重处理可疑窗，不重跑全片。
- 全量 pytest 绿。

### 6.3 三期（兜底）

**目标**：强 BGM/音乐掩盖的真音自动救回。

**动作**：
1. G3 局部 separate-vocals（四组进入条件：硬前置 + 场景适配双门 + 性能预算 + 自校验）。
2. demucs 能量复核（试跑看 other 轨能量确认 demucs 起作用）。
3. G3 自校验退出（重解码后再过双信号 review 复核，仍漏标记放弃）。
4. 完全重叠的笑声+真音标记「当前架构救不了」（未来语音分类模型）。

**涉及文件**：`vocal_sep.py`（局部 separate + 能量复核）、`fill_gaps.py`（G3 接入）、
`tests/`。

**验收**：
- G3 四组进入条件单测绿。
- 强 BGM 视频（如原声带片）的真音被 G3 救回。
- 笑声窗被场景预筛挡掉，不白跑 demucs（单测验证）。
- 性能预算上限生效（G3 总时长 < 30% 且 < 10min）。
- 全量 pytest 绿。

---

## 7. 验收标准（汇总）

- **一期**：默认 bare 生效；5:52 类不再系统性 eject；铁律5文档同步。
- **二期**：5:52 笑声后真音自动救回；G2 守卫防幻觉；独立缓存层生效。
- **三期**：强 BGM 真音自动救回；G3 不白跑 demucs；性能预算生效。

每期独立可交付、可回滚（独立缓存层 + 独立 ADR 章节）。

---

## 8. 风险与回退

| 风险 | 触发 | 对策 |
|---|---|---|
| 默认 bare 后干净视频漂移超预期 | E1 单样本结论不推广 | 二期 review + G1 兜底；必要时局部回退 vad |
| G2 补洞补出幻觉 | TEMPERATURE 激进 | ADR-021 守卫兜底；先用已有 `[0.0,0.2,0.4]` |
| G3 demucs 短窗质量差 | <5s 窗 | 合并邻近可疑窗一起分离 |
| G3 误触笑声窗白跑 | 场景预筛失效 | (c) 画像 + (a) 能量复核双门 |
| golden 全失效 | 默认切 bare | 单轨重跑本地确认（铁律6 已停 git 跟踪） |

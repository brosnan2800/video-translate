# ADR-031: 恢复段质量硬化 —— resegment 守卫、恢复段可见化、verify 声学补盲、state decisions 保全、BGM 能量分级

- 状态：接受
- 日期：2026-09-01
- 关联：ADR-020（尾部回音幻觉防御）、ADR-021（fill_gaps 恢复段守卫）、ADR-016（uncovered-audio）、ADR-012（声学时间戳真相）、ADR-030（控制平面）、`fill_gaps.py` `_is_recovered_hallucination`、`verify.py`、`cli.py` `cmd_resegment`/`cmd_verify`、`state.py` `ensure_state`

## 背景

kathy_meta_vlog（86s Meta 眼镜 Vlog）全片实测暴露了恢复段质量防线的四个真实缺口：

1. **"We'll be right back."（17.47-18.87s）**：resegment 轮 1 从 uncovered 窗解码产出，
   `no_speech_prob=0.90625`（Whisper 自判 90.6% 非语音）——fill_gaps 守卫阈值（≥0.6 即丢）
   足以拦下它，但 `cmd_resegment` 对窗口解码产出**只打 lang 标签直接拼接**，守卫不覆盖
   resegment 路径。该段在 segments_en.json 中无任何 origin 标记，下游（verify/语义回读）
   完全不可见。同类的 "Wait."（nsp=0.851，轮 2 产出）同样逃过。
2. **"Is he not going to make it?"（39.96-43.94s）**：fill_gaps 恢复段（nsp=0.486 擦线）
   的 "Is"（39.96-40.34）**骑在前段 "busy."（39.93-40.16）的词上**，与前段重叠 0.2s；
   用户实听确认真实语音只有 "not going to make it"——幻觉前缀。人声轨能量证据：该 4s 窗
   mean −35~−61dB / max −22.6dB，比确认语音基线（36-38s，max −6.1dB）低 16~35dB。
   verify 声学 lane 既不查段级置信度、也不查相邻段重叠/词碰撞，全部漏过。
3. **语义回读橡皮图章**：恢复段在 `semantic_reread_task` 中无任何标记，agent 只能凭
   「语境成立」放行——而「生成最像该语境的话」恰是 Whisper 幻觉的定义特征。
   语义合理性 ≠ 声学存在性。
4. **state decisions 被抹**：`ensure_state` 的 rebuild 启发式在 segments 文件 sha 变化
   （= resegment 合法修订）时整档重建，`decisions` 全部丢失（实测 `vt_state.json`
   decisions == {}）。

## 决策

### D1 — resegment 产出必须过 `_is_recovered_hallucination` 守卫（P0）

`cmd_resegment` 在拼接前对每个窗口解码段调用 fill_gaps 同一道守卫
（`_is_recovered_hallucination(seg, kept + accepted, check_overlap=True)`，阈值与
ADR-021 完全一致）。命中即丢弃并打印
`[resegment] hallucination guard DROPPED <span> <text> (no_speech_prob=…, avg_logprob=…)`——
**拦截必须可见，不许静默**；done 行携带丢弃计数。保留段打 `origin: "resegment"`（D2）。

### D2 — 恢复段可见化（P0）

- `verify.is_recovered_segment(seg)`：`_recovered` truthy 或 `origin` 非空（≠"whisper"）。
- `build_semantic_reread_task` 对恢复段 pair 附 `suspect: true` + `hint`
  （"确认声学证据，不只靠语义合理性"）。
- `cmd_verify` 报告打印恢复段清单（informational，不参与红灯——置信度巡检 D3 才是闸）。

### D3 — verify 段级置信度巡检（P1）

`verify.find_low_confidence_segments(segments, no_speech_thr=0.6, logprob_thr=-1.0)`：
对携带 `no_speech_prob`/`avg_logprob` 的段（缺失字段跳过——主转写段经 merge 不携带），
`nsp ≥ 0.6` 或 `alp < −1.0` → issue `low-confidence`。并入声学 lane（strict 红）。
阈值与 ADR-021 守卫一致。

### D4/D5 — verify 相邻段重叠 / 词碰撞巡检（P1，只报告不修剪）

`verify.find_adjacent_overlaps(segments, min_overlap=0.05)`：按 start 排序，后段 start
落进前段 end 之前超过 0.05s → issue `adjacent-overlap`（两句话不能共享同一墙钟音频）。
后段**首词**起点早于前段**末词**终点 → `word_collision: true`；后段为恢复段 →
`hint: "suspected hallucinated prefix"`（D5，"Is" 骑 "busy." 指纹）。
**只报告，绝不自动修剪时间戳**（ADR-012 红线）；修法走 resegment/人工，不走自动改字面。
并入声学 lane（strict 红）。

### D6 — `ensure_state` rebuild 保全 `decisions`（P2）

rebuild 前先 `load()`；已有 state 携带 dict 型 `decisions`（及 `video` 路径）时合并回
重建结果。缺失/损坏行为不变（照旧从产物重建）。

### D7 — uncovered 窗 BGM 能量分级（P2）

- `audio_profile.probe_volume_window(path, start, end)`：窗口 volumedetect，复用
  `parse_volumedetect`（subprocess 层归 audio_profile，与 `analyze_audio` 作伴）。
- `verify.classify_vocals_energy(mean, max)` / `classify_uncovered_windows(uncovered, volumes)`：
  纯函数。阈值（kathy_meta_vlog 实测标定，与轮 2 人工仲裁同标准）：
  - `mean ≤ −36dB` → `bgm`（"likely BGM residue — adjudicate as music bed, no resegment needed"）
  - `mean ≥ −25dB` 或 `max ≥ −12dB` → `speech`（"vocal energy present — resegment this window (guard is built in)"）
  - 其余 → `ambiguous`（"mid energy — listen and adjudicate manually"）；探测失败 → `unknown`。
- `cli._find_vocals_wav(segments_path)`：glob `<base>.*.vocals.wav`（与 segments 同目录）；
  不存在 → 跳过分级（行为不变）。分级结果打印在 uncovered 行内（建议性质，
  uncovered 本身已是红，不改变 any_flag）。

## 测试

- `tests/test_resegment_guard.py`：nsp=0.906 丢弃且打印可见 / 干净段保留+origin /
  0.59-0.61 阈值边界 / alp<−1.0 单独命中 / 短段骑在保留段音频（overlap 信号）。
- `tests/test_verify_hardening.py`：真实事故几何（39.26-40.16 vs 39.96-43.94）flag +
  word_collision + prefix hint / 0.03s 模糊边界不 flag / 置信度阈值边界 / 恢复段 pair
  suspect+hint / BGM/语音/ambiguous/unknown 分级。
- `tests/test_verify_gate.py`（追加）：low-confidence → exit 8 / adjacent-overlap →
  exit 8 / vocals 存在时 uncovered 行带 [bgm] 分级 / 无 vocals 跳过 / 回读 task 带 suspect。
- `tests/test_state_chain.py`（追加）：stale sha rebuild 保留 decisions / 缺失重建
  decisions 为空。

## 后果

- resegment 与 fill_gaps 的恢复段质量标准统一为 ADR-021 一道守卫；幻觉拦截从
  「单点」变「全覆盖」，且拦截可见可审计。
- verify 声学 lane 新增三类红灯（置信度 / 相邻重叠 / 词碰撞），存量字幕可被扫出
  kathy_meta_vlog 的 idx2/idx15（这正是设计意图——当时的交付属于漏网）。
- 语义回读从「语义合理性」升级为「恢复段必须声学证据复核」。
- state 链 decisions 在合法修订后不再丢失，decisions=origin 裁决真正可追溯。
- 无 breaking change：所有新增字段/finding 对旧产物向后兼容（缺失字段跳过）。
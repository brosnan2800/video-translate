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

> **⚠️ 已被 [ADR-041](041-verify-decoupled-from-asr-self-report.md) supersede（2026-09-16）**：
> `find_low_confidence_segments` 已从 verify **整体移除** —— 它用 ASR 模型自身的评分字段
> （`no_speech_prob` / `avg_logprob`）巡检 ASR 自己的产物，属**自证**而非独立验证。
> 该能力已归位到① ASR 层内部（幻觉过滤 + `review` 的 A∩B 重处理判定）。
> 以下为历史记录，**不再生效**。

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
- `tests/test_verify_gate.py`（追加）：~~low-confidence → exit 8~~（**该用例已随 ADR-041
  移除 D3 一并删除**）/ adjacent-overlap → exit 8 / vocals 存在时 uncovered 行带 [bgm]
  分级 / 无 vocals 跳过 / 回读 task 带 suspect。
- `tests/test_state_chain.py`（追加）：stale sha rebuild 保留 decisions / 缺失重建
  decisions 为空。

### D8 — verify 重试次数限制 + P0→P1 批准门 (P1/P2)

> **动因**：过去的修复迭代常达到 5-8 次 verify 才清，Agent 陷入「修 fix 的 fix」的
> 本地局部最优振荡，每次还要从 state 里猜当前是第几次。kathy_meta_vlog 本次
> ADR-031 修复就经历了 3 轮 resegment→generate→verify，第 3 轮仍残 6 窗不可收。

**策略**：
- **计数器**：`stages.verify.attempts` in `vt_state.json`，每次 `cmd_verify` 调用 +1。
- **重置**：`run`（完整流水线）重置为 0；`generate`/`resegment`（局部重跑）**不重置**。
- **行为**：
  | 次数 | strict 模式 | exit 码 |
  |---|---|---|
  | 1–2 | strict 默认，红灯 exit 8 | 8 |
  | 3+ | **强制降级报告模式**（打印问题列表，NOT gate 失败） | 0 |
- 强制降级在**代码层**实现（`cmd_verify` 内部计数+降级），Agent 无法绕过 —— 符合
  「闸门只依赖产物文件，state 是增强」的原则：计数器是增强，真正的 gate 判
  据仍来自 segments_en.json / zh_segments.json。

**P0→P1 决策点（翻译流水线）**：翻译流水线 P0（环境体检 + 音频画像）与 P1（转写）之间的
引导选择式决策点，现**已代码化**（非纯 AGENTS.md 约束，零代码时代已结束）：

- `audio_profile.profile_recommendation()` 产出风格 / VAD / 人声分离三项结构化推荐，
  doctor 与 run 共用同一纯函数，杜绝双份逻辑漂移（ADR-032）。
- `cmd_run` 入口强制：`decisions.audio_profile` 无快照则自动补画像并落盘，**绝不裸跑无画像**；
  三决策按 `CLI flag > routing > 画像推荐` 合并，以 origin 分级（explicit/profile）落盘
  `decisions.routing`。
- `--require-profile` 为可选硬闸：要求已存在 `origin=explicit` 的 routing，缺失则 run 直接
  exit 8，防止 Agent 失守时裸跳过决策点。
- Agent 协议层（决策点提问 + 5 分钟超时）见 AGENTS.md「Agent 决策点协议」；超时值由
  `VT_DECISION_TIMEOUT_SECONDS`（默认 300s）配置。

## 测试

- `tests/test_resegment_guard.py`：nsp=0.906 丢弃且打印可见 / 干净段保留+origin /
  0.59-0.61 阈值边界 / alp<−1.0 单独命中 / 短段骑在保留段音频（overlap 信号）。
- `tests/test_verify_hardening.py`：真实事故几何（39.26-40.16 vs 39.96-43.94）flag +
  word_collision + prefix hint / 0.03s 模糊边界不 flag / 置信度阈值边界 / 恢复段 pair
  suspect+hint / BGM/语音/ambiguous/unknown 分级。
- `tests/test_verify_gate.py`（追加）：~~low-confidence → exit 8~~（**该用例已随 ADR-041
  移除 D3 一并删除**）/ adjacent-overlap → exit 8 / vocals 存在时 uncovered 行带 [bgm]
  分级 / 无 vocals 跳过 / 回读 task 带 suspect。
- `tests/test_verify_retry_limit.py`（新增）：attempt 1-2 strict（红灯 exit 8）/ attempt 3+
  强制报告模式（exit 0）/ 计数器落盘 `stages.verify.attempts`。
- `tests/test_resegment_guard.py`（追加）：`test_resegment_resets_verify_attempts_on_new_run` /
  `test_run_command_resets_verify_attempts`：
  `run` 重置计数器，`generate`/`resegment` 不重置。
- `tests/test_state_chain.py`（追加）：stale sha rebuild 保留 decisions / 缺失重建
  decisions 为空。

- resegment 与 fill_gaps 的恢复段质量标准统一为 ADR-021 一道守卫；幻觉拦截从
  「单点」变「全覆盖」，且拦截可见可审计。
- verify 声学 lane 新增三类红灯（置信度 / 相邻重叠 / 词碰撞），存量字幕可被扫出
  kathy_meta_vlog 的 idx2/idx15（这正是设计意图——当时的交付属于漏网）。
- 语义回读从「语义合理性」升级为「恢复段必须声学证据复核」。
- state 链 decisions 在合法修订后不再丢失，decisions=origin 裁决真正可追溯。
- 无 breaking change：所有新增字段/finding 对旧产物向后兼容（缺失字段跳过）。

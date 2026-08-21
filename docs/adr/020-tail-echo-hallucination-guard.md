# ADR-020 — 尾部回音幻觉防御（确定性时间嵌套信号 + Whisper 置信度信号）

- 状态：接受
- 日期：2026-08-21
- 关联：ADR-012（声学时间戳真相 / 静音窗信号）、Spec 12（词级时间戳）、Spec 02（transcribe）、Spec 18（verify 三 lane）、`merge.py` `drop_hallucination_segments`、`transcribe.py` `_seg_to_dict`

## 背景

V4 的 `drop_hallucination_segments` 用**双信号**（word 塌缩率 ≥50% + 与邻居共享 ≥3 词连续 n-gram）拦截无声学支撑的幻觉段；ADR-012 又补了**第三信号**（整段落在 `silencedetect` 静音窗）。三者在 sitcom《Everybody Loves Raymond》实战中暴露盲区：

实战中 57s 附近出现 `I'm not hungry either way.` 紧接真实句 `give me a yogurt either way.`：
- word 级时间戳证明它是幻觉：前缀 `I'm`/`not`/`hungry` 零时长压在 54.22-54.26；尾部 `either way` 与真实句**逐字精确重叠**（DTW 把幻觉得推到邻居词边界上）。
- 但双信号**双双差一点**：塌缩率仅 3/5=40%（<50%，因为 `either way` 借到真实音频、不零时长）；共享 n-gram 仅 `either way` = 2 词（<3）；静音窗信号也不触发（笑声有能量）。
- 后果：过滤器放行幻觉段 → SRT 生成器把真实句截短成 0.64s（一闪而过），幻觉句插在后面。

全片用 whisper 窗口重转写做声学真值确认，共筛出 **6 条同类尾部回音幻觉**（15/18/338/368/394/398），全部是「复述上一句的尾部 + 与前驱共享音频」。这是 Whisper 自回归解码的固有缺陷（窗口无清晰语音时「脑补」最像话的文本，并延续最近上下文把刚说的复述一遍），不是本项目特有 bug。

候选方案：
- **(A) 维持双信号 + 手工救火**：每次靠 agent 对照 whisper 重转写补刀。代价：重复、易漏、不可扩展。
- **(B) 升级过滤器为确定性 + 置信度双冗余（本 ADR）**：把人工诊断用的两个判据固化为自动信号——（4）word 级共享音频（确定性几何指纹）；（5）Whisper 自带 `avg_logprob`/`no_speech_prob`/`compression_ratio`（转写层先保留字段）。

## 决策

选 **(B)**，在 `merge.py` 的 `drop_hallucination_segments` 新增第四、第五信号：

1. **第四信号（时间嵌套，确定性）**：段 B 的**整个时间窗口被邻居段 A 的时间窗口包含**（B 骑在真实语音上，DTW 把回声词压到 A 的音频上），**且** B 内存在 ≥1 个零时长词 → 判定为回音幻觉。
   - 确定性：纯时间戳几何判定（区间包含 + 零时长词），不依赖任何概率阈值。
   - 区分边界模糊：真实相邻段只会边界交叉（后段 start 略早于前段 end），但**不会整体嵌套进邻居窗口**；本信号因此不会误杀正常相邻说话。最初草案用「词级重叠比例 ≥0.5」会误杀边界模糊的相邻段（如 `I like it` 首词区间被后驱 `like it with pizza` 的 "like" 锚点覆盖），改为区间嵌套后消除该误杀。
   - 复现：本次 6 条嵌套回声（39/101/162/339/341/433）100% 命中，并经验证窗口重转写确认无独立语音。
2. **第五信号（低置信度）**：Whisper 段级 `avg_logprob` < `avg_logprob_thr`（默认 -1.0）且（`no_speech_prob` 缺失或 ≥ `no_speech_thr` 默认 0.6）→ 判定为幻觉。
   - `avg_logprob` 是可靠半边：复述型幻觉在无声学支撑下输出文本，token log-prob 显著低于真实语音。
   - `no_speech_prob` 在笑声/掌声场景不一定高（有能量），故仅作 gate，避免误杀。
   - 字段由 `transcribe.py` 的 `_seg_to_dict` 从 faster-whisper Segment 携带进 `segments_en.json`（向后兼容：缺失字段时信号自动 inert）。

两个新信号与原有三信号**独立并存**（任一命中即丢弃），且不改动时间戳（遵守 ADR-012 不变量：只删除、不重算）。

## 理由

- **根因是确定性的**：尾部回音的 DTW 塌陷本质是「幻觉得推到邻居音频上」，这是一个可几何验证的事实，比任何概率阈值更稳。
- **置信度字段是免费午餐**：faster-whisper Segment 自带 `avg_logprob`/`no_speech_prob`/`compression_ratio`，原转写层丢弃是浪费；携带后过滤器无需重解码即可用 Whisper 自己的判据。
- **误伤可控**：第四信号要求「共享音频 + 零时长词」合取，真实叠音/相邻说话无零时长词，必不触发；第五信号阈值保守（avg_logprob -1.0 远低于真实语音常见 -0.3~-0.6）。
- **可回归**：本次 6 条真实样本已固化为单元测试（`test_acoustic_verify.py` 的 `test_drop_hallucination_audio_sharing_echo` 等）。

## 后果

- **`src/video_translate/merge.py`**：
  - `drop_hallucination_segments` 新增参数 `nested_eps=0.1`、`avg_logprob_thr=-1.0`、`no_speech_thr=0.6`。
  - 新增辅助函数 `_is_time_nested`、`_has_zero_dur`、`_low_confidence`。
- **`src/video_translate/transcribe.py`**：
  - 新增 `_seg_to_dict(s, offset)`，从 faster-whisper `Segment` 抽取 segment dict 并携带 `avg_logprob`/`no_speech_prob`/`compression_ratio`（缺失则省略，向后兼容）。
  - `transcribe_video` 与 `transcribe_window` 均改用 `_seg_to_dict`，缓存指纹因 payload 改变而自动失效（安全）。
- **`tests/test_acoustic_verify.py`**：新增第四/五信号单测（含真实样本回放与无零时长词不误伤）。
- **`tests/test_transcribe_contract.py`**：新增 `test_transcribe_carries_confidence_fields` 验证字段序列化。
- **AGENTS.md / Spec 文档**：更新幻觉拦截描述，纳入第四、第五信号。

## 已知限制

- 第四信号要求段含 word 时间戳且能判定窗口嵌套；若某段缺 words 或窗口不嵌套邻居，信号 inert（不会误杀，但也不会拦截该回音）。
- 第五信号的 `avg_logprob` 阈值依赖模型规模/语言；large-v3 实测 -1.0 安全，换小模型可能需要放宽。
- 多说话人同时发言（叠音）且 Whisper 拆成两段、两段均含零时长词且一段整体嵌套进另一段时，第四信号理论上可能误杀——但影视场景 Whisper 通常会把叠音合并进单段，实战未观测到该误杀。如未来出现，可加「文本不相似则不触发」的二次 gate。

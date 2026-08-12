# ADR-011 — VAD 由默认开改为选开（默认关 / 裸跑）

- 状态：接受
- 日期：2026-08-12
- 关联：Spec 02（`transcribe`）、V10

## 背景
早期 `transcribe` / `run` 默认开启 Silero VAD（`vad_filter=True`）。在 Jamie Foxx
粉丝混剪（2026-08-10，笑声 / 配乐 / 重叠说话多、信噪比低）实战中发现：VAD 把
"笑声 / 配乐里的说话"整段判为静音并丢弃，造成**大段漏字**（段数明显偏少，如 59s
只吐 1 段）。即便关掉 VAD，`no_speech_threshold`(默认 0.6) 仍是一道静音闸门，
二者叠加才是低信噪比漏字的真正根因（见 [Spec 16](../specs/16-fill-gaps.md)）。

候选方案：
- **(A) 保持 VAD 默认开**：音乐重 / 轻低音 / 耳语类视频持续漏切，需每次手动 `--no-vad`。
- **(B) VAD 改为选开（默认关 / 裸跑）**：默认即裸跑，音乐重类视频天然修好；干净录音再显式 `--vad`。

## 决策
选 **(B)**：VAD 改为**选开**，`transcribe` / `run` 默认 `use_vad=False`（裸跑）。
需 VAD 时显式传 `--vad`。

理由：
- 决定因素是**内容类型（音乐重 / 低信噪比 vs 干净录音），不是视频长度**。访谈 /
  脱口秀等干净人声几乎不出漏字问题，VAD 默认开反而是负担。
- 裸跑 + `no_speech_threshold=0.0` + 温度回退（V10）已能覆盖绝大多数场景；残留
  空洞由 [Spec 16](16-fill-gaps.md) 的强制解码兜底，无需 VAD 前置切割。
- 指纹（chunk 缓存名 sha1）**包含 VAD 开关**，切 VAD 时旧缓存自动失效，不会用到
  错误参数的缓存。

## 后果
- `transcribe_video(use_vad=...)` 底层 `model.transcribe(vad_filter=use_vad)`；
  默认 `False`。
- CLI：`transcribe` / `run` 新增 `--vad`（默认关）；`fill_gaps` 探针同样默认
  `use_vad=False`，避免重新引入漏切。
- 文档：README / AGENTS.md 同步标注「VAD 选开、默认裸跑（V10）」；旧文档里
  `--no-vad` 写法已纠正为 `--vad`（见 [Spec 16](../specs/16-fill-gaps.md) 备注）。
- 已知限制：耳语段（声学不像语音）VAD 全切、loudnorm 也无效，必须关 VAD 裸跑，
  whisper 可能误识，需按语境 / 原文台词纠正（见 AGENTS.md V7 章节）。

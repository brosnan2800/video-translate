# docs/ — 文档索引

> video-translate 文档库总入口。**先按角色选文档，不要通读。**

| 你是谁 | 从哪开始 |
|---|---|
| **AI Agent**（被召唤来翻译视频） | [`AGENTS.md`](../AGENTS.md)（必读：执行协议 + 避坑红线）→ 用 `pipeline` 单一入口推进 |
| **新贡献者**（搭环境跑通） | [`TOOLCHAIN.md`](../TOOLCHAIN.md) → [`TOOLING.md`](TOOLING.md) |
| **想了解全局规划** | [`MAJOR_VERSION_PLAN.md`](../MAJOR_VERSION_PLAN.md) |
| **想查「为什么这么设计」** | [`adr/`](adr)（架构决策记录，34 篇） |
| **想查「行为契约是什么」** | [`specs/`](specs)（行为规格，24 篇） |
| **想查历史 / 事故复盘** | [`HISTORY.md`](HISTORY.md) · [`POSTMORTEM-JamieFoxx.md`](POSTMORTEM-JamieFoxx.md) |

---

## 目录结构

```
docs/
├── index.md                  本文件：文档库总索引
├── TOOLING.md                工具与依赖管理（操作手册）
├── HISTORY.md                版本演进史（V3–V14）与实战案例
├── POSTMORTEM-JamieFoxx.md   Jamie Foxx 混剪事故复盘（V8–V13）
├── RESEARCH-voice-pro.md     Voice-Pro 对标研究（E1–E4 论证来源）
├── adr/                      架构决策记录（不可变历史，001–036）
├── specs/                    行为规格契约（00–24）
└── archive/                  已归档（历史/废弃，不参与日常查阅）
```

---

## 维护约定（单一维护源）

**同一内容只在一处维护。** 非主源处只保留指向主源的短指引，不复制正文。

| 内容类型 | 唯一主源 | 规则 |
|---|---|---|
| 决策理由 | `adr/NNN-*.md` | **不可变**：Accepted 后不改正文；要变更请新增 ADR 并在旧 ADR 标注 Superseded |
| 行为契约 | `specs/NN-*.md` | 随代码更新 |
| 操作手册 | `TOOLING.md` / [`TOOLCHAIN.md`](../TOOLCHAIN.md) | 随代码更新；**不复述 ADR 的决策论述** |
| 依赖/工具规则 R1–R7 | [`MAJOR_VERSION_PLAN.md`](../MAJOR_VERSION_PLAN.md) §3.2 | 规则源头 |
| Agent 红线 / 踩坑 | [`AGENTS.md`](../AGENTS.md) §1 | 红线主源；`specs/07-gotchas.md` 为开发者视角补充 |
| CLI 参数速查 | [`README.md`](../README.md) | 用户向主源；`specs/11-cli-v2.md` 为 CLI 契约 |
| 版本历史 | [`HISTORY.md`](HISTORY.md) | 唯一变更日志 |

**命令入口**：一切命令统一 `uv run` 前缀（[Spec 23](specs/23-environment-location.md) /
[ADR-029](adr/029-command-entry-uv-run.md)）。**`Makefile` 已移除** —— 文档中若出现
`make setup` / `make doctor` / `make test`，等价为 `uv run video-translate setup` /
`uv run video-translate doctor` / `uv run pytest`。

---

## ADR 索引（按主题）

### 环境与工具链
| ADR | 主题 | 状态 |
|---|---|---|
| [001](adr/001-cpu-int8.md) | CPU int8 量化 | **Superseded by 014** |
| [014](adr/014-cuda-device-abstraction.md) | CUDA 设备抽象 | Accepted |
| [023](adr/023-dependency-uv-lock.md) | `uv.lock` 可复现依赖（E1） | Accepted（已落地） |
| [024](adr/024-ffmpeg-auto-download.md) | ffmpeg 便携版自动下载（E2） | Accepted（已落地） |
| [025](adr/025-model-cache-self-heal.md) | 模型缓存校验 + 自愈（E3） | Accepted（已落地） |
| [026](adr/026-cuda-venv-torch-lib.md) | CUDA 解析 venv torch/lib 优先（E4） | Accepted（已落地） |
| [029](adr/029-command-entry-uv-run.md) | 命令入口统一 `uv run` | Accepted（已实现） |

### 声学与时间戳
| ADR | 主题 | 状态 |
|---|---|---|
| [008](adr/008-stable-ts-spike-rejected.md) | stable-ts Spike | **已回退**（拒收，改走路线 A） |
| [009](adr/009-silence-preservation.md) | 静音保留 | 接受 |
| [012](adr/012-acoustic-timestamp-truth.md) | 声学时间戳真值 | 接受 |
| [013](adr/013-whisperx-gpu-forced-alignment.md) | WhisperX GPU 强制对齐（高层决策） | 接受（**实现层见 028**） |
| [022](adr/022-smart-break-point-and-leading-orphan-rejoin.md) | 智能切点 + 句首孤儿并右 | 接受 |
| [028](adr/028-whisperx-alignment-pass.md) | WhisperX 对齐 pass（T4 落地） | 接受（已实现） |

### VAD 与音频路由
| ADR | 主题 | 状态 |
|---|---|---|
| [011](adr/011-vad-opt-in.md) | VAD 改为选开（默认裸跑） | 接受（**被 034 部分降级**） |
| [015](adr/015-adaptive-per-chunk-vad.md) | 按 chunk 自适应 VAD | Accepted（**被 034 部分降级**） |
| [034](adr/034-audio-routing-redesign.md) | 音频路由重构（默认裸跑，画像仅供参考） | Accepted |

### 召回与补洞
| ADR | 主题 | 状态 |
|---|---|---|
| [016](adr/016-recall-recovery-net.md) | 召回恢复网 | Accepted |
| [017](adr/017-vocal-separation.md) | 人声分离（T2） | Accepted |

### 幻觉拦截
| ADR | 主题 | 状态 |
|---|---|---|
| [020](adr/020-tail-echo-hallucination-guard.md) | 尾部回音幻觉守卫（**含补遗二**：5b 加静音窗闸、信号 6 已回退） | 接受 |
| [021](adr/021-fill-gaps-recovered-hallucination-guard.md) | 补洞恢复段守卫 | 接受 |
| [031](adr/031-recovered-segment-hardening.md) | 恢复段加固 | 接受 |
| [036](adr/036-fill-gaps-prefix-collapse-recovery.md) | 补洞前缀坍缩恢复（信号 C 长度分级） | 接受 |

### 转写与翻译
| ADR | 主题 | 状态 |
|---|---|---|
| [002](adr/002-chunked-resume.md) | 分块断点续跑 | Accepted |
| [003](adr/003-http-proxy-only.md) | 仅 HTTP 代理（禁 SOCKS） | Accepted |
| [005](adr/005-agent-as-engine.md) | Agent 即引擎 | accepted |
| [006](adr/006-lang-autodetect.md) | 语言自动检测 | accepted |
| [007](adr/007-proxy-autodetect.md) | 代理自动检测 | accepted |
| [010](adr/010-glossary.md) | 术语表 | 接受 |
| [027](adr/027-translation-style-tracks.md) | 翻译风格轨（T3） | Accepted |

### 控制平面与流程
| ADR | 主题 | 状态 |
|---|---|---|
| [004](adr/004-segment-merge-strategy.md) | 断句合并策略 | accepted |
| [030](adr/030-control-plane.md) | 控制平面（状态机 / 闸门 / 退出码） | Accepted（已全量落地） |
| [032](adr/032-translation-workflow.md) | 翻译工作流与决策点协议（P0→P1） | 接受 |
| [033](adr/033-control-plane-pipeline-entry.md) | `pipeline` 单一入口幂等推进器（T8） | Accepted（已落地） |
| [035](adr/035-pipeline-data-contract.md) | 阶段间数据契约总线 | Accepted |

> ADR 编号 **缺 018 / 019**：未分配（非删除）。

---

## Spec 索引（按主题）

| 组 | Spec |
|---|---|
| 总览 | [00](specs/00-overview.md)（含文档清单）· [01](specs/01-segment-schema.md) |
| 三阶段 | [02 transcribe](specs/02-transcribe.md) · [03 translate](specs/03-translate.md) · [04 generate](specs/04-generate-srt.md) |
| 断句层 | [08 merge](specs/08-segment-merge.md) · [12 词级时间戳](specs/12-word-level-timestamps.md) · [13 cue 拆分](specs/13-cue-splitting.md) |
| 翻译 | [09 Agent 翻译](specs/09-agent-translate.md) · [10 backfill](specs/10-backfill.md) · [14 术语表](specs/14-glossary.md) |
| 配置与 CLI | [06 config](specs/06-config.md) · [11 CLI v2](specs/11-cli-v2.md) · [07 gotchas](specs/07-gotchas.md) |
| 时序与补洞 | [15 静音时序](specs/15-timing-silence.md) · [16 fill-gaps](specs/16-fill-gaps.md) |
| 校验 | [17 verify-align](specs/17-verify-align.md) · [18 verify 三 Lane](specs/18-verify.md) |
| 能力（T 系列） | [19 人声分离](specs/19-vocal-separation.md) · [21 风格轨](specs/21-translation-styles.md) · [22 WhisperX 对齐](specs/22-whisperx-alignment.md) |
| 环境（E 系列） | [20 env-readiness](specs/20-env-readiness.md) · [23 命令入口](specs/23-environment-location.md) |
| 入口（T8） | [24 pipeline 行为](specs/24-pipeline-behavior.md) |

> Spec 编号 **缺 05**：已删除（说明见 `11-cli-v2.md`）。
> Spec 00–21 为行为契约、随代码演进，不设 Accepted/Superseded 状态；22–24 带状态字段。

---

## 归档区（`archive/`）

以下内容已**退出日常维护**，仅作历史追溯，**不要照着操作**：

| 文件 | 归档原因 |
|---|---|
| [`archive/V3-STATUS.md`](archive/V3-STATUS.md) | 自述冻结于 V3（2026-07），当前已至 V14；含指向 README 旧章节的失效链接 |
| [`archive/CONTROL-PLANE-PLAN.md`](archive/CONTROL-PLANE-PLAN.md) | 控制平面改造方案，已全量落地（2026-09-01）；决策结论见 [ADR-030](adr/030-control-plane.md) |
| [`archive/design/`](archive/design/) | 整目录孤儿（三大入口零引用），内容停在 V3；有效部分已并入 `specs/` |
| [`archive/issues/001`](archive/issues/001-subtitle-timing-gaps-and-early-cues.md) | 字幕时序 issue，已修复闭环；结论见 [Spec 15](specs/15-timing-silence.md) |

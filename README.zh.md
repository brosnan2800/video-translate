# video-translate（中文版）

把视频转成**可导入剪映的中英双语字幕**，走一条忠实、可断点续跑的流水线：

```
视频 ──转写──▶ segments_en.json ──翻译──▶ zh_segments.json ──生成──▶ *.srt / *.txt
        (faster-whisper                 (Google 翻译                (字节级稳定
         large-v3, CPU/int8)            走 HTTP 代理)               SRT/TXT)
```

设计不变量：**时间戳是声学事实**，由转写阶段产生，下游绝不重算——翻译只改写文本，字幕始终与音频对齐。

---

## 面向 AI Agent / 自动化

如果你是一个被指派用本项目生成字幕的 Agent（WorkBuddy / Claude Code / Cursor / Cline / …），请**先读 [`AGENTS.md`](AGENTS.md)**，并按其中的执行协议操作（Preflight 探环境 → run 转写 → 校验）。不要即兴改写流水线——该指南里固化了续跑、代理、golden 校验等规则。

## 面向 WorkBuddy 用户（skill）

一个轻量 WorkBuddy skill 可以包住这个 CLI。安装方式：把 skill 文件夹复制到 `~/.workbuddy/skills/video-translate/`（用户级）或 `{workspace}/.workbuddy/skills/`（项目级），然后在对话里调用。skill 把所有真实工作委托给本文档描述的 `video-translate` CLI。

---

## 快速开始（人工）

```bash
# 1. 安装（创建 .venv，安装依赖 + 本包可编辑模式）
make install-dev

# 2. 自检环境（ffmpeg、模型缓存、代理、依赖、引擎）
make doctor

# 3. 首次下载 large-v3 模型（约 3GB，跨项目复用）
.venv/bin/video-translate setup            # 若 ~/.cache/huggingface 已有则复用

# 4. 全流程——零配置（base/outdir 默认取自视频路径）
.venv/bin/video-translate run "videos/apollo.mp4"             # agent 引擎（默认）
.venv/bin/video-translate run "videos/apollo.mp4" --engine google   # 无头端到端

# 4b. （仅 agent 引擎）Google 漏翻的行会落到 <base>.agent_pending.json
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json                      # 生成 backfill_task.json，退出码 6
# ...... Agent 填好 backfill_task.json 后，合并并重生成：
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json --agent-zh videos/apollo.backfill_zh.json \
    --segments videos/apollo.segments_en.json --outdir videos --base apollo

# 5. 将 <视频目录>/apollo.bilingual.srt 导入剪映
```

V2 默认值：`INPUT` 是位置参数；`--base` = 视频文件名主干；`--outdir` = 视频所在目录；`--lang` 自动检测；`--proxy` 自动检测（`--no-proxy` 走直连）。默认的 **agent 引擎**在转写 + merge 完成后停下，对外吐一份翻译任务文件给调用方 Agent（**退出码 6**）；想全自动（质量较低）跑则用 `--engine google`。各阶段也可用 `transcribe` / `translate` / `generate` 单独执行，或用 `run --skip transcribe` 续跑上次中断的部分。

> **退出码 6 = 轮到 Agent 了。** 使用 `--engine agent` 时，`run`/`translate` 完成转写后会写出 `*.translate_task.json`（或 `backfill_task.json`），然后以退出码 6 结束。调用方 Agent 读取该文件，按 `persona` 把每条 `to_translate` 翻译成中文，写成 `*.zh_segments.json`，再运行 `generate`。本项目**不内置任何 LLM 客户端**——见 [ADR-005](docs/adr/005-agent-as-engine.md)。

## 环境要求

- **Python 3.13**（`.python-version` 已锁定）。
- **ffmpeg + ffprobe** 在 `PATH` 中。
- 模型下载与 Google 翻译需要 **HTTP 代理**（默认 `http://127.0.0.1:7890`，如 Clash）。**不支持 SOCKS**——它会破坏 huggingface_hub（见 [ADR-003](docs/adr/003-http-proxy-only.md)）。
- large-v3 模型约需 3GB 磁盘空间（共享 HF 缓存于 `~/.cache/huggingface`）。

## 配置

优先级：**CLI 参数 > 环境变量 > `.video-translate.toml` > 默认值**。详见 [Spec 06](docs/specs/06-config.md)。`.video-translate.toml` 示例：

```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "auto"          # 自动检测（默认）

[translate]
src = "en"
tgt = "zh-CN"

[llm]
persona = "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感……"

[hf]
cache_dir = "~/.cache/huggingface"   # 共享模型缓存

[merge]
merge_enabled   = true
merge_max_dur   = 8.0
merge_max_gap   = 0.5
```

支持的 TOML 段落：`transcribe`、`translate`、`llm`、`hf`、`merge`（`[hf] cache_dir` 映射到 `hf_cache_dir`）。环境变量覆盖：

| 环境变量                  | 映射到               |
|---------------------------|----------------------|
| `VT_MODEL`                | model                |
| `VT_CHUNK`                | chunk                |
| `VT_LANG`                 | lang（用 `auto` 表示自动检测） |
| `VT_PROXY`                | proxy                |
| `VT_SRC` / `VT_TGT`       | src / tgt            |
| `VT_ENGINE`               | engine（`agent`/`google`） |
| `VT_PERSONA`              | persona              |
| `VT_MERGE_MAX_DUR`        | merge_max_dur        |
| `VT_MERGE_MAX_GAP`        | merge_max_gap        |
| `VT_MERGE_MAX_CHARS`      | merge_max_chars（保留） |
| `HF_HOME`                 | hf_cache_dir         |
| `HTTPS_PROXY`/`HTTP_PROXY` | proxy（在 `VT_PROXY` 未设时兜底） |

## 产物

| 文件                       | 用途                                 |
|----------------------------|--------------------------------------|
| `<base>.bilingual.srt`     | 中文在上 / 英文在下，导入剪映         |
| `<base>.zh.srt`            | 纯中文字幕                            |
| `<base>.en.srt`            | 纯英文字幕                            |
| `<base>.txt`              | 双语校对稿                            |

## 退出码

| 码   | 含义                                                     |
|------|----------------------------------------------------------|
| 0    | 成功                                                     |
| 1    | 运行时错误                                               |
| 2    | 参数错误（argparse）                                     |
| 3    | 缺少依赖（ffmpeg / HF 模型）                              |
| 4    | 代理错误（如传入 SOCKS 代理）                             |
| 5    | 转写被杀死（SIGKILL）；可安全重跑                         |
| 6    | **等待 Agent** —— 转写 + 任务已就绪，需 Agent 接手翻译    |

退出码 6 是 **agent-as-engine** 设计的核心：CLI 负责吃 CPU 的转写，然后把手里的翻译任务交给调用方 Agent 并停下。非 Agent（无头）模式用 `--engine google`，不会返回 6。

## 命令一览

| 命令        | 作用                                                   |
|-------------|--------------------------------------------------------|
| `run`       | 转写 → 翻译 → 生成（完整流水线）                        |
| `transcribe`| 视频 → `segments_en.json`（分块、可续跑、含 merge）      |
| `translate` | `segments_en.json` → `zh_segments.json`（agent 任务 / google） |
| `generate`  | `segments_en.json` + `zh_segments.json` → 4 个字幕文件  |
| `backfill`  | 补全 `agent_pending.json` 并合并重生成                   |
| `setup`     | 检查/下载 HF 模型（已有则复用）                          |
| `doctor`    | 环境自检                                                |

## 开发（TDD + SDD）

- **先写规格**：[`docs/specs/`](docs/specs)（00–11）在写代码前定义行为。
- **决策记录**：[`docs/adr/`](docs/adr) 记录「为什么」（CPU/int8、分块续跑、仅 HTTP 代理、segment-merge、agent-as-engine、语种自动检测、代理自动检测）。
- **测试**：`make test`（快速单测 + 契约 + golden，跳过 `@slow`）；`make test-all`（含基于源视频的真实 e2e）。golden 分层：`test_generate_golden`（build_outputs 字节级一致）、`test_merge_golden`（merge_segments 确定性）、`test_v1_golden_preserved`（V1 归档为 `.v1`）。

```bash
make test        # 约 105 个快测
make test-all    # + 慢速 e2e（需模型 + 视频）
make clean
```

## 设计说明

- **Agent 即引擎**（V2，[ADR-005](docs/adr/005-agent-as-engine.md)）— 默认的 `--engine agent` 对外吐一份翻译任务给调用方 Agent（它自带 LLM），CLI 不依赖任何 LLM 客户端。Google 是 `--engine google` 无头兜底。
- **片段合并**（V2，[ADR-004](docs/adr/004-segment-merge-strategy.md)）— 相邻的 Whisper 碎 cue 重新拼成可读字幕块；时间戳原样取用（首段 start / 末段 end），绝不重算。默认开启（`--no-merge` 可跳过）。
- **Backfill**（V2）— 当 `--engine google` 留下未翻译的行时，会写入 `<base>.agent_pending.json`。`backfill` 先生成一份聚焦任务（`backfill_task.json`，退出码 6）交给 Agent，随后把 Agent 填好的 `*.backfill_zh.json` 合并回去并重生成字幕文件。
- **可续跑转写** — 音频按 `chunk` 切分；每个 `chunk_N.json` 原子落盘，重跑时跳过（[ADR-002](docs/adr/002-chunked-resume.md)）。
- **CPU / int8** — CTranslate2 无 AMD/Metal 支持，强制使用（[ADR-001](docs/adr/001-cpu-int8.md)）。
- **代理自动检测**（V2，[ADR-007](docs/adr/007-proxy-autodetect.md)）— `--no-proxy` / `--proxy` / 环境变量 / 探测 7890 → 直连。SOCKS 仍不支持（[ADR-003](docs/adr/003-http-proxy-only.md)）。

## 许可证

私有项目，归仓库所有者所有。

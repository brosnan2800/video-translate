# 翻译工作流设计文档（Translation Workflow — ADR-032）

> 适用范围：视频翻译流水线从「用户一句话」到「成片交付」的**完整工作流**，重点是
> P0（环境体检 + 音频画像）与 P1（转写）之间的**引导选择式决策点**，以及「绝不跳步、
> 绝不裸跑无画像」的代码硬保证。
>
> 配套代码：`../../src/video_translate/audio_profile.py`、`state.py`、`toolchain.py`、`cli.py`；
> 单一入口推进器见 [`033-control-plane-pipeline-entry.md`](033-control-plane-pipeline-entry.md)
> 与 [`../specs/24-pipeline-behavior.md`](../specs/24-pipeline-behavior.md)；Agent 协议见
> [`../../AGENTS.md`](../../AGENTS.md) §4.5；状态机定义见
> [`../../src/video_translate/pipeline_def.py`](../../src/video_translate/pipeline_def.py)
> （纯数据声明式流程表）与 ADR-030。
>
> **数据契约（ADR-035）**：阶段间产物的命名 / 字段 / 生产者 / 消费者 / 可否重算 /
> 必须穿透字段，以 [`../../src/video_translate/artifacts.py`](../../src/video_translate/artifacts.py)
> 声明式契约表为**唯一事实来源**；本文出现的文件名与契约表不一致时，以契约为准。
> 要点：`segments_raw.json` 全字段不可变（置信度跟段绑死）；`segments_en.json` 是
> 带 `_raw_indices` 回查指针的合并视图（不聚合不覆盖）；声学事实
> （duration / silence_intervals）只在 preflight 算一次落 `vt_state.json`，下游只读。

---

## 1. 需求

### 1.1 用户视角（一句话翻译）

用户只说一句「翻译 XXX 视频」，全程**不敲任何 CLI 命令**。Agent 自动编排：

1. P0：环境体检（`doctor`）+ 音频画像（`analyze_audio`）；
2. **决策点**：把「这个视频建议怎么处理」用引导选择式摆给用户——**只剩翻译风格一项真选择**
   （VAD/人声分离已按 ADR-034 退为显式 flag 覆盖、默认裸跑），列出风格三项 + 推荐默认值；
3. 用户回复 → 按其选择执行；**默认等待 5 分钟，超时未回复则按画像推荐自动执行**；
4. 随后全自动完成：转写 → 翻译 → 生成 → 校验 → 交付。

### 1.2 历史痛点（必须根治）

此前 P0→P1 的交接**只存在于散文**（`doctor` 打印建议，Agent 需自行"读 prose 重新推导"），
导致：

- 画像被跳过，Agent 直接 `run` → 裸跑无画像参数（用户痛点「P0 跳过了」）；
- 决策逻辑在 `cli.doctor` 里**散落且只能打印**，run 与决策点各算一遍，双份逻辑漂移；
- 工具链（ffmpeg/demucs/nvidia-smi/模型缓存）在调用点现场 `shutil.which` / 读 `os.environ`，
  随 CWD 漂移，造成「doctor 里 OK、下一阶段 MISS」；
- 决策无落盘、不可审计，无法追溯「这次是怎么决策的」。

### 1.3 目标

- **不跳步**：P0 画像 → 决策点 → P1 转写 → … → 交付，每一步都发生；该决策的决策、该自动的自动。
- **可配置**：决策点超时、VAD 三参数、风格、人声分离都进入配置体系，用户自己改。
- **可审计**：最终三决策以 `origin` 分级（explicit=用户拍板 / profile=画像推荐）落盘 `vt_state.json`。
- **兜底硬控**：即使 Agent 失守直接 `run`，代码也自动画像 + 自动路由，绝不裸跑无画像。

---

## 2. 设计

### 2.1 三层保证

| 环节 | 保证方式 | 硬度 |
|---|---|---|
| 画像（P0 产出） | `cmd_run` / `pipeline` preflight 强制：转写前查画像快照，无则自动补画像并落盘 | **代码硬控** |
| 决策点（翻译风格一项 + 5 分钟超时） | `pipeline` **默认挂起在决策点**（停点：style only）+ Agent 按 §4.5 问人；超时值可配置；`--prompt never` 跳过 / `--require-profile` 强制 | **代码硬控挂起 + Agent 协议** |
| 其余步骤（转写→翻译→生成→校验） | `pipeline` 引擎驱动 + `status` / `[NEXT]` 块 / exit 6·8 追踪 | **代码硬控** |

### 2.2 关键决策

- **推荐逻辑纯函数化**：`audio_profile.profile_recommendation(prof)` 整合 `recommend_vad` + 静音密度
  / 强 BGM 判断，产出统一三决策推荐结构，doctor 与 run 共用，避免双份逻辑漂移。
  **ADR-034（S2，一期）**：该推荐已降级为**参考信息，不再驱动路由**——`profile_recommendation`
  恒返回 `vad=False` / `adaptive_vad=False` / `separate_vocals=False`（画像只算静音几何供
  doctor 打印 + G3 预筛），默认全裸跑，掩码真音交转写后 review + G1/G2/G3 救回。
- **风格无画像依据，默认 film**：风格由用户偏好决定，画像只决定 VAD 与人声分离；`profile_recommendation`
  的 `style` 恒返回 config 默认，决策点允许用户覆盖。
- **vad_threshold 不再由推荐函数给出**：ADR-034 起 `recommend_vad` 恒返回 `bare`，不再下发
  `0.1` 覆盖值；用户需显式 `--vad --vad-threshold 0.1` 才会传（默认 `transcribe.VAD_THRESHOLD = 0.35`）。
- **决策落盘双 key**：`decisions.audio_profile`（画像快照 + 推荐）、`decisions.routing`（三决策最终值
  + origin）。复用 `record_decision()` + `save()`，向后兼容。
- **run 兜底放 cmd_run 入口而非 pipeline gate**：ADR-030 规定 stage gate 只查产物文件，画像快照属增强
  （state），故 run 入口直接查 state 并自动补画像。
- **工具链收口到中央登记**：`toolchain.resolve_tool()` / `tool_available()` / `model_dir()` 消除现场
  `shutil.which` / `os.environ` 的 CWD 漂移。

### 2.3 数据流

```
analyze_audio(video)
   └─> profile_recommendation(prof)           # 纯函数，doctor 与决策点共用
         └─> record_audio_profile()           # 落盘 decisions.audio_profile
               └─> 决策点 (Agent 引导选择式 + 读 VT_DECISION_TIMEOUT_SECONDS 超时)
                     └─> record_routing()      # 落盘 decisions.routing (value + origin)
                           └─> cmd_run 读 routing 合并 (CLI flag > routing > config 默认)
                                 └─> cmd_transcribe
                                       └─> _record_run_decisions() 落盘最终路由
```

### 2.4 合并优先级（cmd_run 内 `_resolve_routing`）

```
最终三决策 = CLI 显式 flag  >  已落盘 routing  >  画像推荐兜底
```

- `store_true` 的 flag（vad / adaptive_vad / separate_vocals）只有显式传了才为 `True`，否则可被
  routing / 推荐覆盖；
- `default=None` 的字段（vad_threshold / style）非 `None` 即视为显式。

---

## 3. 配置

### 3.1 变量全表

| 变量 | 默认 | 含义 | 等价 CLI flag |
|---|---|---|---|
| `VT_DECISION_TIMEOUT_SECONDS` | `300` | 决策点等待超时（秒，=5 分钟） | —（Agent 侧等待） |
| `VT_PROMPT` | `always` | 决策点模式（`always` 问人+超时自动 / `never` 直接自动路由 / `require-profile` 硬闸） | `--prompt` |
| `VT_STYLE` | `film` | 翻译风格 | `--style` |
| `VT_VAD` | `false` | 全局 VAD（锚定静音） | `--vad` |
| `VT_ADAPTIVE_VAD` | `false` | 分块自适应 VAD（混合音频） | `--adaptive-vad` |
| `VT_VAD_THRESHOLD` | `0.35` | VAD 概率阈值 | `--vad-threshold` |
| `VT_SEPARATE_VOCALS` | `false` | Demucs 人声分离 | `--separate-vocals` |
| `VT_MERGE_ENABLED` | `true` | 断句/切分合并 | `--no-merge` 取反 |

### 3.2 配置位置（用户自行修改）

优先级：**CLI flag > `.video-translate.toml` > `.env` / `.env.local` > 代码默认**。

- **环境变量**：写在仓库根 `.env` 或 `.env.local`（模板见 `.env.example`）。
- **TOML**：写在仓库根 `.video-translate.toml`，例如：

  ```toml
  [pipeline]
  decision_timeout_seconds = 300      # 决策点超时（秒）
  prompt = "always"                   # always / never / require-profile

  [translate]
  style = "film"                      # 默认翻译风格

  [transcribe]
  vad = false
  adaptive_vad = false
  vad_threshold = 0.35
  separate_vocals = false
  ```

> 改完无需代码改动；`doctor` / `run` / `pipeline` 会自动读取。（TOML 扁平键按
> `transcribe / translate / hf / llm / merge / pipeline` 六段落到 Config，见
> `config._TOML_SECTIONS`；`[app]` 不是合法段。）

### 3.3 决策点超时（重点）

**「5 分钟」在哪里配？** 一个地方：`VT_DECISION_TIMEOUT_SECONDS`（默认 `300`），写进 `.env` 或
`.video-translate.toml` 的 `decision_timeout_seconds`。

注意：这个超时是 **Agent 在聊天里等待用户回复** 的时间，CLI 本身不 sleep（CLI 无法感知聊天）。
Agent 读配置得到这个值，超时后按画像推荐自动执行。把它设小（如 `60`）即「1 分钟」，设大即更长。

---

## 4. 落盘结构（`vt_state.json`）

```jsonc
{
  "decisions": {
    "audio_profile": {           // 由 record_audio_profile 写入（doctor / run 兜底）
      "value": {
        "style": "film",
        "vad": false,            // ADR-034（S2）：画像推荐恒为 false，只作参考
        "adaptive_vad": false,
        "separate_vocals": false,
        "vad_threshold": null,
        "rationale": "clean level ...; silence fraction 0.18 (advisory only)"
      },
      "origin": "profile"
    },
    "routing": {                 // 由 record_routing 写入（决策点 / run 合并）
      "value": {
        "style": "film",
        "vad": false,            // 默认裸跑；显式 --vad 才为 true
        "adaptive_vad": false,
        "separate_vocals": false,
        "vad_threshold": null
      },
      "origin": "explicit"       // explicit=用户拍板 / profile=画像推荐
    }
  }
}
```

- `decisions.audio_profile`：画像快照 + 推荐，是 P0→P1 交接的可审计依据。
- `decisions.routing`：最终三决策 + origin，run 用它做自动路由。

---

## 5. 决策点协议（Agent 侧，详见 AGENTS.md §4.5）

`pipeline` 跑到 preflight（画像落盘）后**默认挂起在决策点**（`[NEXT] stage=preflight
(STOP POINT — decision point: style only)`），进程退出（exit 6）等 Agent 介入。Agent 接手后：

1. 读 `[NEXT]` 块 / `status --json` 确认停在决策点，取画像推荐（`decisions.audio_profile` 已落盘）的 `rationale` 作参考。
2. 在聊天里引导选择式提问，**只问翻译风格一项**（film / literal / bilingual_study，默认 film），
   并说明等待 `VT_DECISION_TIMEOUT_SECONDS`（默认 300s）。VAD/人声分离已按 ADR-034 退为显式 flag，
   默认裸跑，确有需要才传给 `pipeline`。
3. 用户回复 → 带显式 flag 重跑 `pipeline --style <picked>`（落盘 `origin=explicit`）；
   超时未回复 → 不带 flag 重跑 `pipeline`，按画像推荐自动路由（`origin=profile`）。
4. 重跑 `uv run video-translate pipeline "<video>"` → 见 routing 已存在，续跑 transcribe 至下一停点（翻译）。

**`--prompt` 三档**（默认 `always`）：`always` 问人 + 超时自动 / `never` 全自动 /
`require-profile` 硬闸（必须 explicit routing，缺失 exit 8）。

---

## 6. 工具链收口（附带修复）

所有外部工具 / 依赖目录统一经 `toolchain.py` 中央登记，调用点不再现场解析：

- `_TOOL_REGISTRY`：登记 `ffmpeg` / `ffprobe` / `demucs` / `nvidia-smi`，`init_toolchain` 一次性写入
  `ToolchainStatus`（demucs_path / nvidia_smi_path）。
- `_DEP_REGISTRY` + `model_dir(name)`：登记 `hf_cache` / `nltk_data` / `demucs_models` 依赖目录，
  调用点用 `model_dir("hf_cache")` 而非读 `HF_HOME`。
- 收口点：`ffmpeg_utils._resolve_binary`（委托 `resolve_tool`）、`cli._has`（→ `tool_available`）、
  `vocal_sep`（demucs/ffmpeg 走 `resolve_tool`，修复原 `VT_FFMPEG` 错 key）、`transcribe`（nvidia-smi
  → `tool_available`）、`cli` 的 `HF_HOME` / `VT_FFMPEG_DIR` 读取。

---

## 7. 验证

- `tests/test_config_routing.py`：配置解析（env/toml/cli 优先级、类型转换、无效值回退）。
- `tests/test_profile_recommendation.py`：推荐纯函数（干净/低电平/强 BGM/混合四类几何、往返序列化）。
- `tests/test_run_profile_gate.py`：run 兜底（无快照自动补画像、有快照复用、routing origin 分级、
  显式 flag 覆盖、`--require-profile` 硬闸）。
- `tests/test_pipeline_advance.py`：`pipeline` 幂等推进器契约（`next_action` 六动作决策表、
  `--prompt` 三档校验、分派到 run/generate/verify、决策点挂起与显式 flag 解锁、require-profile 硬闸）。
- 全量回归基线：`uv run pytest`。

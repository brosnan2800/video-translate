# 架构与代码规范（Architecture & Code）

## 分层与依赖方向（禁止反向 import）

```text
io_utils / config / toolchain / ffmpeg_utils      基础设施（无业务语义）
  → audio_profile / transcribe / merge / fill_gaps / vocal_sep / align   声学层
    → translate / generate / verify / verify_align / glossary             内容与表现层
      → state / pipeline / pipeline_def / capabilities                    控制面
        → cli                                                             唯一入口（薄壳）
```

- 上层可 import 下层，**下层永远禁止 import 上层**（如 verify 禁止 import cli）。
- 新模块先问「属于哪一层」；跨两层使用的能力下沉到被依赖最多的那层。

## 纯函数纪律

- 模块 docstring 声明 `Pure (no I/O)` 的（如 `verify.py`）**禁止** subprocess/文件访问。
- subprocess 只允许在执行层：`analyze_audio` / `probe_volume_window` / `transcribe` 等。
- **解析与构造必须纯函数**：`parse_*`（合成 stderr 即可单测）、`build_*_cmd`
  （命令拼接可单测）——参照 `audio_profile.parse_volumedetect` / `ffmpeg_utils`。
- 二进制解析一律走 `ffmpeg_utils._resolve_binary`（toolchain 单一来源），
  **禁止散落 `shutil.which`**。

## 控制面原则（ADR-030，违反 = 架构破坏）

- **闸门只依赖产物文件**（segments/zh），永不依赖 `vt_state.json`；
  state 只是增强，删除不得破坏闸门。
- 显式 flag + 能力缺失 = **exit 8 硬停 + 修复指引**（capabilities 意图闸），
  禁止静默降级；逃生门（`--allow-degrade` / `--no-strict`）必须显式传参留痕。
- 显式决策落盘 `decisions`（origin=explicit/default）；best-effort，state 故障不阻断流水线。

## 守卫与可见性

- **任何自动拦截/丢弃必须打印**（带证据字段：no_speech_prob / avg_logprob / overlap），
  禁止静默 continue（ADR-031 教训）。
- 校验类失败 = 红灯，**不许 skip 或吞异常**；「没查到」永远不能冒充「查过，没问题」。
- 幻觉/质量守卫放对应层：转写层 `drop_hallucination_segments`、恢复段
  `_is_recovered_hallucination`、校验层 `verify.find_*`；**所有产出路径共用一道守卫**
  （resegment 事故教训）。

## 异常与时间戳红线

- `except Exception` 必须带 `# noqa: BLE001 - <为什么可吞>` 注释；无注释的裸吞 = bug。
- **声学时间戳不可重算**（ADR-012）：下游只改文本；唯一例外是删除整段这类
  「合法修订」，且必须刷新 `segments_sha` 锚点。

## 其他硬规矩

- 打印：`[module] message` 前缀；错误走 stderr；进度 `flush=True`；
  agent 停点必须带 `[NEXT]` 块。
- 命名：模块单数名词、函数动词开头、常量 UPPER_SNAKE、issue 字符串用小写连字符
  （`"low-confidence"`）；测试文件 `test_<模块/特性>.py`。
- **数据格式向后兼容**：旧产物缺新字段必须优雅跳过
  （参照 `find_low_confidence_segments` 对缺失置信度字段的处理）。
- Windows 兼容：路径用 `os.path`/`pathlib`；一切命令 `uv run` 前缀；
  禁止引入 Makefile / shell-only 语法（红线 R7）。
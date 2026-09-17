# Spec 25: CLI 路径与文件名卫生校验（参数边界）

- 状态: 批准（实现）
- 日期: 2026-09-04
- 关联: ADR-035（数据契约总线 D4 边界校验）、[ADR-043](../adr/043-input-form-auto-routing.md)（输入形态自动判定 / URL 豁免）、Spec 11（CLI v2）、Spec 23（环境定位）、Spec 24（pipeline 入口）
- 修复对象: 「中文文件名在 Windows PowerShell 5.1 下变成不可诊断的深栈崩溃」——
  事故数据见 §根因回顾，现象是 `io_utils.save_json` 抛
  `OSError: [WinError 123] 文件名、目录名或卷标语法不正确`

## 目标

把「参数在进入业务逻辑前就已损坏」变成**在入口当场报错、带修复指引**的
确定性行为，而不是让损坏的参数一路流到 `os.replace()` 才以一个与病因无关的
`WinError 123` 崩溃。

本 Spec **不修复终端编码本身**（那是宿主环境的事），只保证损坏一旦发生就
被看见、被说清、被指到可操作的修法上。

## 根因回顾（为何需要本 Spec）

事故：`videos/角斗士采访.mp4` 经 Windows PowerShell 5.1 传给 CLI。

1. 宿主把命令写成 **UTF-8 无 BOM** 的 `.ps1` 临时脚本。
2. PowerShell 5.1 读取无 BOM 脚本时按 **ANSI 代码页 ACP=936 (GBK)** 解码 →
   `角斗士采访.mp4` 变成 `瑙掓枟澹\ue0a6噰璁?mp4`。
   - `角` = `E8 A7 92`，尾字节 `0x92` 被解成**右单引号** `’`——所以损坏后的
     字符串还可能**破坏 PowerShell 自身的引号配对**，命令连解析都过不去。
   - `.` 被解成 `?`，于是 `Path.stem` 找不到扩展名分隔符，把 `…?mp4`
     整个当成了 stem。
3. `_default_base()` 返回含 `U+E0A6`（BMP 私用区）与 `?`（Windows 文件名
   非法字符）的 base。
4. 该 base 一路传到 `vt_state.save()` → `io_utils.save_json()` →
   `os.replace()`，抛出 `WinError 123`。堆栈指向 `save_json`，**与病因无关**，
   用户无从判断这是编码问题。

实测确证（2026-09-04）：

```
file_bytes   = 232,167,146,230,150,151,...   # UTF-8 "角斗士采访"，无 BOM
read_as_ANSI = 瑙掓枟澹圭爢璁                # PS 5.1 按 GBK 解码 → 损坏
read_as_UTF8 = 角斗士采访                    # 显式指定编码才正确
ps1_bom      = 99,104,99 ("chc")             # 命令脚本本身也无 BOM
ACP=936 / PSVer=5.1 / pwsh7=未安装
```

## 行为契约

### 1. 校验时机：入口，而不是调用点

`main()` 在 `parser.parse_args()` 之后、`args.func(args)` 之前做一次卫生校验
（ADR-035 D4：阶段边界断言，缺字段/坏字段当场报错）。**一处覆盖所有子命令**，
各 `cmd_*` 不重复实现。

失败返回 `EXIT_ARGS`（2），错误前缀 `[args]`，写 stderr。

### 2. 校验对象

对每个子命令，按参数名取值（缺失则跳过）：

| 参数 | 语义 | 校验层 |
|---|---|---|
| `input`（positional，视频/音频路径） | 输入 | 编码损坏 + 平台非法 |
| `--base` / 由 `input` 派生的 base | 产物文件名 | 编码损坏 + 平台非法 |
| `--segments` / `--zh` / `--video` / `--pending` | 输入 | 编码损坏 + 平台非法 |
| `--outdir` / `--out` | 输出 | 不做存在性断言，仅编码损坏 |

base 派生沿用 `_default_base()`（Spec 11 既有语义，不改）；校验针对**派生结果**。

### 3. 两层检测（关键：跨平台语义不同）

**层 1 — 编码损坏（跨平台一致，与操作系统无关）**

命中以下任一码位即判「文件名编码损坏」：

| 码位区间 | 含义 |
|---|---|
| `U+0000–U+001F` | C0 控制字符 |
| `U+007F–U+009F` | DEL + C1 控制字符 |
| `U+D800–U+DFFF` | 代理区（未配对代理 / UTF-16 误解码） |
| `U+E000–U+F8FF` | BMP 私用区（**GBK 误解 UTF-8 的典型产物**） |
| `U+FFFD` | 替换字符 |
| `U+F0000–U+10FFFD` | 补充私用区 |

理由：这些码位在**任何**平台的人类可读文件名里都不该出现，是编码链路损坏的
确定性指纹，与平台文件名规则无关。

**层 2 — 平台非法文件名字符（按 `os.name` 取字符集）**

| 平台 | 非法集 |
|---|---|
| Windows (`os.name == "nt"`) | `< > : " \| ? *` + 上述控制字符 |
| POSIX (macOS / Linux) | `NUL`、`/`（出现在 basename 中） |

理由：`:` 在 macOS 文件名中合法、`?` 在 POSIX 合法——**不得用 Windows 规则
误伤 Mac**。这是本 Spec 对「不影响 Mac」的硬约束。

### 4. 不做什么（避免误伤）

- **不**把「文件不存在」当作编码问题：不存在交给原有逻辑报
  `FileNotFoundError`，保持既有行为。
- **不**拦截合法非 ASCII 文件名：macOS/Windows 上的正常中文名（`角斗士采访`）
  不含层 1 任何码位，必须放行。
- **不**修改 `_default_base` / `_default_outdir` 的既有语义。
- **不**触碰任何转写 / 翻译 / 生成业务逻辑。
- **不**把 **URL** 当文件名校验（ADR-043 D8）：URL 的 `?` / `:` / `/` 是**语法的一部分**，
  不是文件名污染。`_path_hygiene_error` 对**带 scheme 的 URL 值**（复用
  `ytcaptions.looks_like_url`，ADR-043 D2）跳过层 2 的平台非法字符检查。
  - **事故来源（2026-09-17）**：`pipeline "https://www.youtube.com/watch?v=<id>"` 的
    basename 是 `watch?v=<id>`，`?` 命中 Windows 非法集 → 入口直接 `exit 2`，
    于是 `pipeline <url>` 的输入形态自动判定（[Spec 24](24-pipeline-behavior.md) §6）
    **根本无从执行** —— 判定写得再对也到不了。
  - **豁免只针对 URL 形态**：真实文件名仍受全量保护。`?` 在 Windows 文件名里确实非法、
    且是编码事故的指纹之一（§根因回顾第 2 条），**不得放宽整类**。

### 5. 报错格式（可观测性：可操作，而非仅可诊断）

```
[args] 输入文件名编码损坏，无法继续：videos/瑙掓枟澹\ue0a6噰璁?mp4
  坏字符: U+E0A6 (私用区), '?' (平台非法)
  Fix: 命令行参数在到达 Python 前已被重新编码。
       本机 Windows PowerShell 5.1（ACP=936）会把 UTF-8 中文参数按 GBK 解码。
       ① 视频改用 ASCII 文件名；② 换 PowerShell 7（无 BOM 脚本按 UTF-8 读）；
       ③ 或显式传 --base <ascii-name>。
```

## 范围与边界

- **IN**：`main()` 入口卫生校验（§1）、两层检测规则（§3）、报错格式（§5）。
- **OUT**：不修改宿主终端 / 系统代码页（属用户环境决策，不在项目内自动改）；
  不修改 `.codebuddy/rules/`；不改已产出的任何视频产物；不改 `pyproject` /
  `uv.lock`（纯标准库实现，无新依赖）。

## 跨平台影响（macOS 复核）

- 层 1 检测与平台无关，Mac 上同样生效：编码损坏在哪都是损坏。
- 层 2 在 Mac 上只禁 `NUL` 与 `/`，**不会**误伤 `:`、`?`、`*` 等 macOS/HFS+
  与 APFS 的合法文件名字符。
- Mac 默认终端（bash/zsh，UTF-8）本就不会产生本事故的 mojibake，校验恒为
  no-op——**零行为变化**。
- 实现仅用标准库（`os` / `unicodedata` / `pathlib`），无平台专属依赖，不违反
  R6 依赖准入清单。

## TDD 验收清单

- [x] `find_broken_encoding_chars()`：命中 §3 层 1 各区间；正常中文名（`角斗士采访`）
      与 ASCII 名返回空。
- [x] `find_illegal_filename_chars()`：Windows 集拦 `<>:"|?*`；POSIX 集**放行**
      `:` 与 `?`、拦 `/`（`monkeypatch` 切 `os.name`）。
- [x] 事故回归：`main(["pipeline", "videos/瑙掓枟澹\ue0a6噰璁?mp4"])` 返回
      `EXIT_ARGS`，stderr 含 `[args]` 与修复指引，**不抛 OSError**。
      （事故来源：`角斗士采访` 在 PS 5.1/ACP 936 下的 mojibake）
- [x] 合法中文路径不被拦截（tmp_path 下建中文名文件，`_path_hygiene_error`
      返回 None）。
- [x] 既有 CLI 测试全绿（`tests/test_cli_smoke.py` 等），无行为回归。
- [x] 全量回归：657 passed / 12 skipped / 3 deselected（2026-09-04）。
- [x] 真机验证（Windows PS 5.1 / ACP 936）：
      `uv run video-translate pipeline "videos/角斗士采访.mp4"` 由
      `OSError WinError 123` 变为 `[args] …U+E0A6 (私用区)…` + 三条修法，
      exit code 2，且**不写任何产物文件**。
- [x] URL 豁免（ADR-043 D8）：`_path_hygiene_error` 对带 scheme 的 URL
      （`.../watch?v=<id>`、`.../youtu.be/<id>?t=30`）返回 `None`，且**不由 URL
      派生 base**（`watch?v=` 会派生出含 `?` 的 base）；**同时** `videos/a?b.mp4`
      这类真实含 `?` 的 Windows 文件名**仍被拒** —— 证明豁免只放宽 URL 形态，
      Spec 25 的保护不回退。
      （`tests/test_pipeline_input_routing.py`；后一条按 `os.name != "nt"` 跳过，
      因为 `?` 只在 Windows 文件名里非法。）

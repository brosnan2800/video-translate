---
name: doctor-ffmpeg-hard-gate
overview: 让 `video-translate doctor` 在 ffmpeg/ffprobe 缺失时默认硬失败（退出码 7 / EXIT_DOCTOR_FAIL），无需 --strict，使其与 AGENTS.md「全绿才继续」的约定一致，避免用户被「doctor 退 0」误导后到转写阶段才崩溃。保留 [FIX] 提示与「ffmpeg 已装但 probe 抽风」时的优雅降级；仅 ffmpeg/ffprobe 升格为硬依赖，whisperx/demucs/proxy/nltk 等可选依赖维持默认宽松、--strict 才拦。同步文档与测试。
todos:
  - id: doctor-exit
    content: 修改 cli.py cmd_doctor 出口逻辑，ffmpeg/ffprobe 缺失默认返回 EXIT_DOCTOR_FAIL(7)
    status: completed
  - id: doctor-tests
    content: 在 tests/test_doctor.py 新增 ffmpeg 缺失默认 exit 7 与可选依赖缺失仍 exit 0 断言
    status: completed
    dependencies:
      - doctor-exit
  - id: doctor-docs
    content: 同步 AGENTS.md/TOOLCHAIN.md/docs/TOOLING.md 的 doctor 退出码口径
    status: completed
    dependencies:
      - doctor-exit
  - id: doctor-verify
    content: 运行 uv run pytest 确认 doctor 相关用例全绿
    status: completed
    dependencies:
      - doctor-tests
      - doctor-docs
---

## 需求概述

当前 `doctor`（不带 `--strict`）在 ffmpeg/ffmpeg 缺失时仅打印 `[MISS]` 并退出 0（EXIT_OK），造成"doctor 全绿却 `run` 一转写就崩"的误导。需将 ffmpeg/ffprobe 缺失升格为 doctor 的**默认硬失败**（退出码 7，EXIT_DOCTOR_FAIL），让硬依赖问题在入口即暴露。

## 核心改动

- doctor 默认对 ffmpeg/ffprobe 缺失返回 EXIT_DOCTOR_FAIL(7)；`--strict` 行为不变。
- 其余可选依赖（whisperx / demucs / nltk / proxy / entry BARE 警告等）仍默认宽松，仅 `--strict` 才拦截。
- 缺失时保留既有 `[FIX] setup --ffmpeg` 提示；保留 `analyze_audio` 内部 ffprobe→None 优雅降级与 `transcribe.py`/`ffmpeg_utils.py` 现有抛错语义（不改动）。
- 同步 AGENTS.md / TOOLCHAIN.md / docs/TOOLING.md 的 doctor 退出码口径，并补测试断言。

## 技术栈

Python 3 CLI（argparse），无新增依赖；复用既有 `toolchain.tool_available` 与 `cli._has()` 判定。

## 实现方案

在 `cmd_doctor` 出口判定处，于现有 `if strict and failed:` 之前插入 `if ffmpeg_missing: return EXIT_DOCTOR_FAIL`。`ffmpeg_missing` 变量已在就绪检查循环中根据 `_has("ffmpeg")/_has("ffprobe")` 设置，作用域可达，改动为单点、局部、无新分支复杂度。

关键决策与理由：ffmpeg/ffprobe 是转写抽轨（`extract_chunk`）与时长探测（`probe_duration`）的物理前提，缺失即必崩；而 whisperx（仅 GPU 对齐）、demucs（仅 `--separate-vocals`）、proxy（仅 google 引擎）属可选路径，应继续宽松。把真正的硬依赖单独升格为"默认硬失败"，既消除"doctor 绿却 run 崩"的误导，又不扩大 `--strict` 的拦截面。

## 关键代码改动

cli.py `cmd_doctor` 出口段（原 397-399 行）：

```
    # ffmpeg/ffprobe 是核心流水线的硬依赖：转写缺它无法 extract_chunk 抽轨 /
    # probe_duration 取时长，缺省即必崩。故 ffmpeg/ffprobe 缺失默认硬失败，
    # 不再仅依赖 --strict。其余可选依赖保持宽松，仅 --strict 才拦。
    if ffmpeg_missing:
        return EXIT_DOCTOR_FAIL
    if strict and failed:
        return EXIT_DOCTOR_FAIL
    return EXIT_OK
```

## 实现注意

- 严格复用既有 `_has()`（底层 `tool_available`/`resolve_tool`），不引入散落 `shutil.which`/`os.environ`，符合 Rule 3 工具链查找红线。
- 不改 `analyze_audio` 内部 ffprobe→None 降级（其价值是"ffmpeg 已装但某文件 probe 偶发失败"时 doctor --video 仍出 partial 画像 + 提示），与本次"完全缺失默认硬失败"正交、不冲突。
- 不改 `transcribe.py`/`ffmpeg_utils.py` 抛错语义；不在转写层新增 gate（doctor 前置已足够，避免退出码语义发散）。
- 保留 `[FIX] setup --ffmpeg` 提示块（cli.py 385-388），确保硬失败时仍给出确定性修法。

## 架构与目录

本改动属控制面就绪检查（control-plane readiness）的局部修正，不引入新模块/新模式，沿用现有 `cmd_doctor` 判定结构。

```
src/video_translate/cli.py   # [MODIFY] cmd_doctor 出口逻辑：ffmpeg_missing 默认 EXIT_DOCTOR_FAIL(7)
tests/test_doctor.py         # [MODIFY] 新增 ffmpeg/ffprobe 缺失默认 exit 7、可选依赖缺失仍 exit 0 断言
AGENTS.md                    # [MODIFY] Phase 0 doctor 口径：ffmpeg/ffprobe 缺失 doctor 默认 exit 7
TOOLCHAIN.md                 # [MODIFY] doctor 行为与退出码语义段落
docs/TOOLING.md              # [MODIFY] doctor 行为与退出码语义段落
```
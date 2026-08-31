# 架构重整方案 (architecture-reorg)

> 目标：把"强制翻译流程"的控制权从 Markdown + 包外脚本，收回到 Python 运行时。
> 现状（已实测）：新会话不会走 P0→P1 闸门。根因见第 0 节。

## 0. 背景与根因

现有"强制翻译流程"写在 `docs/PIPELINE.md` + `gates/` 里，但新会话 agent 不执行它。
三层断链，全部命中：

1. **不可见**：`AGENTS.md` 阅读顺序（9–12 行）不含 `docs/PIPELINE.md`，新会话 agent 读不到闸。
2. **不可达**：`gates/` 在仓库根、非包、无 `__init__.py`，运行时 `cli.py` import 不进来。
3. **未被调用**：`cli.py` 仅 3 处注释提及 `gates/`（887、1398、1740 行），`cmd_run`(904) / `cmd_transcribe`(689) / `main()`(1761) 零处实际调用。

本质：**文档不执行，代码才执行。** 把"写下的流程"当成"生效的流程"是错误的。

## 1. 设计原则

1. 文档不执行，代码才执行；代码能判定的全进代码，文档只留判断类红线。
2. 单一入口：agent 不需记流程，一条命令 + 跟随程序打印的 `NEXT`。
3. 状态持久化：流程状态落 `.vt_state.json`，跨会话续跑，不依赖上下文记忆。
4. 推进即校验：每阶段启动先 `enforce()`，不合法非零退出并打印修复命令。
5. 零 shell 依赖：消灭 `make` / `$(...)`，全部 `uv run video-translate <sub>`，跨平台。

## 2. 目标架构

```
AGENTS.md(瘦:1入口+判断红线) ──指路──┐
                                      ▼
vt pipeline 驱动器 (一次性跑到底, 遇停点打印 NEXT)
  src/video_translate/pipeline.py
    ├─ STAGES   声明式阶段图 (唯一真相源)
    ├─ enforce() 每阶段前置闸门
    ├─ State    .vt_state.json 读写
    └─ next()   生成可执行 NEXT 块
        │ 同进程调用 (绕不过)
        ▼
cmd_* 阶段实现 (run/transcribe/generate/verify…)
  每个 cmd_* 首行: pipeline.enforce(stage)
        │
        ▼
src/video_translate/gates/ (进包, 可 import)
  preflight.py content.py acoustic.py verify_loop.py
```

支点：`gates/` 从"仓库根独立脚本"变为"包内模块"——位置决定能否被强制执行。

## 3. 核心机制

### 3.1 状态机 `.vt_state.json` (outdir 内)

```json
{ "schema_version": 1, "base": "x",
  "video": {"path": "videos/x.mp4", "size": 123456, "mtime": 1700000000},
  "stage": "awaiting_translation",
  "stages": {"preflight":"done","signoff":"done","transcribe":"done",
             "translate":"pending","generate":"pending","verify":"pending"},
  "decisions": {"separate_vocals": false, "style": "film", "vad_mode": "none"},
  "confirmed": true, "confirmed_flags": ["--style","film"], "retry": 0,
  "history": [{"at":"...","stage":"transcribe","event":"done"}] }
```

任何命令启动先读它。

### 3.2 闸门内建

```python
# 每个 cmd_* 第一行
rc = pipeline.enforce("transcribe", base=base, video=video, outdir=outdir)
if rc != 0:
    return rc
```

`enforce()` 查：① 前驱阶段完成没；② 本阶段闸门过没过（签字 / 覆盖 / 三 Lane）；③ 素材指纹变没变。

### 3.3 停点协议 (让 agent 不迷路)

程序停在任何位置都打印可直接复制的 `NEXT` 块：

```
[STOP] stage=signoff reason=need_human_signoff exit=10
NEXT: uv run video-translate decide --base x --set separate_vocals=false
NEXT: uv run video-translate decide --base x --confirm
NEXT: uv run video-translate pipeline --video videos/x.mp4
```

agent 照抄 NEXT 即可，新会话失忆也无影响。

### 3.4 退出码 (新增, 避开已占 3/4/6/7)

| 码 | 含义 | agent 行为 |
|---|---|---|
| 0 | 阶段完成 | 读 NEXT 继续 |
| 6 | `AWAITING_AGENT` 等翻译 | 读 task 翻译后写回 |
| 10 | `STOP` 需人工拍板 | 转达用户，拍完重跑同命令 |

## 4. 最终 CLI

```bash
uv run video-translate pipeline --video "videos/x.mp4"   # 唯一入口, 跑到底
uv run video-translate decide --base x --set separate_vocals=true
uv run video-translate decide --base x --confirm
uv run video-translate status --base x
uv run video-translate pipeline --video x.mp4 --force-stage transcribe  # 逃生口
```

`make` 与仓库根 `gates/*.py` 全部下线（Makefile 可保留为 *nix 别名，但不再是权威路径）。

## 5. AGENTS.md 新形态 (≤60 行)

- 唯一入口一条命令
- 停点含义 (exit 6 / 10)
- 判断类红线 5 条（不改时间戳 / 不删缓存 / 覆盖 100% / 对齐后重译 / 不重试退出码）
- 深入阅读按需指路 (`docs/PIPELINE.md`, `docs/adr/`, `TOOLCHAIN.md`)

执行内容全部移除，留在代码里。

## 6. 分阶段迁移

| 阶段 | 内容 | 验收 |
|---|---|---|
| **S1 止血** | `gates/` 迁包为 `src/video_translate/gates/`；`cmd_transcribe` 首行焊 `enforce("transcribe")` | 未签字跑 `run` → 非零退出并打印签字命令 |
| **S2 状态机** | 引入 `.vt_state.json`，阶段完成/签字/指纹统一走它 | 关窗重开 `status` 显示正确阶段 |
| **S3 驱动器** | 新增 `pipeline` 子命令，跑到底 + 停点打印 NEXT | 一条命令从 0 走到 exit 6 |
| **S4 闸门合并** | `checkpoint.py` / `gate.py` / `verify_gate.py` 逻辑并入 `gates/`，删仓库根脚本 | 根目录不再有 `gates/` |
| **S5 文档瘦身** | `AGENTS.md` 压 60 行；`docs/PIPELINE.md` 转流程视图 | 新会话只读 AGENTS.md 也能走对 |
| **S6 契约测试** | 每条红线配"违反必失败"测试 | 故意不签字 → 断言非零 |

S1 可独立先上堵洞；S2–S6 排期做。

## 7. 明确不做的事

- **不重写算法层**：`transcribe.py` / `merge.py` / `fill_gaps.py` / `align.py` / `verify.py` 有真实单测覆盖、已在真跑，是资产。重构只动编排层。
- **不删 `docs/adr/`**：是 rationale 记录，保留。
- **一次只迁一个闸门**，迁完跑全量 `pytest`，避免大爆炸式改动。

## 8. 验收总表

```bash
uv run pytest -q                                  # 全量单测不破
uv run video-translate run "videos/x.mp4"; echo $?   # 未签字 → 非零
# 走完决策单后：
uv run video-translate run "videos/x.mp4"; echo $?   # → 6 (AWAITING_AGENT)
uv run video-translate run "videos/x.mp4" --no-preflight-gate  # 逃生口可用
```

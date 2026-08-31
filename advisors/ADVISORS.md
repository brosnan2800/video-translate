# Phase 0 决策项扩展规范（advisors/）

> **这份文档回答一个问题：以后又分析出新的优化内容，该怎么加进流程？**
>
> 答案：**新建一个文件，不改任何旧代码。** 下面是硬性契约。

---

## 1. 为什么用注册表，而不是"改 doctor 那段代码"

「每次翻译发现新问题 → 往流程里加一段逻辑」是 **shotgun surgery（散弹式修改）**：
补丁全打在共享流程上，越打越脆，且每次改动都可能碰坏上游。

本目录改成**开闭原则**：

| | 旧做法（补丁式） | 本规范（注册表式） |
|---|---|---|
| 新增决策项 | 改 `doctor` / `cli.py` 核心分支 | **新建 `advisor_xxx.py`** |
| 碰到旧逻辑 | 会（同一函数里加 if） | **物理上碰不到** |
| 出错影响面 | 可能波及既有决策 | 仅限新文件 |
| 谁能加 | 得看懂核心流程 | 照模板填三个函数即可（可交给 coding agent） |

注册表在 import 时用 `pkgutil.iter_modules` 自动发现 `advisor_*.py`，**不需要注册表登记、不需要改 `__init__.py`**。

---

## 2. 新增一个决策项：完整步骤

### Step 0 · 先确认 FLAG 在真实 CLI 里存在（**别跳**）

```bash
uv run video-translate run --help | grep -- --your-flag
```

决策单的产出会**直接拼进 Phase 1 命令行**。如果 `FLAG` 拼错或仓库根本没这个参数，
`run` 会在签字之后才炸（`ARGS`，退出码 2），白白浪费一次签字。
**没验证过的 flag，不许写进 advisor。**

若某项暂时只想记录、不想注入命令行 → 设 `FLAG = None` 且不写 `render()`
（缺省渲染遇 `FLAG=None` 返回空列表，等于"只入档不执行"）。

### Step 1 · 新建文件 `advisors/advisor_<你的项>.py`

文件名必须以 `advisor_` 开头，否则不会被发现。
反过来说：**改名成 `.py.example` 即可临时下线一个决策项**，不必删文件
（本目录的 `advisor_merge_max_chars.py.example` 就是这么一份可直接照抄的模板）。

### Step 2 · 导出四样东西

```python
NAME = "my_option"        # 决策项 key，唯一，写进决策单 items 的键名
CRITICAL = True           # True = 未拍板不许 confirm（详见 §3 判定标准）
FLAG = "--my-flag"        # 对应 CLI flag；值本身即完整参数串时设 None 并自写 render()

def advise(video: str) -> dict:
    """算建议 + 给理由。禁止在此做决定，也禁止执行任何副作用。"""
    return {
        "recommended": True,          # bool / str / None（None = 程序无法推断，必须人定）
        "reason": "为什么这么建议（写清阈值和实测值，人要靠这句拍板）",
        "detail": {"metric": 0.04, "threshold": 0.10},   # 可选，审计用
    }

def render(chosen) -> list[str]:      # 可选
    """把已签字的值渲染成命令行片段。缺省行为：
       bool -> [FLAG] 或 []；str -> [FLAG, value]；None -> []"""
    return ["--my-flag", str(chosen)] if chosen else []
```

### Step 3 · 验证（两条命令，必跑）

```bash
uv run python gates/preflight_decision.py propose --base demo --video videos/demo.mp4
uv run python gates/preflight_decision.py show    --base demo
```
新项应出现在决策单里，带建议和理由。**不出现 = 文件名或 NAME 不合规。**

### Step 4 · 完事

不需要改 `Makefile`、不需要改 `preflight_decision.py`、不需要改其它 advisor。
渲染、签字校验、CRITICAL 强制、参数注入全部自动生效。

---

## 3. `CRITICAL` 怎么定：一条判据

> **问自己：这个选项选错了，是"重跑整条流水线才能修"，还是"局部重跑就能修"？**

| 代价 | CRITICAL | 例子 |
|---|---|---|
| 污染整条下游，只能重跑全流程 | `True` | `separate_vocals`（坏地基→转写/翻译/生成全废）、`style`（翻完才发现轨错） |
| 可局部修复，或下游有闸门能抓 | `False` | `vad_mode`（漏句会在 Phase 4 声学 lane 以 uncovered-audio 暴露，重跑单 chunk 即可） |

`CRITICAL=True` 的项**未拍板就不许 `confirm`**，也就进不了 Phase 1 —— 这是它唯一的效力，别滥用（每加一个都在增加你的签字负担）。

---

## 4. 三条硬性禁令

1. **`advise()` 里禁止做决定。** 它只返回建议。决定权在人（`set` + `confirm`），执行权在渲染出的命令行。三权分离，任何一环都可审计。
2. **`advise()` 里禁止有副作用。** 不写文件、不改配置、不跑 Demucs。它可能被反复调用（每次 `propose`）。
3. **禁止静默降级。** 拿不到数据时返回 `recommended=None` + 写清原因（如"仓库模块导入失败"），**不要瞎猜一个值**。静默给错建议比明说"不知道"危险得多 —— 人会以为程序已经检查过了。

---

## 5. `recommended=None` 的正当用途

有两类情况**必须**返回 `None`：

- **程序无法推断**：如 `style` —— 风格是"你要发什么内容"的意图，不是可计算量。`advisor_style.py` 就永远返回 `None`，只把三条轨的适用场景摊出来强制你选。
- **分析失败**：ffmpeg 挂了、仓库模块导入不到。此时写清 `reason`，让人知道"这项没被检查过"。

配合 `CRITICAL=True`，`None` 就等于"**强制人拍板**"。这是诚实设计：程序不假装自己算得出来。

---

## 6. 已有决策项一览

| NAME | CRITICAL | 判据来源 | 说明 |
|---|---|---|---|
| `separate_vocals` | ✅ | `silence_fraction < 0.10` | Demucs 人声分离，选错必须重跑全链 |
| `style` | ✅ | 无（意图） | film / literal / bilingual_study |
| `vad_mode` | ❌ | `mean < -20dB` 或 `max < -5dB` | bare / vad / vad-low / adaptive |

---

## 7. 未来可加的候选（示例，不是待办）

照上面模板即可加入，各自独立成文件。**每个都要先过 Step 0 验证 flag 真实存在**：

- `advisor_merge_max_chars.py` — 按剪映上限与语速实测建议断句字数
  （已备好模板 `advisor_merge_max_chars.py.example`，确认仓库有 `--merge-max-chars` 后去掉 `.example` 即生效）
- `advisor_glossary.py` — 检测素材是否属于已有术语表覆盖的作品，建议挂 `--glossary`
- `advisor_source_hint.py` — 提示补 `--source`（视频出处），影响专有名词译法
- `advisor_align.py` — 按是否有 GPU 建议 `--align auto/none`
- `advisor_model.py` — 按时长与显存建议 whisper 模型档位
- `advisor_verify_breach.py` — 读 `.vt_verify_gate.json`，把上次 verify 的 `breached` 摘要回灌进决策单（`CRITICAL=False`，只提示不拦截；与 Phase 4 硬闸门 `verify_gate.py` 配合，详见 `GAPB_IMPLEMENTATION.md`）

每加一个，你的 Phase 0 就多一道"想清楚再开跑"的提醒，**而核心流程一行不动**。

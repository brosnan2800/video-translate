# 用「外置流程 + 机器闸门」治好 AI 协作失控

> 一份给 `video-translate` 项目的方案文档。它同时回答两类失控——
> **开发失控**（AI 改代码越改越乱）与**翻译失控**（Agent 跑长流程漏步、你不得不全程盯）——
> 并给出同一套解药与可直接拷进项目的成品。
> 第 4、7 章吸收了与 **OpenMontage（开源蒙太奇）** 的代码对比结论（详见对话中的「逻辑归属决策图」）。
> 第 **7.3** 章是后补的关键一刀：**开跑前的人工签字闸门**——治"建议只是打印、默认静默生效"。

---

## 1. 两个失控：同一病灶，但执行者不同

| 维度 | 开发失控（代码） | 翻译失控（流程） |
|---|---|---|
| 现象 | AI 只挖一块、改坏另一块；新会话不读架构文档 | 长流程 agent 漏步；你自己也忘了步骤；全程盯才不出错 |
| 你原话 | "让 AI 自己改，很容易只挖这一块" | "流程太长，我自己也忘了一些；必须盯着干，那我还不如自己干" |
| **执行者** | **coding agent**（Claude Code / Cline / Copilot） | **翻译 agent**（跑 Stage 2 的那个） |
| 根因 | **流程/状态活在人的脑子里** | **流程/状态活在人的脑子里** |
| 解法结构 | 软层 md + 硬层机械闸门 | 软层 md + 硬层机械闸门 |

**病灶相同**：凡是依赖"人记住流程、AI 自觉照做"的协作，都会随复杂度上升而失控——人脑和 AI 的 context 都是易失、易漏、易漂移的。

**但执行者不同，所以约束必须分家。** 这是本方案的第一条铁律：

> ⚠️ **职责错位是最隐蔽的坑**：翻译协作用的 `AGENTS.md` 是给**翻译 agent** 看的流程说明书；它不写你的核心代码。把"别乱改核心、先读架构、加 assert"这类**代码约束**塞进翻译 `AGENTS.md`，等于**让错的人看错的说明书**——翻译 agent 不需要它，coding agent 也不会去读它。代码约束必须放在 coding agent 自己读的文件里（`CLAUDE.md` / `.clinerules` / 独立 `ARCHITECTURE.md`），与翻译 `AGENTS.md` **物理分离**。

### 约束归属清单（谁管什么、写哪里）

| 失控类型 | 执行者 | 软层 md（给对的人看，可跳过） | 硬层闸门（绕不过） |
|---|---|---|---|
| 翻译流程 | 翻译 agent | 翻译 `AGENTS.md` + `PIPELINE.md` | `gates/checkpoint.py`（prerequisites + schema + gated）、`Makefile` 阶段依赖、非零退出码 |
| 代码质量 | coding agent | `CLAUDE.md` / `.clinerules` / `ARCHITECTURE.md`（**不写进翻译 AGENTS.md**） | `.pre-commit-config.yaml`、CI（GitHub Actions）、`assert`、golden 回归测试、模块边界 |

**两份 md 都只是起点，终点都是代码层机械闸门**——不仅代码约束不能放翻译 agent 的 md，连"靠 coding agent 自觉读某份 md"也不是终极防线。md 只当辅助说明书，**真拦人的是闸门**。

## 2. 监督悖论：全程盯 = 自己干

当 AI 的产出质量取决于你全程监工，你的监督成本就逼近亲自动手的成本，AI 的"省力"红利归零。

**结论**：正确目标不是让 AI 更自主，而是——
> **AI 当廉价、不厌烦的「单步执行工」；你当只在红绿灯前做判断的「法官」。**

监工成本→0，法官成本低且高价值。

## 3. 解法总纲：强制力梯度

约束不能只写在文档里等 AI"自觉读"。必须逐级焊成它绕不过去的机械闸门：

```
文档(README/AGENTS.md)  ← 新会话可跳过（你现在在这）
   ↓
自动加载记忆文件(CLAUDE.md/AGENTS.md)  ← 每次注入，仍属"建议"
   ↓
约束写进代码(docstring/assert)  ← 改到那块就看得见
   ↓
测试即回归闸门(pytest golden)  ← 改坏上游立刻红
   ↓
pre-commit / CI 机械门禁  ← 红则禁止合入，物理上绕不过  ← 目标
```

- **闸门（gate）**= 比喻：一切"检查不通过就拦下"的机制。
- **Hook（钩子）**= Git 的自动化机制，在 commit/push 等生命周期点自动跑脚本。
- **pre-commit** = git hook 家族里最常用的一个，时机在 `git commit` 执行、写入仓库之前；脚本非零退出 → 提交中止。
- **CI** = 远程闸门，push 后由服务器（GitHub Actions 等）跑测试+门禁，红则拒绝合入。

## 4. 逻辑归属分层：哪些写死 / 哪些给 agent / 哪些人工

对比 OpenMontage 后最关键的一条：**不是"该不该全交给 agent"，而是哪一层该写死。** 你的现状（agent 只管 `translate`，其余声学/对齐/生成/验证是确定性代码）经对比是**比 OpenMontage 更稳的边界**——声学时间戳、中英索引对齐是精确物理计算，确定性代码比 agent 即兴更可控；OpenMontage 为通用视频生产牺牲了这块精度。

| 层 | 归属 | 典型逻辑（video-translate 场景） | 强制力来源 |
|---|---|---|---|
| ① 代码强制层（写死） | 确定性代码 | 声学时间戳计算、中英索引对齐、字节稳定、产物 schema 校验、阶段 prerequisites 守卫 | pre-commit / CI / checkpoint 退出码（物理绕不过） |
| ② agent 决策层（开放启发式） | 交给 agent | 翻译措辞与语境、边缘情况兜底策略、按语境选模型/工具、**把新规则注册进管线** | 受外围代码闸门约束（进/出都过校验） |
| ③ 人工 gate 层（意图 + 艺术判断） | 你拍板 | **开跑前**：人声分离、风格轨（不可回滚的地基决策）<br>**交付前**：中文顺不顺、情感·语气、文化适配 | 你的红绿灯（**首道** Phase 0 签字闸门 + **末道** verify gate） |

- **写死的（你做对了）**：声学时间戳、中英索引对齐、字节稳定、schema、prerequisites。确定性、可验证、精确计算 → 代码做，不开放给 agent。
- **该开放给 agent 的**：翻译阶段内部"每次翻译发现新问题就加规则"的启发式边缘判断——语义类、难穷举、你永远写不完规则。这些正是"shotgun surgery（散弹式补丁）"的痛苦根源，见第 7.2 章外置法。
- **人工 gate 的**：分两处，**不是只有末道**——

> ⚠️ **人工闸门必须前置一道。** 原设计只在末道设人工 gate，但有一类决策**事后审无意义**：人声分离选错，地基就是坏的，末道再怎么审也只能重跑全链；风格选错，要等翻完才发现轨不对。这类"**不可回滚的地基决策**"必须在开跑前拍板并**签字冻结**，否则默认值会静默生效（仓库 `--style` 默认 `film` 就是活例）。落地见第 7.3 章。

| 人工 gate | 位置 | 判断性质 | 选错的代价 |
|---|---|---|---|
| **首道·签字闸门** | Phase 0→1 之间 | **意图**（你要发什么）+ 地基参数 | 重跑整条流水线 |
| 末道·质量闸门 | Phase 4 verify | **艺术**（中文顺不顺） | 局部重译即可 |

## 5. 翻译线落地闸门（执行者 = 翻译 agent）

> 本章约束**只**写进翻译 `AGENTS.md` / `PIPELINE.md`，硬层落在 `checkpoint.py` / `Makefile`。

1. **流程外置成 `PIPELINE.md`**：你"自己也忘了步骤"= 流程在你脑子里。写成 runbook（Preflight→Transcribe→Translate→Generate→Verify，每阶段输入/输出/闸门），你不用记、agent 不能假装走完。
2. **切成离散 stage 命令**：别给"去把这个视频翻了"这种大模糊任务。每次只调一个 stage（你仓库已有 `run`/`generate`/`verify`），上下文窄、不易跑偏。
3. **每 stage 之间插 `verify` 闸门**：你已有三维门禁（声学/内容/表现），接成每个 stage 后可执行脚本，红则拦下且明确报错"哪个 stage 没过/被跳过"。漏步从静默埋雷→大声失败。
4. **你只审闸门**：绿灯过、红灯才介入。注意力从"盯整部电影"压缩成"看几个红绿灯"。

**分工边界（诚实划定）**：翻译的"中文翻得准不准、顺不顺"机器判不了——那是你该拍板的，但**只放在最后一道 gate 人工审**；前面的机械项（索引对齐、字节稳定、声学时间戳、幻觉信号）全交机器 gate。**机器管机械，你管艺术。**

## 6. 代码线落地闸门（执行者 = coding agent）

> ⚠️ 本章所有约束**独立于翻译 `AGENTS.md`**。翻译 agent 不写代码，不该读这些；coding agent 不做翻译，也不会去读翻译流程文档。**两条线的 md 各自成文，不互相引用、不合并。**

- **软层：给 coding agent 一份自己的约束文件**。Claude Code 自动读 `CLAUDE.md`，Cline 读 `.clinerules`，Codex/Copilot 读仓库 `AGENTS.md`（若与翻译 AGENTS.md 同名冲突，把代码约束放 `CLAUDE.md` + `ARCHITECTURE.md`，翻译流程留在 `docs/` 下的 AGENTS.md，避免混淆）。内容只谈：模块边界、改前必读哪份架构文档、不可动的不变量、改完必跑什么。
- **约束写进代码**：最致命的几条不变量（声学时间戳只携带不重算、中英索引 100% 覆盖…）复制成模块顶部 docstring + 关键处 `assert`——coding agent 不读 md 也躲不过。
- **测试即回归闸门**：golden byte-exact 回归覆盖到 translate 阶段（补丁重灾区），"改一块坏另一块"立刻现形。
- **pre-commit 本地硬闸门**：`.pre-commit-config.yaml` 挂 `pytest` + `ruff`/`mypy` + `make ci`，非零退出 → 提交中止。
- **CI 远程硬闸门**：GitHub Actions push 即跑，红则拒绝合入——你、队友、任何 AI 都绕不过。
- **模块边界（结构性解药）**：开闭原则 + 规则外置（见 7.2），让"加新逻辑 = 新建文件"，物理上碰不到旧流程。

**本目录的成品目前只覆盖翻译线**（见第 8 章）。代码线的三份成品（`.pre-commit-config.yaml`、GitHub Actions 工作流、给 coding agent 的 `CLAUDE.md` 骨架）尚未产出，见第 10 章落地清单。

## 7. 推荐架构：吸收 OpenMontage 精华，不改 agent-only-translate 边界

OpenMontage 的真正价值 **80% 在代码层强制闸门（checkpoint.py 的 GATE VIOLATION / PREREQUISITE VIOLATION），仅 20% 在 agent 灵活**。所以不改成"agent 编排一切"，而是补两刀：

### 7.1 代码层闸门升级（checkpoint 风格）
把 `gate.py` 的"跑测试"升级为 `checkpoint.py` 的"阶段 prerequisites + 产物 schema + gated 审批"三道物理强制：
- **prerequisites 守卫**：前驱阶段未完成，后继直接 `PREREQUISITE VIOLATION`、非零退出——agent 在物理上跳不过去。
- **产物 schema 校验**：每阶段 JSON 产物按 schema 校验，不合法不许标记 `completed`。
- **gated 审批**：标 `human_approval_default` 的阶段（如 translate 中文质量、verify 最终交付），未带 `human_approved` 就标记完成 → `GATE VIOLATION`。
- **状态落盘 `.vt_checkpoint.json`**：唯一真相源，可回放/回滚，复杂度不堆在 context 里。
- 退出码：0 OK / 2 PREREQUISITE / 3 GATE / 4 SCHEMA / 6 AWAITING_AGENT（保留）。

### 7.2 translate 策略注册表（边缘逻辑外置）
把"每次翻译发现新问题就加补丁"从「改核心循环」变成「追加一个 rule 文件」：
- 每个 rule 是纯函数 `(segment, ctx) -> (segment, note)`，注册进 `Registry`，按顺序应用到 `zh_segments`。
- **agent / 你 加新逻辑 = 在 `translate/rules/` 新建一个 `rule_*.py`**，物理上碰不到旧流程 → "改一块坏一块"结构性消失。
- 插入点：Stage 2 Agent 产出 `zh_segments.json` **之后、进 generate 之前**。这是第 4 章「agent 决策层（开放启发式）」的代码落点——规则可插拔，但每条 rule 必须确定性、机器可判；中文语义质量仍由人工末道 gate 审。
- 见 `translate/RULES.md`（写给 agent 的"如何加新 rule"规范）。

### 7.3 Phase 0→1 人工签字闸门（决策单）

#### 要解决的病：建议是软的，默认是静默的

查仓库真实代码后确认了两个漏洞——它们不是 bug，是**设计上的软性**：

| 现状 | 问题 |
|---|---|
| `doctor` 能算出 VAD 路由与人声分离建议，但只 `print` 一句话 | 建议**只是打印**，执行与否靠人/agent 自觉；忘传 `--separate-vocals` → 转写建在坏地基上 |
| `--style` 有默认值 `film`，不传也能跑 | 风格**静默生效**，翻完才发现轨错了 |

共性：**程序知道该怎么做，却没有任何机制保证它被做。** 这正是失控的源头——不是 AI 不听话，是流程根本没设卡。

#### 解法结构：三权分离

不要把"建议 / 决定 / 执行"混在一起，那样谁都能悄悄替你做主：

```
advise  程序算       → 只给建议 + 理由 + 实测值，禁止做决定
  ↓
set     你拍板       → 唯一的决定权，逐项写进决策单
  ↓
confirm 你签字       → 冻结成 confirmed_flags（关键项未拍板 → 拒绝签字）
  ↓
assert  机器守门     → Phase 1 前置闸门，未签字物理进不去
  ↓
render-flags 自动注入 → 参数由决策单渲染，不靠你记得传
```

> **最关键的一环是 `render-flags`。** 它把问题从"检测人有没有传对参数"变成"**参数根本不由人手输**"——从源头消除不一致，而不是事后抓不一致。签过字的东西一定会被执行，你不需要记住任何 flag。

#### 完整过程（真实命令，已跑通）

```bash
# ① Phase 0：体检 + 生成决策单
make preflight VIDEO=videos/x.mp4
#   → doctor 跑完，advisors/ 下每个决策项各出一条「建议 + 理由」
#   → 落盘 videos/x.preflight_decision.json（含视频指纹）

# ② 看单子：哪些程序算得出，哪些必须你定
make decide-show BASE=x
#   [!] separate_vocals   建议:None  理由:无法导入音频分析模块，必须人工判断  <-- 待拍板
#   [!] style             建议:None  理由:风格取决于你要发布什么，程序无法推断  <-- 待拍板
#   [ ] vad_mode          建议:vad-low（已预填，可改）
#   [!] = CRITICAL，未拍板不许签字

# ③ 逐项拍板（非法值当场炸，不留到跑命令时）
make decide BASE=x ITEM=separate_vocals VALUE=true
make decide BASE=x ITEM=style           VALUE=literal

# ④ 签字冻结
make confirm BASE=x
#   [CONFIRM] Phase 1 将使用参数: --separate-vocals --style literal --vad --vad-threshold 0.1

# ⑤ Phase 1：参数自动注入，你一个 flag 都不用记
make transcribe VIDEO=videos/x.mp4 BASE=x
```

#### 五种强制行为（都实测验证过）

| 你的操作 | 机器反应 | 退出码 |
|---|---|---|
| 跳过签字直接 `make transcribe` | `[GATE VIOLATION] Phase 0 决策单未签字，禁止进入 Phase 1` | **3** |
| 关键项没拍板就想 `confirm` | `[GATE VIOLATION] 关键决策项未拍板: ['style']` | **3** |
| `set` 了个非法值（如 `style=poetic`） | `[ERROR] style='poetic' 非法: 不在 choices 内` | 1 |
| 签字后换/重导了视频再跑 | `[STALE DECISION] 视频指纹与签字时不符，禁止沿用旧决策单` | **4** |
| 签字后又 `set` 改了某项 | 签字**自动失效**（`confirmed=false`），必须重签 | — |

- **视频指纹防串单**：决策单记录 `size + mtime`。换素材或重新导出后，指纹不符即 `STALE DECISION`——避免拿上一个视频的决策跑这一个（这个坑在批量处理时特别容易踩）。
- **诚实的"我不知道"**：拿不到音频分析数据时，advisor 返回 `recommended=None` + 写清原因（"仓库模块导入失败"），**绝不瞎猜一个值**。静默给错建议比明说不知道危险得多——你会以为程序已经检查过了。
- **`style` 永远返回 `None`**：风格是"你要发什么内容"的**意图**，不是可计算量。程序不假装自己算得出来，只把三条轨的适用场景摊开强制你选。

#### CRITICAL 怎么定：一条判据

> **这个选项选错了，是"重跑整条流水线才能修"，还是"局部重跑就能修"？**

| 代价 | CRITICAL | 例子 |
|---|---|---|
| 污染整条下游，只能重跑全流程 | `True` | `separate_vocals`、`style` |
| 可局部修复，或下游闸门能抓 | `False` | `vad_mode`（漏句会在 Phase 4 声学 lane 以 `uncovered-audio` 暴露） |

`CRITICAL=True` 的唯一效力就是"未拍板不许签字"。**别滥用**——每加一个都在增加你每次开跑前的签字负担，那正是我们要压缩的成本。

#### 以后又分析出新的优化项，怎么加？

**新建一个文件，不改任何旧代码。** 注册表在 import 时用 `pkgutil.iter_modules` 自动发现 `advisor_*.py`，不需要登记、不需要改 `__init__.py`、不需要动 `Makefile` 和 `preflight_decision.py`。

```python
# advisors/advisor_glossary.py  ← 只需这一个新文件
NAME     = "glossary"          # 决策项 key
CRITICAL = False               # 见上面判据
FLAG     = "--glossary"        # 必须先确认真实 CLI 有这个参数

def advise(video: str) -> dict:
    return {"recommended": None,
            "reason": "写清阈值和实测值，你要靠这句拍板",
            "detail": {}}       # 可选，审计用
```

然后 `make preflight` —— 新项自动出现在决策单里，**渲染、签字校验、CRITICAL 强制、参数注入全部自动生效**。

这就是**开闭原则**对上"补丁式修改"的胜负所在：

| | 补丁式（旧） | 注册表式（本方案） |
|---|---|---|
| 新增决策项 | 改 `doctor`/`cli.py` 核心分支 | **新建一个 `advisor_*.py`** |
| 会碰到旧逻辑吗 | 会（同一函数里加 `if`） | **物理上碰不到** |
| 出错影响面 | 可能波及既有决策 | 仅限新文件 |
| 谁能加 | 得先看懂核心流程 | 照模板填三样即可（**可放心交给 coding agent**） |
| 临时下线一项 | 注释代码，容易漏 | 改名 `.py.example` 即停用 |

三条硬性禁令（详见 `advisors/ADVISORS.md`）：
1. **`advise()` 里禁止做决定**——它只给建议，决定权在人。
2. **`advise()` 里禁止有副作用**——不写文件、不跑 Demucs（每次 `propose` 都会调它）。
3. **禁止静默降级**——拿不到数据就返回 `None` + 说明原因，不许瞎猜。

> 注意这一章和 7.2 是**同一个模式的两次应用**：7.2 让"加翻译规则 = 新建 `rule_*.py`"，7.3 让"加决策项 = 新建 `advisor_*.py`"。凡是"以后还会不断加东西"的地方，都该做成注册表——否则那里就是下一个失控点。

### 7.4 Phase 4 硬闸门 + 自动恢复 + 熔断（Gap B）

上一轮把 `verify` 当成"报告型 + 默认放行"（红线命中退 0、不自动触发、语义回读只产不消费），这是目前离"真不盯全程"最近的一个缺口。本方案把它补成**半自动硬闸门**，同时**强制两条约束**（来自你两次纠偏）：

#### 约束一：自动恢复不能无限循环 → 熔断
红线命中后"重 generate → 重 verify"的自动环路**必须带上限**。一旦 `retry ≥ MAX_RETRY`（建议 2），立刻**熔断转半自动**，停止自动重跑——绝不死循环耗你。

#### 约束二：声学 / 内容 分流；语义回读是独立任务、要有硬代码闭环
- **声学挖出问题**（确定性，有 `silencedetect` 当参考，`find_uncovered_speech` 能精确算出"有声无字幕"区间）→ **可自动修**：回头补那段 translate + generate。这是自动环路的主战场。
- **内容确定性挖出问题**（coverage / index-drift / 漏译）→ 也能自动修漏译段。
- **语义回读 fidelity 命中**（概率性、需 LLM 判断）→ **不进自动环**（修了可能更差），直接转半自动；且不消耗 retry 次数。
- **语义回读是半独立 agent 子任务，必须有硬代码闭环**：真实仓库 `verify.py` 的 `build_semantic_reread_task()` 只写 `<base>.semantic_reread_task.json`（遵守 ADR-005"CLI 永不调 LLM"，回读由你的 agent 执行）。本方案补上**回读结果必须被 gate 消费**——产 `semantic_reread_result.json` 后，gate 检查它，命中即退半自动。现状"产了就完了、不检查、不退码"就是缺这最后一步。

#### 设计结构（分流 + 熔断 + 半自动落点）

```
verify 跑完三维门禁
  │
  ├─ 全绿 ───────────────▶ 写 gate: breached=false → 退 0（可交付）
  │
  ├─ 声学/确定性内容 命中 ─▶ 自动修该段（translate+generate）→ 重 verify → retry++
  │        └─ retry ≥ MAX_RETRY(=2) ──▶ 熔断 ─┐
  │                                          │
  └─ 语义回读 fidelity 命中 ─────────────────┤（不消耗 retry，直接落点）
                                             ▼
                                   转「半自动」：停自动环
                                   写 gate: breached=true, mode=half_auto
                                   → 退 3（GATE VIOLATION，待人工放行）
                                   → 你 `make verify-approve` 或 `make finish` 手动重跑
```

#### 硬代码落点（给 CodeBuddy 的实现手册见 `GAPB_IMPLEMENTATION.md`）
- 新增 `gates/verify_gate.py`：消费 `verify` 三 Lane 结果 + `semantic_reread_result.json`，写 `.vt_verify_gate.json`（机器可读：`lane` / `retry` / `breached` / `mode`），按上面分流决定退码与是否自动重跑。
- 改 `gates/checkpoint.py`：`verify` 标记 `GATED`，`complete verify` 前读 `.vt_verify_gate.json`；`breached` 且未审批 → `GATE VIOLATION`（退 3）。
- 改 `Makefile`：`finish` 的 verify 段改为**单次**（不再假想自动循环）；新增 `make verify-fix`（跑自动恢复环，带 retry 上限）与 `make verify-approve`（半自动放行）。
- 真实仓库 `verify.py` 必须补三刀：①**默认阻断**（非 `--strict` 也退非 0）；②`generate` 后**自动触发 verify**；③**消费 `semantic_reread_result.json`**（现状只产不消费）。

> 两条约束的来由（你纠偏的原话复盘）：
> - *"红线命中之后的动作不能无限循环，如果重 generate 了又 verify 报错了就停下来给半自动"* → 即约束一（熔断）。
> - *"声学挖出问题怎么处理？内容呢？语义回读任务是单独的任务吗？有硬代码支撑？"* → 即约束二（分流 + 语义回读必须有硬代码闭环）。

## 8. 成品清单（见同目录文件）

### 8.1 翻译线成品（执行者 = 翻译 agent）✅ 已产出

| 文件 | 作用 | 拷到哪 |
|---|---|---|
| `PIPELINE.md` | 外置的标准流程 runbook（阶段/命令/产出/闸门/红线） | 仓库根 |
| `Makefile` | gated driver：阶段依赖 + 闸门 + **决策单参数注入**，红则停 | 仓库根 |
| `gates/gate.py` | 三维硬闸门 + 阶段顺序守卫（v1，可执行可失败） | 仓库 `gates/` |
| `gates/checkpoint.py` | **代码层强制闸门 v2**：prerequisites + schema + gated 审批（对齐 OpenMontage） | 仓库 `gates/` |
| `gates/preflight_decision.py` | **Phase 0→1 人工签字闸门**：propose/set/confirm/assert/render-flags（见 7.3） | 仓库 `gates/` |
| `advisors/__init__.py` | 决策项注册表：自动发现 `advisor_*.py`，渲染命令行参数 | 仓库 `advisors/` |
| `advisors/advisor_vocal_sep.py` | 决策项：人声分离（`CRITICAL`，选错重跑全链） | 仓库 `advisors/` |
| `advisors/advisor_style.py` | 决策项：风格轨（`CRITICAL`，永远 `None` 强制人选） | 仓库 `advisors/` |
| `advisors/advisor_vad.py` | 决策项：VAD 路由（非 critical，可局部修） | 仓库 `advisors/` |
| `advisors/advisor_merge_max_chars.py.example` | 新增决策项的**可抄模板**（去掉 `.example` 即生效） | 仓库 `advisors/` |
| `advisors/ADVISORS.md` | **给 agent / 你的"如何加新决策项"规范**（含 CRITICAL 判据、三条禁令） | 仓库 `advisors/` |
| `translate/strategy_registry.py` | translate 阶段策略注册表核心（加载+应用 rule） | 仓库 `translate/` |
| `translate/rules/rule_glossary.py` | 示例 rule：专有名词一致性 | 仓库 `translate/rules/` |
| `translate/rules/rule_long_sentence.py` | 示例 rule：超长句软标记 | 仓库 `translate/rules/` |
| `translate/RULES.md` | 给 agent 的"如何加新 rule"规范 | 仓库 `translate/rules/` |
| `gates/verify_gate.py` | **Phase 4 硬闸门**：三 Lane 分流 + 自动恢复环 + 熔断（见 7.4 / `GAPB_IMPLEMENTATION.md`） | 仓库 `gates/` |
| `GAPB_IMPLEMENTATION.md` | **给 CodeBuddy 的实现手册**：文件/函数/常量/JSON schema/retry 逻辑/4 项 smoke test | 仓库根（或 `docs/`） |

### 8.2 代码线成品（执行者 = coding agent）⬜ 待产出

| 文件 | 作用 | 拷到哪 |
|---|---|---|
| `.pre-commit-config.yaml` | 本地硬闸门：pytest + ruff/mypy + `make ci`，红则提交中止 | 仓库根 |
| `.github/workflows/ci.yml` | 远程硬闸门：push 即跑门禁，红则拒绝合入 | 仓库 `.github/workflows/` |
| `CLAUDE.md`（或 `.clinerules`） | 给 coding agent 的**独立**代码约束（模块边界/不变量/改完必跑），**不与翻译 AGENTS.md 合并** | 仓库根 |

## 9. 用法

```bash
# --- 主流程（注意 Phase 0→1 之间的人工签字闸门）---
make preflight VIDEO=videos/x.mp4           # doctor + 生成决策单
make decide-show BASE=x                     # 看建议/理由/待拍板项
make decide BASE=x ITEM=separate_vocals VALUE=true
make decide BASE=x ITEM=style           VALUE=literal
make confirm BASE=x                        # 签字（关键项未拍板 -> 退出码 3）
make transcribe VIDEO=videos/x.mp4 BASE=x  # 未签字进不来；参数自动注入；预期退出码 6
# <- Stage 2 Agent 翻译，产出 videos/x.zh_segments.json
make finish VIDEO=videos/x.mp4 BASE=x      # check-translate -> generate -> verify
make ci                                    # pytest + 三维闸门（pre-commit / CI）

# --- 决策单单独操作（Makefile 之外）---
uv run python gates/preflight_decision.py show   --base x
uv run python gates/preflight_decision.py assert --base x --video videos/x.mp4  # 前置闸门
uv run python gates/preflight_decision.py render-flags --base x                 # 看会注入什么

# --- 代码层强制闸门（v2，checkpoint 风格）---
uv run python gates/checkpoint.py check translate --base x   # 前置/产物/gated 全过才绿
uv run python gates/checkpoint.py approve translate          # 你人工确认中文质量后审批
uv run python gates/checkpoint.py complete translate --base x # 标记完成（未审批则 GATE VIOLATION）
uv run python gates/checkpoint.py status                      # 看全流程 checkpoint

# --- translate 策略注册表（边缘逻辑外置）---
uv run python translate/strategy_registry.py x   # 应用所有 rule，产出 .zh_segments.rules.json + .rules_audit.json
```

`gate.py` 子命令：`content`(R4 覆盖+对齐后重译) / `acoustic`(R1) / `presentation`(R5) / `all` / `stage-order`(漏步即红)。
`checkpoint.py` 子命令：`status` / `check <stage>` / `complete <stage>` / `approve <stage>`。
`preflight_decision.py` 子命令：`propose` / `show` / `set` / `confirm` / `assert` / `render-flags`。
退出码约定（三个脚本一致）：`0` OK / `2` PREREQUISITE / `3` GATE VIOLATION / `4` SCHEMA·STALE / `6` AWAITING_AGENT。

## 10. 落地清单

- [ ] 把 `PIPELINE.md` / `Makefile` / `gates/` / `advisors/` / `translate/` 拷进仓库，跑一次 `make preflight` 验证命令体系。
- [ ] **校准 advisor 的真实判据**：`advisor_vocal_sep.py` / `advisor_vad.py` 目前 import 仓库音频分析模块，在本目录外拿不到数据时会诚实返回 `None`。接进仓库后确认能真正读到 `silence_fraction` / `mean_db`，让建议从"不知道"变成"算得出"。
- [ ] **验证每个 advisor 的 `FLAG` 在真实 CLI 存在**（`uv run video-translate run --help`）——拼错会在签字之后才炸。
- [ ] 让 Claude Code 把 `checkpoint.py` 的 `ARTIFACTS` schema 按仓库真实 `zh_segments.json` / `segments_en.json` 字段对齐（这是参考骨架，路径/字段需适配）。
- [ ] 让 Claude Code 把 `strategy_registry.py` 接入 Stage 2→Stage 3 之间（产出 zh 后、generate 前）。
- [ ] 下次翻译：**先签字，再只跑 `make`**，红灯才介入——验证"不用盯全程"是否成立。

**（Gap B · 必做，已设计）** Phase 4 从"报告型"升级为"半自动硬闸门"。完整实现手册在 `GAPB_IMPLEMENTATION.md`，交给 coding agent（CodeBuddy）照做。落地清单：
- [ ] 新增 `gates/verify_gate.py`：`MAX_RETRY=2`；消费 `verify` 三 Lane + `semantic_reread_result.json`；写 `.vt_verify_gate.json`；声学/确定性内容命中→自动修该段→重 verify→`retry++`；`retry≥2` 或语义命中→熔断转半自动（退 3）。
- [ ] 改 `gates/checkpoint.py`：`verify` 标 `GATED`；`complete verify` 前读 `.vt_verify_gate.json`，`breached` 未审批→`GATE VIOLATION`（退 3）。
- [ ] 改 `Makefile`：`finish` 的 verify 段改为单次；新增 `make verify-fix`（自动恢复环，带上限）与 `make verify-approve`（半自动放行）。
- [ ] 改真实仓库 `verify.py`：①默认阻断（非 `--strict` 也退非 0）；②`generate` 后自动触发 verify；③消费 `semantic_reread_result.json`（现状只产不消费）。
- [ ] 4 项 smoke test：uncovered-audio 自动修 retry=1 转绿 / 语义命中立即半自动 retry=0 / 声学持续超限 retry=2 熔断 / 全绿退 0。

**代码线（执行者 = coding agent，与上面翻译线分开做）**

- [ ] 给仓库加 `.pre-commit-config.yaml`，挂载 `pytest` + `ruff`/`mypy` + `make ci`（本地闸门）。
- [ ] GitHub 加 Actions：push 即跑 `make ci`（远程闸门，你/队友/任何 AI 都绕不过）。
- [ ] 建一份**独立于翻译 AGENTS.md** 的 coding agent 约束文件（`CLAUDE.md` / `.clinerules`）：只写模块边界、改前必读哪份架构文档、不可动的不变量、改完必跑什么。
- [ ] 把最致命几条不变量挑 2–3 条写成 `assert`（如 `assert len(zh)==len(en)`）+ 模块顶部 docstring。
- [ ] golden 回归测试覆盖到 translate 阶段（补丁重灾区）。

---

*核心一句话：流程外置成文件，关卡焊成机器闸门，约束**按执行者分家**（翻译 md 归翻译 agent、代码 md 归 coding agent，不混），逻辑按"写死 / agent / 人工"分层归属，**不可回滚的决策在开跑前签字冻结、参数由决策单自动注入**，人只审首末两道闸门、不盯全程。凡"以后还会不断加东西"的地方都做成注册表（`rule_*.py` / `advisor_*.py`），加新逻辑 = 新建文件。失控由此被结构性消除，而非靠自觉。*

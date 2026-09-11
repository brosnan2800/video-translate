# G3L 方案（讨论稿）— G3 递进式多模型人声分离救回（ASR 指标选优）

Date: 2026-09-11
Status: Discussion Draft（讨论稿，非正式 ADR；位于 `docs/drafts/`）
Companion: ADR-034（G3 四组进入条件）, ADR-017（vocal_sep）, ADR-016（fill_gaps）,
ADR-021（恢复段幻觉守卫）, ADR-036（prefix collapse）, Spec 16 / Spec 19。

> **本稿是「未来可能做」的讨论底稿，不是已定决策。** 内容收敛后再决定归属
> （并入 ADR-034，或新开 ADR）。
>
> 主题：G3 分离救回的升级草案（G3L = G3 Level-up）。在 ADR-034 §3.4「G3 局部
> separate-vocals」之上，把「单模型跑一次」升级为「**失败窗递进多模型 + ASR 指标选优**」。
> 只对 G3 中 self-check 仍 MISSING 的窗生效，干净窗 / 已救回窗绝不触碰。

---

## 1. Context（背景）

### 1.1 触发：角斗士混剪 13 窗救不回

`videos/角斗士混剪.mp4` G3 实测（`角斗士混剪.review.log`）：

- 进 G3：**18 窗** = 3 窗成功 + **13 窗 `unrecoverable`** + 2 窗笑声预筛跳过；
- 另有 8 窗由 G2（`fill_gaps` 强制解码）救回，未用到分离。

即：**当前 G3 对大部分强 BGM 窗「分不出来」**。日志逐窗为
`self-check still MISSING — marked unrecoverable, NOT spliced`——系统选择「宁可留空，
也不拼接假内容」（ADR-034 组4），但真音因此永久丢失。

### 1.2 根因：不是「没走 G3」，而是「单模型能力不够 + 模型选型不是最优」

- **G3 确实跑了**（18 窗，`vocals.wav` 产物为证），问题不在触发，在**救回率**。
- 当前 G3 硬编码单一模型 `htdemucs`（`fill_gaps.py:727` 的 `g3_demucs_model`，
  `cli.py:858` 甚至未透传该参数）。`htdemucs` 是**通用 4-stem 音乐源分离模型**，不是
  为人声优先任务优化到极致的模型。
- 用户实证 + 调研：`htdemucs_ft` / `mdx_extra` / UVR 的 MDX 类人声模型，在「人声 vs
  配乐」上各有更强项，**换模型有机会把 13 窗里「分得动但分不净」的一部分救回**。

### 1.3 关键约束：分离伪影本身会损害 ASR（务必记住）

调研结论（用户 2026-09-11 查证 + 相关 ASR 文献）：

- **目标是 ASR，不是「听起来干净」**：分离得越狠 ≠ 识别越好。`mdx_extra` 是**不同
  架构**，在某些音频更干净，在另一些会产生金属音 / 水声 / 人声断裂 / 齿音消失 /
  BGM 残留颗粒声——这些**伪影本身会诱发 Whisper 漏字与幻觉**。
- **无差别全片分离有害**：Demucs 可能损伤本来清楚的语音（齿音、弱音节衰减）。近期
  ASR 实验显示：按「音乐存在程度」**选择性处理**优于对所有片段无差别分离。
- **标准是 WER/CER、漏字、幻觉数、时间戳稳定性**，不是耳朵。

> 结论：换模型必须是**递进 + 有条件 + 以 ASR 指标选优**，而非「一路换到最狠」。

### 1.4 现状代码落点（初稿依据）

| 关注点 | 位置 | 现状 |
|---|---|---|
| G1/G2/G3 编排 | `fill_gaps.py:685`（`fill_gaps()`），顺序调用 `_apply_g1`/`_apply_g2`/`_apply_g3`（`1387/1389/1393`） | 顺序编排，非嵌套 |
| G3 决策 | `_apply_g3()` `fill_gaps.py:1254` | 逐窗 separate → 能量复核 → self-check |
| 模型参数 | `g3_demucs_model` `fill_gaps.py:727`（默认 `"htdemucs"`），透传 `separate_window` `fill_gaps.py:1323` | **硬编码，CLI 未透传** |
| 分离函数 | `separate_window()` `vocal_sep.py:439-450` | 已支持任意 `model_name` |
| 自校验 | `g3_self_check_ok()` `fill_gaps.py:664-682` | 重解码→再过 `review_segments`，仍 MISSING 即 False |
| 结果分支 | recovered `fill_gaps.py:1362-1376` / unrecoverable `1355-1361` | 单模型一次定生死 |
| 能量复核 | `g3_energy_verdict()` `fill_gaps.py:610-628`（读 vocals vs other） | 笑声窗跳过 |
| 预筛 / 预算 / 短窗合并 | `g3_prescreen()` `595` / `select_g3_windows()` `631` / `group_short_windows()` `569` | 组2/3 已实现 |
| 窗口缓存 | `window_fingerprint()` `vocal_sep.py:426-435`，命名 `471-475` | **fp 含 model → 换模型自动新文件** |
| review 缓存 | `<base>.review.json`，键含 `params`（含 `g3_demucs_model`）`fill_gaps.py:780` | **改模型 → 全量重跑 G1/G2/G3** |
| CLI 逃生门 | `--no-g3` `cli.py:2258/2381`；`--demucs-model` `cli.py:2248` 仅影响**主 pass** | 无 G3 模型/阶梯 flag |

---

## 2. Decision（架构决策）

把 G3 升级为 **G3L：递进式多模型救回 + ASR 指标选优**。三条铁律：

1. **有条件递进（不是固定质量等级）**：仅对 G3 中 self-check 仍 MISSING 的窗，逐级
   尝试阶梯中的下一模型；某级通过即停止（早停）。
2. **ASR 指标选优（不是音频干净度）**：每个候选（模型级）分离出的 vocals 重解码后，
   用统一 ASR 指标打分，选**最优**一版缝合；**「更干净但幻觉更多」的候选被拒绝**。
3. **只碰失败窗 + 早停**：干净窗与已救回窗（G1/G2 已修复）绝不进入阶梯，算力只花在
   真正漏译的窗上。

### 2.1 模型阶梯（默认）

```
① 原音频（主 pass 已裸跑，无需重跑——见 §2.2）
     ↓ 裸跑漏译 → 双信号 review 判 MISSING
② G3-L1  htdemucs        — 快、通用、稳定；现有默认，首选
③ G3-L2  htdemucs_ft     — 同 HTDemucs 路线微调，更偏「保留人声」，约慢 4×
④ G3-L3  mdx_extra       — 不同架构（MDX-Net）；复杂伴奏 / 持续和声 / 频段重叠时试
⑤ G3-L4  UVR MDX-Net / MDX23C  — 可选扩展（stretch），需额外权重/集成，不阻塞前三级
```

- **L1–L3 为 demucs 内置**（`-n <model>` 即可，零新依赖）；
- 每级只在**上一级 self-check 仍 MISSING** 时才跑；
- 阶梯可配置，默认 `["htdemucs", "htdemucs_ft", "mdx_extra"]`。

### 2.2 为什么不重跑「原音频」

ADR-034 §2.1 起主 pass 已默认裸跑（`use_vad=False`）。G3 的触发前提就是「裸跑已经漏了
这窗」，故**原音频这一级已隐含在流程上游**，G3L 阶梯从 L1(`htdemucs`) 开始，不再重跑
原音频。§1.3 的「选择性处理」正是由此保证：只有 review 判定漏译的窗才进阶梯。

### 2.3 ASR 选优指标（核心）

对每个候选重解码结果 `segments`，复用 `review.py:237`（`review_segments`）与
`g3_self_check_ok` 的同一套信号打分：

| 指标 | 定义 | 方向 | 来源 |
|---|---|---|---|
| `missing_count` / `missing_dur` | review 判 MISSING 的窗数 / 时长 | 越低越好（**主指标**） | `review.py` |
| `halluc_count` | 静音窗出文本 + 低置信度段（`nsp≥0.6` / `alp<-1.0`） | 越低越好 | `review.py` / ADR-021 守卫 |
| `ts_stability` | 与 silencedetect 边界 + 邻居段的漂移 | 越低越好 | `audio_profile` |
| `proper_noun`（二期） | 专名 / 数字准确度（glossary + 正则比对） | 越高越好 | glossary |

- 综合分加权 → 选最高分候选缝合。
- **伪影防线**：若某级「音频更干净」但 `halluc_count` 上升（伪影诱发），打分自然拒绝它；
  这正是「以 ASR 而非耳朵」的落地。
- 现有 `g3_self_check_ok` 本质就是「重解码 → `review_segments` 是否还 MISSING」，G3L
  把它从**单级布尔**泛化为**逐级布尔 + 跨级打分**。

### 2.4 四组进入条件的扩展（不改 ADR-034 组1/组2 骨架）

- **组1 硬前置**：不变（review 命中漏译 + G1/G2 空 + 窗长 ≥5s）。
- **组2 场景适配**：**能量复核（笑声窗检测）在 L1 前只做一次**——笑声窗对所有模型都
  分不开（ADR-034 §2.3「G3 不兜笑声」），直接早退，避免白跑整个阶梯。
- **组3 性能预算（重定义）**：递进成倍耗时，新预算 = `Σ(窗时长 × 已尝试级数)` 上限，
  并叠加「单窗总耗时上限」。超限的窗不再降级，按当前最优候选收尾 + 报告。
- **组4 自校验（升级为逐级 + 选优）**：逐级 self-check；通过即该级胜出并早停；到顶仍
  MISSING → 在**所有候选**中按 ASR 指标选最优（可能「虽不完美但最好」），仍不达标才
  终标 `g3-ladder-exhausted`。

---

## 3. 详细设计

### 3.1 控制流（伪代码）

```
for window in kept_windows:                     # 组3 预算筛选后
    # 组2 笑声预筛：一次
    sep_L1 = separate_window(window, model=L1)
    if energy_verdict(sep_L1) == "nothing separated":   # 笑声档
        mark(window, "laughter"); continue

    best = None
    for level, model in enumerate(ladder):              # L1 → L4
        vocals, other = separate_window(window, model)  # per-window 缓存复用
        cand = re_decode(vocals)                        # Whisper
        score = asr_score(cand)                         # §2.3
        best = argmax(best, cand, key=score)
        if g3_self_check_ok(cand):                      # 逐级自校验
            pick(cand); break                           # 早停
        if over_budget(window, level):                  # 组3
            pick(best); mark(window, "budget"); break
    else:
        pick(best) if best else mark(window, "g3-ladder-exhausted")
```

### 3.2 缓存（关键工程点）

- **窗口级 `vocals.wav`/`.other.wav` 缓存已含 model**（`vocal_sep.py:471-475`）→
  各级分离产物**天然共存**：重跑只补缺失级，已算过的级直接 `[skip] cached`。
- **但 per-video `review.json` 的 `params` 键含 `g3_demucs_model`**（`fill_gaps.py:780`）
  → 改阶梯会**全量重跑** G1/G2/G3。改造分两步：
  - **A（最小，一期）**：`params` 键由单一 `g3_demucs_model` 改为序列化 `g3_ladder`，
    语义正确但换阶梯仍全量重跑；
  - **B（推荐，二期）**：新增**窗口级 G3 结果缓存**（键 = 窗 tag + 阶梯 + 各候选指纹），
    使「只重跑某些窗」（用户诉求）成为可能；review.json 只缓存「窗 → 最终选优结果」。

### 3.3 CLI 透传（修补缺口）

- 新增 `--g3-ladder`（默认 `htdemucs,htdemucs_ft,mdx_extra`）；
- 新增 `--g3-max-level` / `--no-g3-ladder`（退化回单级 = 现状，便于对照与回滚）；
- 修补 `cli.py:858`：把 `g3_demucs_model`（改为 `g3_ladder`）真正透传给 `fill_gaps(...)`；
- 保留 `--no-g3` 逃生门。

### 3.4 落盘 / 可观测（呼应用户的 P0→P1 停顿判断）

- `review.log` 逐窗打印，例如：
  `[audit] G3L 8.0-14.4s: L1 htdemucs MISSING → L2 htdemucs_ft recovered (missing 2→0, halluc 0) → pick L2`
- `review.json` 的 `g3_unrecoverable` 升级为 `g3_ladder_report`：每窗记录
  **尝试过的模型阶梯 + 各级 ASR 指标 + 最终选择/放弃 + 终止原因**。
- **翻译前门禁（P0→P1）**：把该报告在进入翻译前呈现给人，决定「人工听音补 / 接受缺口 /
  换更强模型重跑」——而不是译完才在 verify 报红灯。
- 终止原因枚举：`g3-ladder-exhausted` / `laughter` / `budget-dropped`（区分「试过所有模型
  仍不行」与「笑声档」「超预算未试完」）。

### 3.5 设备与性能

- 沿用 `pick_separation_device`（G3 运行时 Whisper 已占显存，探测剩余显存决定 GPU/CPU）。
- 增量成本估算：绝大多数窗 L1 成功（本片 3/18 + G2 8 窗）；仅 L1 失败的 ~13 窗进
  L2/L3 → 增量 ≈ 13 窗 × 1~2 额外级。早停 + 阶梯预算把最坏情况封顶。
- `htdemucs_ft` 慢约 4×：只在 L1 失败窗触发，且若不通过会进 L3，需纳入预算。

---

## 4. 与既有 ADR 的关系

- **ADR-034**：本 ADR 是 §2.3 / §3.4「G3」的**增强**，不改四组骨架（组1/组2 不变，
  组3 预算重定义，组4 升级为逐级+选优）。
- **ADR-017**：沿用其「局部 separate」能力边界论述；本 ADR 补充「多模型递进」维度。
- **ADR-016 / ADR-021**：G3L 缝合的恢复段仍须过 `_is_recovered_hallucination` 守卫。
- **ADR-036**：prefix-collapse 不在本 ADR 范围。
- **Spec 19 / Spec 16**：接口与参数文档同步。

---

## 5. Consequences

### 5.1 涉及文件

- `src/video_translate/fill_gaps.py`：`_apply_g3` 改多级循环 + 打分选优；
  新增 `g3_ladder` 参数、ASR 选分函数、阶梯报告落盘。
- `src/video_translate/vocal_sep.py`：无需改（`separate_window` 已支持 `model_name`）；
  可能新增「阶梯候选指纹」辅助。
- `src/video_translate/cli.py`：新增 `--g3-ladder`/`--g3-max-level`，修补 `858` 透传。
- `src/video_translate/config.py`：可选新增 `g3_ladder` 配置（env `VT_G3_LADDER`）。
- `docs/`：Spec 16 / Spec 19 同步；AGENTS.md 决策点表补 G3L。
- `tests/`：TDD 先行（demucs 全 mock）。

### 5.2 验收标准

- 角斗士混剪 13 个 `unrecoverable` 窗：L2/L3 至少多救回若干（以实测 WER/漏字为准）。
- 干净窗 / 已救回窗**零触碰**（只处理 MISSING 窗）。
- 换阶梯不引入新幻觉（`halluc_count` 守卫）；「更干净但幻觉更多」的候选被拒绝。
- 换阶梯缓存语义正确（一期全量重跑可接受；二期支持只重跑某些窗）。
- 全量 pytest 绿 + 新增 G3L 单测（阶梯选择 / 早停 / 选优 / 预算）。

### 5.3 风险与回退

| 风险 | 触发 | 对策 |
|---|---|---|
| `htdemucs_ft` 慢 4× 拖垮预算 | 大窗进 L2 | 早停 + 阶梯预算 + 仅失败窗 |
| `mdx_extra` 伪影加剧幻觉 | 金属音/齿音消失 | ASR 选优拒绝「更干净但幻觉多」 |
| 逐级重跑重复算力 | 换阶梯全量重跑 | 窗口级 model 缓存复用，只补缺失级 |
| 换模型污染 golden / 缓存 | 默认阶梯变化 | 阶梯入 `params`，缓存语义化；golden 本地重确认 |
| UVR MDX 集成复杂度 | L4 | 标 **stretch**，不阻塞 L1–L3 |
| 用户嫌慢 | 默认开启递进 | `--no-g3-ladder` 退化单级（= 现状） |

### 5.4 分期

- **一期**：L1–L3（demucs 内置）阶梯 + ASR 选优 + CLI `--g3-ladder` 透传 + 日志/落盘。
- **二期**：窗口级 G3 结果缓存（只重跑特定窗）+ P0→P1 门禁报告。
- **三期（可选 stretch）**：L4 UVR MDX-Net / MDX23C 集成。

---

## 6. 待定问题（Open Questions）

1. **选优权重**：`missing` / `halluc` / `ts_stability` 的权重初值？（建议 `missing` 主导）。
2. **阶梯默认**：是否默认开启 L2/L3，还是 `--g3-ladder` 显式启用（保守）？
3. **L4 集成路径**：UVR MDX 走 demucs 内置的 `mdx_extra` 系列即可，还是需引入独立 UVR
   工具链/权重？影响是否作为 stretch。
4. **P0→P1 门禁的呈现形态**：log 摘要 / 结构化表格 / pipeline 停点提示？
5. **短窗 <5s**：ADR-034 用「合并邻近可疑窗」规避；G3L 是否对合并后的长窗沿用同一策略。

---

## 7. 落地清单（初稿 → 实现）

- [ ] `fill_gaps.py`：`g3_demucs_model` → `g3_ladder`；`_apply_g3` 多级循环 + 选优。
- [ ] `fill_gaps.py`：`asr_score()` 纯函数（复用 `review_segments` 信号）。
- [ ] `fill_gaps.py`：`g3_ladder_report` 落盘（替换/增强 `g3_unrecoverable`）。
- [ ] `cli.py`：`--g3-ladder` / `--g3-max-level` / `--no-g3-ladder`；修补 `:858` 透传。
- [ ] `review.json` 缓存 `params` 键改 `g3_ladder`（一期）→ 窗口级缓存（二期）。
- [ ] `tests/`：阶梯 / 早停 / 选优 / 预算 单测（demucs mock）。
- [ ] `docs/`：Spec 16 / 19 同步 + AGENTS.md 决策点表。
- [ ] 实测：角斗士混剪 13 窗重跑，记录各级救回率与 ASR 指标。

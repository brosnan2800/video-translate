# Post-mortem: Jamie Foxx 视频字幕翻译踩坑记录

- **视频**：`Jamie Foxx Making Celebrities Cry With Laughter.mp4`（粉丝致敬/脱口秀混剪，笑声+配乐+重叠说话多，信噪比低）
- **工具**：`video-translate`（faster-whisper large-v3, CPU int8 + 人格化翻译 agent）
- **最终字幕版本**：`_v7`（391 段，双语/纯中/纯英/txt 四件套）
- **相关提交**：`ea77a83`（代码加固）、`24f98ed`（文档同步）、`1fbc98e`（大版本计划改名）
- **配套规格**：[Spec 16 fill-gaps](specs/16-fill-gaps.md)、[Spec 17 verify-align](specs/17-verify-align.md)、[ADR-011 vad-opt-in](adr/011-vad-opt-in.md)
- **V7 及更早的坑**：见 `AGENTS.md` 的 `### Hard-won pitfalls` 段（quiet/low-volume 那批）。本文只覆盖 **V8–V13** 这次翻译实际踩到的坑。

---

## 一句话结论

这次翻译暴露的六类缺陷，根子都在「**把单次 Whisper 转录当成真理**」——它没有覆盖率自检、也没有自愈闭环。最终的成片质量能超过机器翻译/agent 直译，**赢在先把地基打平（转录完整 + 索引对齐），再让人格化翻译发挥**；地基错了，翻译层照单全收一堆缺字/串台的字幕。

---

## 缺陷清单（V10–V13）

| # | 缺陷 | 用户最早报的表象 | 模块 | 状态 |
|---|---|---|---|---|
| 1 | 大段漏字（双静音闸） | 「1分27到1分41秒没字幕」 | transcribe / fill_gaps | ✅ 已闭环 |
| 2 | 段内塌陷 | 「put two 前漏几秒说话」 | fill_gaps | ✅ 已闭环 |
| 3 | prefix collapse（探洞 pad 写死） | 同上，更深一层 | fill_gaps | ✅ 已闭环 |
| 4 | 回声漏网 | 同一句出现 2–3 次 | fill_gaps | ✅ 已闭环 |
| 5 | 译文索引漂移（zh/en 错位） | 「里根那句起中文越来越乱」 | verify_align | ✅ 已闭环 |
| 6 | 残余空洞 | 「招牌后」「七宗罪后」漏字 | fill_gaps / 手动补 | ✅ 已闭环 |
| 7 | 重叠回声 + 0.44s 闪幕 | 「Oval Office 压很短、后面又过长」 | merge / generate | ⏳ 设计完成，未落地 |

---

## 1. 大段漏字（两道静音闸门连环漏切）

- **表象**：粉丝混剪类视频大段人声整段缺失；访谈/脱口秀剪辑却几乎不出问题。
- **根因**：两道独立闸门叠加——
  1. Silero VAD 默认开 → 把「笑声/配乐里的说话」整段判静音丢弃；
  2. 即便关 VAD，`transcribe_video()` 没传 `no_speech_threshold`，吃 faster-whisper 默认 `0.6` → 低信噪比窗口直接吐空。
- **关键认知**：**是内容类型决定漏不漏（信噪比低），不是视频长度、也不是音量高低。**
- **修复**：`transcribe.py` 默认 `use_vad=False`（VAD 改为选开）、`NO_SPEECH_THRESHOLD=0.0`、`TEMPERATURE_FALLBACK=[0.0,0.2,0.4]`；指纹纳入新参，旧缓存自动失效。`fill_gaps.py` 扫描 HEAD/内部/TAIL 三类时间轴空洞 → 强制解码（`no_speech=0`）→ 回声去重 → 真语音插回。
- **教训**：工具的「默认」必须贴合最差内容类型（混剪），而不是最干净样本。

## 2. 段内塌陷（in-segment collapse）

- **表象**：时间轴连续、gap 扫描完全看不见，但某段 13s 只吐了 44 字符（整段 Oprah 模仿被吞）。
- **根因**：Whisper 把一段长语音压成极短文本，时间窗没断、只是内容塌陷。gap 扫描盲区。
- **检测**：字符密度 `cps = len(text)/dur`，对比**本文件自身中位数**；`dur>=4s 且 cps < median*0.45` 判塌陷（用本文件中位数而非绝对阈值 → 跨语种/语速鲁棒）。
- **修复**：`fill_gaps.find_collapsed()`，塌陷窗重解码，若产出 ≥2 段或字符数 >1.6× 原文则替换原段。

## 3. prefix collapse（探洞 pad 写死触发「首段锁定」）

- **表象**：同一个 28s 空洞，流水线只解出 `'than usual.'`，整窗内容全丢。
- **根因**：探洞 pad 写死 `0.5s` → 把上一段话尾拖进解码窗口开头，解码器咬住该碎片后立即预测 `end-of-transcript`，整窗剩余音频判为「无内容」。
- **实证 A/B**（同窗口、同模型同参数，唯一变量 pad）：
  - `pad=0.5` → **1 段**（`'than usual.'`，全丢）
  - `pad=0.2` → **7 段** 完整对白（特朗普/Snoop/Death Row 整段）
  - `language=None` 与 `language='en'` 结果完全相同 → 证伪「逐窗误检语种」的直觉归因。
- **修复（不是换个赌注）**：`_PROBE_PADS=(0.2, 0.0, 0.5)` 多 pad 探测 + 按「解码覆盖时长」择优；窗口 ≥4s 才多探，覆盖 ≥60% 提前退出，常见情况仍是单次解码。

## 4. 回声漏网（同一句的不同转写变体）

- **表象**：`he bit my earl.` vs `He bit my ear off.` —— 同一句字幕出现 2–3 次。
- **根因**：`Jaccard=0.5` 低于 `0.6` 阈值，但其实是同一句的转写变体，未被判为回声。
- **修复**：`_is_echo` 增加字符级相似度 `difflib.SequenceMatcher > 0.7`，多剔除 4 条重复。

## 5. 译文索引漂移（zh/en 错位，最隐蔽）

- **表象**：英文轨全对，中文轨从「演罗纳德·里根的黑人」起整体后移一位、越往后越乱。
- **根因**：agent 逐批写 `zh_part_N.json` 时按行生成 index，中间漏读一行 → 该批之后全部平移 +1（`zh[i]` 实为 `en[i+1]`）。**英文轨不变 → 流水线完全不可见**（段数对、索引连续、无空值、SRT 正常），属静默失败。
- **定位**：错位在 v3 翻译时已引入；v4 按 `(start,text)` 键复用旧译文 → 错误被**继承并放大**。
- **修复**：不做机械平移（v4 夹了 40 段，硬移会二次污染）→ 直接**重译 159–388 共 230 段**，写入前做索引全覆盖断言（`exp==got`，无缺无重）。
- **固化**（新模块 `verify_align.py`）：
  - `check_alignment()`：**长度剖面 Pearson 相关**——滑窗内 `len(en_i)` 与 `len(zh_i)` 的相关，与 shift=±1/±2 比较，高出 margin 即判漂移。无需词表，跨语种通用。
  - `check_digits()`：源文数字应出现在译文，缺失但在邻段出现 = off-by-N 指纹。
  - 接进 `cmd_generate`，渲染前自动跑、仅告警不阻断；`--no-align-check` 可关。
  - 验证：真实错位版检出 `segs 168-192 drifted +1`；修复版 `[align] ok` 零误报；人工注入 shift+1@250 检出 corr 0.967。

## 6. 残余空洞闭环

- **表象**：V10 审计曾宣称「剩余空洞 100% 为 echo」，用户仍报 50.40 与 195.76 两处漏字。
- **处理**：把 11 个 >3s 空洞逐一强制解码，**逐洞比对邻段上下文**防回声误插：
  - `50.40→53.88`：`calling card, you get your hands off me.` —— 真语音（「招牌」后）→ **补**
  - `195.76→202.32`：`Jealousy, envy, sloth, wrath, Ronnie, Bobby, Ricky, Mike.` —— 真语音（「七宗罪」后接七罪+名字 roll）→ **补**
  - 其余 9 个：真静音 / 邻段尾泄漏 / 整窗强解猜词 / <0.5s 碎片 → 全跳过。
- **补字纪律（避免触发 #5 的 off-by-N）**：插入中段会令后续所有 zh 键 +2 平移。做法——原段打源索引、附 recovered 段、按 start 排序后**按对象身份重建 zh**，断言集合相等，绝不盲移。
- **交付**：`segments_en.json`/`zh_segments.json` 升级 391 段；`verify_align` 跑通 `[align] ok`；出 `_v7`。

---

## 待办 / 未闭环（#7）：重叠回声 + 0.44s 闪幕

**已设计确定性修复规则，尚未落地、未文档化进代码。** 以实测数据为例：

| 段 | 时间轴 | 文本 |
|---|---|---|
| A | **373.40 → 374.54** | `the way to the Oval Office.` |
| B | **374.04 → 379.74** | `Oval Office when I first started out it was all about presidents because I would` |

- **现象**：B 起点 `374.04` 落在 A 尾巴（`374.54`）内，**重叠 0.5s**；B 头 `Oval Office` 复述 A 尾 → 看着像「把上一句尾巴偷走接在自己前面」。A 被 `_emit` 收紧到末词尾 `373.84`，显示成 **0.44s 闪幕**（词时间戳失真），还没看清就跳到 B。
- **归谁管**：这跟 `fill_gaps` 的 pad 无关，是 **`merge.py` 字幕条切分/边界**问题。
- **设计的确定性规则**（挂 `merge.py` 新 stage `repair_overlap_echo`，放在 `snap_drifted_words` 之后、`split_long_cues` 之前）：
  1. **闸门1 时间重叠/贴脸**：`B.start < A.end` 或 `0 <= B.start - A.end < 0.30`
  2. **闸门2 A 是完整句**：`A` 以 `[.!?]` 结尾（否则 B 可能是 A 被切开的真续句，别动）
  3. **闸门3 文本重复**：取 A 尾/B 头各 4 词做最长公共 n-gram（`_max_shared_ngram`，复用现有），共享 ≥2 词
  4. **分叉**：
     - **情况A（回声/riff，本例）**：砍 B 头重复词，`B.start = A.end`（`374.54`）
     - **情况B（尾部漂移反向）**：B 起点才是真声学起点、A 尾被错误归因 → 砍 A 尾、保留 `B.start`
     - 判据（不重新听音频的廉价启发）：重复 run 在 B 里短（1–2 词）且 A 完整 → 情况A；重叠窗口很大、B 的「重复」是其真内容开头 → 情况B
  5. **0.44s 闪幕是独立问题**：靠 `generate.py --min-dur` 拉长显示窗口，或加「词跨度相对词数明显失真就回退段级边界」的 sanity；不要和 overlap 修复混为一谈。
- **防误杀**：文本相似**单独出现**（干净间隔 + 重复）时不砍——那是真·表演重复（喜剧 riff）；必须**时间重叠 + 文本重复同时成立**才动手，与现有代码的「多信号 AND」风格一致。
- **待办**：实现 `repair_overlap_echo` stage + A 闪幕 fix + 一条 golden test（把 Oval Office 这段钉死，TDD）。

---

## 跨缺陷的通用经验（铁律）

1. **译文按 index 分批写入必须有跨模态一致性校验**——段数对、索引连续、无空值三项全过 ≠ 对齐。
2. **复用旧译文前先验旧译文本身是否对齐**（v4 复用 v3 错位 → 继承放大）。
3. **改转录参数前先单变量 A/B**，别凭直觉归因（pad 案例直接证伪了「语种误检」假设）。
4. **审计日志 `echo/empty` 不等于真静音**——可能是 prefix collapse 造成的假阴性。
5. **往中段插段必须重建 zh 映射，绝不盲移**（off-by-N 坑）。
6. **默认 `auto` 语种、不强制**；`auto` 失败「不对称可见」（→ja 压英文一眼能看出，→en 压「英文夹日文」会静默藏起），故默认 auto 比强制 en 更安全。

---

## 相关文档

- `AGENTS.md` → `### Hard-won pitfalls`（V7 及更早的坑）+ `## V8–V13 additions`（本文各缺陷的「固化方案」视角）
- `README.md` → `What's new since V6`（V7–V13 特性概览，EN/ZH 双语）
- `docs/specs/16-fill-gaps.md`、`docs/specs/17-verify-align.md`（缺陷 #1–#6 的规格）
- `docs/adr/011-vad-opt-in.md`（VAD 改选开的设计决策）

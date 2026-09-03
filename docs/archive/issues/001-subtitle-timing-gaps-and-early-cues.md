# Issue #001 — 字幕时间轴：cue 之间无空隙 + 部分 cue 提前出现

- **状态**：已定位根因，修复**暂缓**（用户 2026-07-26 要求先记录、暂不改动）
- **影响版本**：`video-translate` v2.0.0
- **触发样本**：`明星模仿秀.mp4`（用户反馈"这次比上次差"）

---

## 1. 现象（用户反馈）

1. **cue 之间无空隙**：字幕一段接一段"墙到墙"紧贴，没有呼吸停顿。
2. **部分 cue 提前出现**：声音还没响，字幕已经显示在画面上。

用户的第一直觉是"是不是没跟着 faster-whisper 的时间走 / 代码动了"。

---

## 2. 结论速览

- 时间轴 **100% 来自 faster-whisper**，代码未改动，`generate` 阶段只是把 `start/end` **原样搬运**进 SRT，**既不补空隙也不做偏移**。
- 这两个现象与 `segment-merge` **无关**（见 §4 证伪）。
- 根因有**两条独立的链路**，分别解释"无空隙"与"提前"，且都源于 **whisper 段级时间戳本身的特性 + 这段音频的说话节奏**，不是偏移 bug，也不是回归。

---

## 3. 证据

### 3.1 生成链路不引入任何时间偏差

`generate.py:33-34` 直接读取段级时间戳：

```python
for i, s in enumerate(segments, 1):
    st, en = s["start"], s["end"]      # 原样搬运，无偏移、无间隙
```

`srt_utils.py:36` 的 `block()` 仅做格式化：

```python
return f"{index}\n{srt_time(start)} --> {srt_time(end)}\n" + "\n".join(lines) + "\n"
```

→ **SRT 时间 = whisper 段级 `start/end` 的精确重现。**

### 3.2 "无空隙"根因：源音频无缝 + whisper 填满每一瞬间

`明星模仿秀.segments_raw.json`（19 段）相邻段 **`end_i == start_{i+1}`（零间隙）**：

```
0.0→3.0, 3.0→4.0, 4.0→6.0, 6.0→7.62, 7.62→8.62, … , 63.34→65.34
```

这是一段快节奏模仿秀对白，说话间几乎没有停顿，whisper 把时间轴填满了。

**对照** `AI女友.segments_raw.json`：说话有自然停顿，whisper 留出了缝隙（例如某处相邻段差 1.75s、某处差 4s），所以那版看着有"呼吸感"。

→ 是否留空隙由**源音频的说话节奏**决定，非生成逻辑问题。

### 3.3 "提前"根因：whisper 段级时间戳天然"头重"

`transcribe.py:106-110` 只取**段级**时间戳，**未开启词级时间戳**：

```python
segs, _info = model.transcribe(
    wav, language=lang, task="transcribe",
    beam_size=BEAM_SIZE, best_of=BEST_OF,
    vad_filter=True, vad_parameters=dict(VAD_PARAMS),   # VAD_PARAMS 见下
)
chunk_segs = [
    {"start": round(s.start + cstart, 2),              # 段级 start
     "end":   round(s.end + cstart, 2),                # 段级 end
     "text":  s.text.strip()}
    for s in segs
]
```

`transcribe.py:27` 的 VAD 参数在段首植入了前导静音：

```python
VAD_PARAMS = {"min_silence_duration_ms": 500, "speech_pad_ms": 200}
```

**机理**：whisper 段级 `start` 通常包含一段前导静音（此处 VAD 再 pad 200ms），即 cue 在 `start` 那一刻弹出，但真正的第一个词要晚 0.1–0.5s 才发声。在**密集短 cue**场景下，每段都"快半拍"，累积成明显的"字幕比声音先到"观感。

---

## 4. 与 `segment-merge` 无关（证伪）

`merge.py:3` 不变量明确：

> `merged.start = first segment's start; merged.end = last segment's end.` Timestamps are never recomputed.

且对 `明星模仿秀.mp4`：

- `明星模仿秀.segments_en.json` 与 `明星模仿秀.segments_raw.json` **逐字节相同** → merge 阶段是**空操作**，用户看到的就是 whisper 原生输出。

merge 只在"句子不以 `.!?` 结尾"时把碎片拼成长句（如 `AI女友` 触发了 merge）。即便 merge 触发，也只是平移 `start/end`，不会凭空制造空隙或提前——它解释的是"成句/偏长"，而非本 issue 的两个现象。

---

## 5. candidate 修复方案

| 方案 | 解决的现象 | 代价 / 风险 | 评价 |
|---|---|---|---|
| **A. 词级时间戳** | "提前"（根治） | `transcribe` 开 `word_timestamps=True`，segments 需存 `words`；为 v3 按长度断句铺路 | **推荐**，精度最高，同为 v3 计划内工作 |
| **B. `generate` 加最小间隔 `GAP`** | "无空隙"（兜底） | `cue_i.end = min(end_i, start_{i+1} - GAP)`（如 0.2s），切掉的多为尾随静音，安全 | 安全兜底，可与 A 并用 |
| **C. `start` 加固定偏移** | "提前"（粗犷） | 全段 `start + 0.25s`，可能引入新缝隙/重叠，治标不治本 | **不推荐**作主方案 |

---

## 6. 建议落地（尚未执行）

1. `transcribe.py`：`model.transcribe(..., word_timestamps=True)`，每个 segment 存 `words`（含每词 `start/end`）。
2. `generate.py`：用「首词 `start` / 末词 `end`」替代段级 `start/end`，削掉前导/尾随静音，字幕精确卡在真实发声点。
3. `generate.py`：新增 `--gap`（默认 0.2s）参数，相邻 cue 强制留最小间隔（方案 B 兜底）。

> 以上均不违反既有"时间戳不变量"精神：词级时间同样是声学事实，只是更精细；`--gap` 仅裁掉尾随静音，不重算语义边界。

---

## 7. 复现 / 验证命令

```bash
# 1. 确认 generate 是否原样搬运（diff raw 与 en 段）
#    若字节相同 → merge 空操作；差异 → merge 触发
cmp 明星模仿秀.segments_raw.json 明星模仿秀.segments_en.json

# 2. 查看相邻段间隙（零间隙即"无空隙"现象来源）
python - <<'PY'
import json
segs = json.load(open("明星模仿秀.segments_raw.json"))
for a, b in zip(segs, segs[1:]):
    print(f"{a['start']:>7}→{a['end']:<7} gap={b['start']-a['end']:+.2f}")
PY

# 3. 对照样本（应有自然间隙）
cmp video-translate/videos/AI女友.segments_raw.json video-translate/videos/AI女友.segments_en.json
```

---

## 8. 决策记录

- 2026-07-26：用户要求**先记录问题、暂不修改**。本 issue 文档为当前唯一产物。
- 后续若启动修复，按 §6 落地，并补 golden 用例（含"密集短 cue + 无间隙"样本）防回归。

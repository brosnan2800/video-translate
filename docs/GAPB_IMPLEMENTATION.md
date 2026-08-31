# GAPB_IMPLEMENTATION.md — Phase 4 半自动硬闸门实现手册（给 coding agent / CodeBuddy）

> **目标读者**：负责改 `video-translate` 仓库代码的 coding agent（Claude Code / CodeBuddy / Cline）。
> **任务**：把 Stage 4 `verify` 从"报告型 + 默认放行"升级成"分流 + 自动恢复 + 熔断 + 半自动"的硬闸门。
> **来源设计**：`SOLUTION.md` §7.4、`PIPELINE.md` R10 + Stage 4。
> 本文件是**可以直接照做的施工单**，不是讨论。所有文件路径、函数名、常量、退出码、JSON schema 都已给定。

---

## 0. 两条不可违背的约束（你纠偏的原话，已转成硬规则）

1. **熔断（约束一）**：自动恢复环路**必须带上限**。`retry ≥ MAX_RETRY`（=2）或语义 fidelity 命中 → **立即停自动环、转半自动**。绝不无限循环。
2. **分流 + 语义回读硬闭环（约束二）**：
   - 声学 / 确定性内容命中 → 自动修该段 → 重 verify → `retry++`。
   - 语义回读 fidelity 命中 → **不进自动环**（修了可能更差），直接转半自动，不消耗 retry。
   - 语义回读是**半独立 agent 子任务**，必须有硬代码闭环：产 `semantic_reread_task.json` → agent 回读 → 产 `semantic_reread_result.json` → **`verify_gate.py` 必须消费这个 result**，命中即退半自动。现状"产了就完了、不检查、不退码"正是缺这最后一步。

---

## 1. 新增文件：`gates/verify_gate.py`

这是 Gap B 的核心。职责：消费 `verify` 三 Lane 结果 + 语义回读 result，写机器可读 gate 状态，按分流决定退码与是否自动重跑。

### 1.1 常量

```python
MAX_RETRY = 2                 # 熔断上限（约束一）
GATE_FILE = "{base}.vt_verify_gate.json"
RERead_TASK = "{base}.semantic_reread_task.json"
RERead_RESULT = "{base}.semantic_reread_result.json"
REPORT_FILE = "{base}.verify_report.json"   # verify 三 Lane 的原始产出
EXIT_OK = 0
EXIT_GATE_VIOLATION = 3        # breached 且未审批（半自动落点）
```

### 1.2 数据 schema：`.vt_verify_gate.json`

```json
{
  "base": "x",
  "breached": true,
  "mode": "half_auto",          // "auto" 进行中 / "half_auto" 已熔断转人工
  "lane": "semantic",           // "acoustic" | "content" | "semantic" | "clean"
  "auto_fixable": false,        // 声学/确定性内容=true；语义=false
  "retry": 0,
  "max_retry": 2,
  "breaker_tripped": true,
  "semantic_reread_result": "videos/x.semantic_reread_result.json",
  "report": "videos/x.verify_report.json",
  "ts": "2026-08-31T14:00:00"
}
```

### 1.3 主流程（伪代码，照此实现为 `main()`）

```python
def main(base, video):
    retry = 0
    while True:
        # ① 跑真实仓库 verify，产出三 Lane 报告
        run_repo_verify(base, video)                 # uv run video-translate verify ...
        report = load_json(REPORT_FILE.format(base=base))
        breaches = classify(report)                 # 见 1.4

        if not breaches:
            write_gate(base, breached=False, lane="clean",
                       mode="auto", auto_fixable=False, retry=retry,
                       breaker_tripped=False)
            return EXIT_OK                            # 全绿，可交付

        # ② 语义 fidelity 命中 → 直接半自动（不消耗 retry）
        semantic = [b for b in breaches if b["lane"] == "semantic"]
        if semantic:
            write_gate(base, breached=True, lane="semantic",
                       mode="half_auto", auto_fixable=False, retry=retry,
                       breaker_tripped=True)
            return EXIT_GATE_VIOLATION               # 等你审，不退 6

        # ③ 只剩声学/确定性内容（可自动修）
        if retry >= MAX_RETRY:
            write_gate(base, breached=True, lane=breaches[0]["lane"],
                       mode="half_auto", auto_fixable=True, retry=retry,
                       breaker_tripped=True)
            return EXIT_GATE_VIOLATION               # 熔断转半自动（约束一）

        auto_fix(base, video, breaches)              # 见 1.5
        retry += 1
        # 回到 while 顶部重 verify
```

### 1.4 `classify(report)` — 三 Lane 分流

返回 breach 列表，每个 `{lane, auto_fixable, windows?}`：
- `lane="acoustic"`：`report.acoustic.uncovered_audio` 非空 → `auto_fixable=True`，附带 `windows`（有声无字幕区间，来自 `find_uncovered_speech`）。
- `lane="content"`：`report.content.coverage_drift` / `index_drift` / `untranslated_latin` 非空 → `auto_fixable=True`，附带问题段 index 列表。
- `lane="semantic"`：`report.content.semantic_reread.fidelity < 阈值`（且 `RERead_RESULT` 存在且判定不通过）→ `auto_fixable=False`。

> 判据直接复用真实仓库 `verify.py` 已有函数：`find_uncovered_speech`、`validate_zh`、`verify_align`、`find_untranslated_latin_words`、`build_semantic_reread_task`。**不要重写判据，只消费它们的产出。**

### 1.5 `auto_fix(base, video, breaches)` — 自动补该段

```python
def auto_fix(base, video, breaches):
    for b in breaches:
        if b["lane"] == "acoustic":
            # 只对 uncovered-audio 区间重翻 + 重生成，不动全片
            windows = b["windows"]                    # "12.0-18.5;40.1-44.0"
            run_repo_resegment(base, video, windows)  # resegment --windows ...
            run_repo_generate(base)
        elif b["lane"] == "content":
            # 只对问题 index 重译 + 重生成（headless google 引擎）
            run_repo_translate(base, video, indices=b["indices"])
            run_repo_generate(base)
```

> 关键：自动修复**只针对问题区间/段**，不重跑全片。这正是"自动环路省时"的来源。
> 注：真实仓库 `run` 不支持 `--windows`/`--indices` 部分重译，故声学窗口用
> `resegment --windows`（本地已支持，复用 cached vocals.wav），内容缺失用
> `translate --engine google` 重译整份 zh 后再 generate。

---

## 2. 改 `gates/checkpoint.py`：`verify` 标 `GATED`

- 在 `GATED` 集合里加入 `"verify"`（与 `translate` 同列）。
- `complete <stage>` 逻辑：当 `stage == "verify"` 时，先读 `.vt_verify_gate.json`：
  - `breached == False` → 允许标记 completed。
  - `breached == True` 且 `.vt_checkpoint` 中 `verify` 已有 `human_approved` → 允许（半自动放行）。
  - 否则 → `GATE VIOLATION`，**退出码 3**，不许标记完成。
- 退出码保持：0 OK / 2 PREREQUISITE / 3 GATE / 4 SCHEMA·STALE / 6 AWAITING_AGENT。

---

## 3. 改 `Makefile`：单次 verify + 自动恢复环 + 半自动放行

```makefile
# 旧：finish 里假想 verify 自循环（去掉）
# 新：
finish:
	$(PY) gates/preflight_decision.py assert --base "$(BASE)" --video "$(VIDEO)"
	$(PY) gates/checkpoint.py complete transcribe --base "$(BASE)"
	# <- Stage 2 翻译产出 zh_segments.json（人工/agent）
	$(PY) gates/gate.py content --base "$(BASE)"
	$(PY) video-translate generate ...
	$(PY) gates/verify_gate.py run --base "$(BASE)" --video "$(VIDEO)"   # 单次，写 gate
	$(PY) gates/checkpoint.py complete verify --base "$(BASE)"            # 读 gate，挡则退 3

verify-fix:                                                     # 自动恢复环（带上限）
	$(PY) gates/verify_gate.py run --base "$(BASE)" --video "$(VIDEO)" --auto-loop

verify-approve:                                                 # 半自动放行（人工确认）
	$(PY) gates/checkpoint.py approve verify --base "$(BASE)"
```

> `verify_gate.py run --auto-loop` 即第 1.3 节的 while 循环；不带 `--auto-loop` 时只跑一次单次校验（供 `finish` 调用，环交给 `verify-fix`）。两种模式共用 `classify` / `auto_fix` / `write_gate`。

---

## 4. 改真实仓库 `src/video_translate/verify.py`（三刀）

1. **默认阻断**：去掉"非 `--strict` 红线命中退 0"的默认放行。红线命中一律非零退出（沿用 `EXIT_RUNTIME`），除非显式 `--report-only`。
2. **自动触发**：`generate` 子命令末尾自动调用 `verify`（或 `verify_gate.py run`），让"generate 完不 verify"的漏步从静默埋雷变大声失败。
3. **消费语义回读结果**：`verify` / `verify_gate` 必须读取 `<base>.semantic_reread_result.json` 并据此判定 fidelity，而不是"产 `semantic_reread_task.json` 就结束"。若 result 文件不存在，视为 breached（强制人工补回读），不得静默跳过。

---

## 5. 语义回读子任务接线（半独立 agent 任务）

| 步骤 | 产出 | 谁做 |
|---|---|---|
| `verify.py::build_semantic_reread_task()` | `<base>.semantic_reread_task.json` | CLI（确定性，无 LLM，遵守 ADR-005） |
| 你的翻译 agent 读 task → 回读 → 判 fidelity | `<base>.semantic_reread_result.json` | 有 LLM 的 agent |
| `verify_gate.py` 消费 result | gate 状态 / 退码 | CLI（确定性） |

> 若 agent 未回读（result 缺失），gate 必须判 `breached=true, lane=semantic`，转半自动，**不得静默放行**。

---

## 6. 4 项 smoke test（实现后必跑，全部要过）

```bash
# 准备：造一份假的 verify_report.json 分别触发不同 Lane
PY=uv run python

# T1 uncovered-audio 自动修，retry=1 转绿 → 期望退 0，且只补问题区间
echo '{"acoustic":{"uncovered_audio":[{"start":12.0,"end":18.5}]},"content":{},"presentation":{}}' > videos/x.verify_report.json
$PY gates/verify_gate.py run --base x --video videos/x.mp4 --auto-loop; echo "exit=$? (期望 0)"
$PY -c "import json;d=json.load(open('videos/x.vt_verify_gate.json'));print(d['retry'],d['breached'])"   # 期望 1 False

# T2 语义命中立即半自动，retry=0 → 期望退 3，不进自动环
echo '{"acoustic":{},"content":{"semantic_reread":{"fidelity":0.4}},"presentation":{}}' > videos/x.verify_report.json
touch videos/x.semantic_reread_result.json
$PY gates/verify_gate.py run --base x --video videos/x.mp4 --auto-loop; echo "exit=$? (期望 3)"
$PY -c "import json;d=json.load(open('videos/x.vt_verify_gate.json'));print(d['lane'],d['retry'],d['mode'])"  # 期望 semantic 0 half_auto

# T3 声学持续超限，retry=2 熔断 → 期望退 3，breaker_tripped=True
echo '{"acoustic":{"uncovered_audio":[{"start":12.0,"end":18.5}]},"content":{},"presentation":{}}' > videos/x.verify_report.json
# 模拟 auto_fix 永远修不好：让 classify 恒定返回 acoustic
$PY gates/verify_gate.py run --base x --video videos/x.mp4 --auto-loop --simulate-unfixable; echo "exit=$? (期望 3)"
$PY -c "import json;d=json.load(open('videos/x.vt_verify_gate.json'));print(d['retry'],d['breaker_tripped'],d['mode'])"  # 期望 2 True half_auto

# T4 全绿 → 退 0，可交付
echo '{"acoustic":{},"content":{},"presentation":{}}' > videos/x.verify_report.json
$PY gates/verify_gate.py run --base x --video videos/x.mp4; echo "exit=$? (期望 0)"
```

> T3 的 `--simulate-unfixable` 仅测试用，让 `classify` 恒定返回 acoustic breach，验证熔断上限生效；生产代码里删掉这个开关。

---

## 7. 与既有成品的关系（不要改坏它们）

- `gates/checkpoint.py`：只新增 `verify` 到 `GATED`，不改既有 `translate` 逻辑。
- `Makefile`：只新增 `verify-fix` / `verify-approve`，`finish` 的 verify 步由"假想自循环"改为"单次 + 写 gate"。
- `preflight_decision.py` / `advisors/`：Phase 0→1 签字闸门，**完全不动**。Gap B 是末道（Phase 4），与首道（Phase 0）独立。
- `translate/strategy_registry.py`：Phase 2 规则外置，不动。

**改动只增不删、只加文件不碰旧逻辑**（开闭原则）。新增 `verify_gate.py`；改 `checkpoint.py` / `Makefile` / `verify.py` 三处各只补一小段。

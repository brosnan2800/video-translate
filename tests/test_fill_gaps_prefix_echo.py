"""ADR-036b: big look-back pad re-emits the PREVIOUS line as a tail echo.

事故（Walken `10:02-10:09`，真实音频）：ADR-036 把 `_PROBE_PADS` 扩到
`0.2/0.0/0.5/2.0/4.0/6.0` 以回溯到前句开头避免 prefix collapse；但 whisper 不
collapse 时，大 pad 把整句前句拖进解码窗，作为"恢复段"重发，重叠邻居 1.4-2.0s
并重述其尾巴（+"延续句"）。ADR-036 D6 把信号 C 放宽为只拦短段，信号 A 只拦
`<=4` 词重叠 —— 这些**长尾句回声**因此漏网，落进字幕成为"前句后半 == 后句整句"
的重复。

实测 5 例（均 `recovered` 且 `maxOverlap` 1.40-2.00s，文本均重述邻居尾巴）：
  [91]  280.55-284.49 ov=1.53 重述 [92] "something you're gonna have to take with you to..."
  [180] 531.32-537.10 ov=1.58 重述 [183] "Is there any dessert or what are we going to do now?"
  [206] 602.04-609.08 ov=2.00 重述 [205] "and the jelly would go into the donut and it was very"
  [258] 738.04-746.58 ov=1.45 重述 [257] "basically there's no"
  [305] 893.06-897.88 ov=1.40 重述 [304] "And let's just say the encounter didn't disappoint."

判别器（真实数据坐实）：真实恢复段重叠邻居 `<=0.50s`（Walken 821-837s 恢复
0.00s），回声段 `>=1.40s`。`_ECHO_OVERLAP=1.0` 卡在两者之间，零误杀。本测试
用上述 5 例几何数据复现，断言信号 E 拦截；并用 `<=0.50s` 真实重叠断言不误杀。
"""
import sys

from video_translate import fill_gaps as F


def _mk_rec(start, end, text, nsp=0.1, alp=-0.3, nwords=None):
    words = [{"word": f"w{i}", "start": start + i * 0.1,
              "end": start + i * 0.1 + 0.05}
             for i in range(nwords or max(1, len(text.split())))]
    return {"start": round(start, 2), "end": round(end, 2), "text": text,
            "words": words, "no_speech_prob": nsp, "avg_logprob": alp,
            "_recovered": True}


# (cand, neighbour) 几何取自 Walken 事故 5 例
ACCIDENT_CASES = [
    # [206] 602.04-609.08 重述 [205] 601.61-604.04 (ov=2.00)
    (_mk_rec(602.04, 609.08,
             "would go into the doughnut and it was very exciting and while some skits age"),
     {"start": 601.61, "end": 604.04,
      "text": "and the jelly would go into the donut and it was very"}),
    # [91] 280.55-284.49 重述 [92] (ov=1.53)
    (_mk_rec(280.55, 284.49,
             "but you'd be killing an innocent man that's something you're gonna have to take with you to"),
     {"start": 283.0, "end": 286.0,
      "text": "something you're gonna have to take with you to you please"}),
    # [180] 531.32-537.10 重述 [183] (ov=1.58)
    (_mk_rec(531.32, 537.10,
             "just guide it onto there patient is ready is there any dessert or what are we gonna do now"),
     {"start": 535.5, "end": 539.0,
      "text": "Is there any dessert or what are we going to do now?"}),
    # [258] 738.04-746.58 重述 [257] (ov=1.45)
    (_mk_rec(738.04, 746.58,
             "See, there's no punctuation, anything that comes out of my mouth."),
     {"start": 744.6, "end": 747.0, "text": "basically there's no"}),
    # [305] 893.06-897.88 重述 [304] (ov=1.40)
    (_mk_rec(893.06, 897.88,
             "encounter didn't disappoint. I met Christopher Walken on the set of Suicide Kings. I go to say"),
     {"start": 894.48, "end": 899.0,
      "text": "And let's just say the encounter didn't disappoint."}),
]


def test_echo_overlap_signal_blocks_all_five_accident_cases():
    """ADR-036b 信号 E：重叠 >1.0s 的尾句回声全部判为幻觉（即使长段、nsp 低）。"""
    for cand, neigh in ACCIDENT_CASES:
        segments = [neigh]
        assert F._is_recovered_hallucination(
            cand, segments, check_overlap=True), (
            f"should drop tail-echo {cand['start']}-{cand['end']}: "
            f"{cand['text']!r} (overlap with neighbour "
            f"{min(cand['end'], neigh['end']) - max(cand['start'], neigh['start']):.2f}s)")


def test_echo_overlap_signal_off_on_collapse_path():
    """collapse / G1 / G2 路径 check_overlap=False，信号 E 不误杀（窗口本就重叠父段）。"""
    for cand, neigh in ACCIDENT_CASES:
        segments = [neigh]
        assert not F._is_recovered_hallucination(
            cand, segments, check_overlap=False), (
            "collapse/G1/G2 path must NOT drop by overlap signal E")


def test_genuine_small_overlap_not_dropped():
    """真实恢复段（重叠 <=0.50s）不得被信号 E 误杀（Walken 真实上限 0.50s）。"""
    # 仿 Walken [217] 531.32? no — 用 [217] 738? 这里造 0.50s 重叠的真实长段
    cand = _mk_rec(100.0, 108.0,
                   "this is genuinely new speech recovered from the hole and it is long enough",
                   nsp=0.1)
    neigh = {"start": 107.5, "end": 112.0,
             "text": "the previous line ends here and partial overlap is just a boundary"}
    segments = [neigh]
    assert not F._is_recovered_hallucination(cand, segments, check_overlap=True)
    # 边界：恰好 1.00s（阈值）不应触发；需严格 > _ECHO_OVERLAP
    cand2 = _mk_rec(100.0, 108.0,
                    "another genuine long recovery that merely starts one second early",
                    nsp=0.1)
    neigh2 = {"start": 107.0, "end": 112.0, "text": "previous line here"}
    assert not F._is_recovered_hallucination(cand2, [neigh2], check_overlap=True)


# ---------------------------------------------------------------------------
# 集成测试：mock Whisper，复现 [206] 的"大 pad 重发前句尾巴"场景
# ---------------------------------------------------------------------------

def _mk_word(word, s, e):
    return type("W", (), {"word": word, "start": s, "end": e})


def _mk_seg(text, s, e, words):
    return type("Seg", (), {
        "text": text, "start": s, "end": e, "words": words,
        "avg_logprob": -0.3, "no_speech_prob": 0.1, "compression_ratio": 1.0,
    })


def test_hole_probe_drops_previous_line_tail_echo(monkeypatch):
    """洞 604.04-609.98：大 pad 解码重发前句 [205] 尾巴+延续句，必须被信号 E 丢弃。"""
    captured = {"ss": None}

    def fake_extract_chunk(src, wav, ss, dur):
        captured["ss"] = ss

    monkeypatch.setattr(F, "extract_chunk", fake_extract_chunk)
    monkeypatch.setattr(F, "probe_duration", lambda p: 700.0)
    monkeypatch.setattr(F, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            ss = captured["ss"]
            # 真实 whisper 行为：大 pad（2/4/6）把前句整句拖进窗，重发前句尾巴；
            # 小 pad（0.2/0.0/0.5）只掏洞内干净对白；尾洞等无语音区解码为空。
            # 仅当解码窗实际重覆 [205] (601.61-604.04) 时才吐"前句尾巴回声"。
            ov205 = min(ss + 7.04, 604.04) - max(ss, 601.61)
            if ov205 > 0.5:
                # 大 pad：重发前句尾巴 + 延续句，绝对起点 ss，重叠前句 >1s
                return [
                    _mk_seg(
                        "would go into the doughnut and it was very exciting and while some skits age",
                        0.0, 7.04,
                        [_mk_word("would", 0.0, 0.5),
                         _mk_word("go", 0.5, 1.0),
                         _mk_word("into", 1.0, 1.5),
                         _mk_word("the", 1.5, 2.0),
                         _mk_word("doughnut", 2.0, 2.5),
                         _mk_word("and", 2.5, 3.0),
                         _mk_word("it", 3.0, 3.5),
                         _mk_word("was", 3.5, 4.0),
                         _mk_word("very", 4.0, 4.5),
                         _mk_word("exciting", 4.5, 5.0),
                         _mk_word("and", 5.0, 5.5),
                         _mk_word("while", 5.5, 6.0),
                         _mk_word("some", 6.0, 6.5),
                         _mk_word("skits", 6.5, 6.8),
                         _mk_word("age", 6.8, 7.04)]),
                ], None
            if 595.0 <= ss <= 605.0:
                # 小 pad 洞内恢复：绝对 [604.04, 609.98]，不重叠前句
                rel_s = round(604.04 - ss, 2)
                rel_e = round(609.98 - ss, 2)
                return [
                    _mk_seg(
                        "exciting and while some skits age",
                        rel_s, rel_e,
                        [_mk_word("exciting", rel_s, rel_s + 0.5),
                         _mk_word("and", rel_s + 0.5, rel_s + 1.0),
                         _mk_word("while", rel_s + 1.0, rel_s + 1.5),
                         _mk_word("some", rel_s + 1.5, rel_s + 2.0),
                         _mk_word("skits", rel_s + 2.0, rel_s + 2.3),
                         _mk_word("age", rel_s + 2.3, rel_e)]),
                ], None
            # 尾洞 / 其它无语音区：解码为空（不凭空造回声）
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    segments = [
        {"start": 601.61, "end": 604.04,
         "text": "and the jelly would go into the donut and it was very"},
        {"start": 609.98, "end": 611.76,
         "text": "this one turned into his personal nightmare"},
    ]
    out = F.fill_gaps("vid.mp4", segments, silence_intervals=[],
                      review=False, g1=False, g2=False, g3=False,
                      progress=lambda *_: None)
    recovered = [s for s in out if s.get("_recovered")]
    # 信号 E 必须把尾句回声丢掉 -> 不插入任何 recovered 段（洞留空，符合"不重发前句"）
    assert not any("doughnut" in (s.get("text") or "")
                   for s in recovered), f"tail-echo leaked: {recovered}"
    # 原段未被改动（声学时间戳保留）
    assert any(abs(float(s["start"]) - 601.61) < 1e-6
               and "jelly" in (s.get("text") or "") for s in out)

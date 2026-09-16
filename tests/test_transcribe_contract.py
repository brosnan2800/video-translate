"""Contract tests for transcribe planning/merge/resume (Spec 02, ADR-002).

No model or ffmpeg needed: plan_chunks and merge_chunks are pure, and resume is
exercised by pre-seeding chunk_N.json so the model is never loaded.
"""
import os

import pytest

from video_translate import transcribe as T
from video_translate.io_utils import save_json


def test_plan_chunks_scheme():
    # n = int(total // chunk) + 1
    plan = T.plan_chunks(500.0, 240.0)
    assert [ci for ci, _, _ in plan] == [0, 1, 2]
    assert plan[0] == (0, 0.0, 240.0)
    assert plan[1] == (1, 240.0, 240.0)
    # last chunk clamps to remaining duration
    assert plan[2][0] == 2 and abs(plan[2][2] - 20.0) < 1e-9


def test_plan_chunks_exact_multiple_has_trailing_chunk():
    plan = T.plan_chunks(480.0, 240.0)
    # int(480//240)+1 = 3, but 3rd has zero duration -> dropped
    assert [ci for ci, _, _ in plan] == [0, 1]


def test_plan_chunks_zero_or_negative():
    assert T.plan_chunks(0, 240.0) == []
    assert T.plan_chunks(-3, 240.0) == []


def test_merge_chunks_preserves_order():
    a = [{"start": 0, "end": 1, "text": "a"}]
    b = [{"start": 1, "end": 2, "text": "b"}]
    assert T.merge_chunks([a, b]) == a + b


def test_forced_constants():
    # V5 (ADR-014): device is no longer a hard-coded constant; it defaults to
    # "auto" and resolves to cpu/int8 on a CUDA-free machine.
    assert T.DEFAULT_DEVICE == "auto"
    assert T.DEFAULT_COMPUTE_TYPE == "auto"
    assert T.resolve_device("cpu", "int8") == ("cpu", "int8")
    # V4 quality pass: beam search (not greedy) + no cross-segment conditioning
    assert T.BEAM_SIZE == 5 and T.BEST_OF == 5
    assert T.CONDITION_ON_PREVIOUS_TEXT is False
    assert T.REPETITION_PENALTY > 1.0
    # V6 (B2): recall-biased VAD so quiet / music-underscored lines still decode
    assert T.VAD_THRESHOLD < 0.5
    assert T.VAD_PARAMS["threshold"] == T.VAD_THRESHOLD


def test_resolve_device_explicit_override():
    """Explicit device/compute_type are honoured verbatim (no auto-probing)."""
    assert T.resolve_device("cuda", "float16") == ("cuda", "float16")
    assert T.resolve_device("cpu", None) == ("cpu", "int8")


def test_resolve_device_auto_cpu_fallback(monkeypatch):
    """On a CUDA-free machine, auto resolves to the historical cpu/int8."""
    monkeypatch.setattr(T, "_cuda_available", lambda: False)
    assert T.resolve_device("auto", "auto") == ("cpu", "int8")
    assert T.resolve_device(None, None) == ("cpu", "int8")


def test_vad_threshold_override_changes_fingerprint():
    """A different VAD threshold must invalidate the chunk cache — otherwise a
    re-run with --vad-threshold would silently reuse the old transcription.

    The threshold only affects output when VAD is ON (use_vad=True), which is
    the real cache-invalidation scenario (ADR-011). When VAD is off the
    threshold is irrelevant, so the fingerprint stays identical.
    """
    base_fp = T.transcribe_fingerprint("large-v3", 240.0, None,
                                       T.build_vad_params(), use_vad=True)
    tuned_fp = T.transcribe_fingerprint("large-v3", 240.0, None,
                                        T.build_vad_params(0.2), use_vad=True)
    assert base_fp != tuned_fp
    # VAD off: threshold irrelevant -> identical fingerprint regardless of value
    off_a = T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params())
    off_b = T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params(0.2))
    assert off_a == off_b


def test_build_vad_params_does_not_mutate_module_default():
    T.build_vad_params(0.9)
    assert T.VAD_PARAMS["threshold"] == T.VAD_THRESHOLD


def _seed_chunk(outdir: str, base: str, ci: int, payload) -> None:
    """Seed a chunk cache at the fingerprinted path the runner will look up."""
    fp = T.transcribe_fingerprint("large-v3", 240.0, None)
    save_json(os.path.join(outdir, base, f"{base}.{fp}.chunk_{ci}.json"),
              payload, indent=0)


def test_resume_skips_completed_chunks_without_model(tmp_path, monkeypatch):
    """If every chunk_N.json exists, transcribe_video must not import a model."""
    outdir = str(tmp_path)
    # total 300s, chunk 240s -> chunks 0,1 (durations 240, 60)
    monkeypatch.setattr(T, "probe_duration", lambda p: 300.0)

    def _boom(*a, **k):  # extract must never be called on full resume
        raise AssertionError("extract_chunk called during full resume")

    monkeypatch.setattr(T, "extract_chunk", _boom)

    _seed_chunk(outdir, "apollo_story", 0, [{"start": 0, "end": 1, "text": "hello"}])
    _seed_chunk(outdir, "apollo_story", 1, [{"start": 240, "end": 241, "text": "world"}])

    out = T.transcribe_video("dummy.mp4", outdir, base="apollo_story",
                             align_backend="none",
                             progress=lambda *_: None)
    merged = __import__("json").load(open(out, encoding="utf-8"))
    assert [s["text"] for s in merged] == ["hello", "world"]


# --- V3: word-level timestamps ---


def test_transcribe_stores_words(tmp_path, monkeypatch):
    """V3: transcribe must request word_timestamps=True and carry words into
    the emitted segments (rounded to 2dp, offset by chunk start)."""
    import json as _json
    import sys

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    captured = {}

    class FakeWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class FakeSeg:
        def __init__(self):
            self.text = "Hello world"
            self.start = 1.0
            self.end = 2.0
            self.words = [FakeWord("Hello", 1.05, 1.4), FakeWord("world", 1.5, 1.95)]

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["word_timestamps"] = kw.get("word_timestamps")
            return [FakeSeg()], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang=None,
                              align_backend="none",
                              progress=lambda *_: None)
    merged = _json.load(open(out, encoding="utf-8"))
    assert captured["word_timestamps"] is True
    assert merged[0]["words"] == [
        {"word": "Hello", "start": 1.05, "end": 1.4},
        {"word": "world", "start": 1.5, "end": 1.95},
    ]


def test_transcribe_carries_confidence_fields(tmp_path, monkeypatch):
    """V5 / ADR-020: transcribe must carry Whisper's per-segment confidence
    fields (avg_logprob / no_speech_prob / compression_ratio) into the emitted
    segments so the hallucination filter can use them without re-decoding."""
    import json as _json
    import sys

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    class FakeWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class FakeSeg:
        def __init__(self):
            self.text = "Hello world"
            self.start = 1.0
            self.end = 2.0
            self.words = [FakeWord("Hello", 1.05, 1.4), FakeWord("world", 1.5, 1.95)]
            self.avg_logprob = -0.42
            self.no_speech_prob = 0.03
            self.compression_ratio = 1.1

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            return [FakeSeg()], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang=None,
                             align_backend="none",
                             progress=lambda *_: None)
    merged = _json.load(open(out, encoding="utf-8"))[0]
    assert merged["avg_logprob"] == -0.42
    assert merged["no_speech_prob"] == 0.03
    assert merged["compression_ratio"] == 1.1



def test_transcribe_resume_keeps_words(tmp_path, monkeypatch):
    """On full resume, pre-seeded chunk JSON with words must survive merge."""
    import json as _json

    monkeypatch.setattr(T, "probe_duration", lambda p: 300.0)

    def _boom(*a, **k):
        raise AssertionError("extract_chunk called during full resume")

    monkeypatch.setattr(T, "extract_chunk", _boom)
    _seed_chunk(str(tmp_path), "apollo_story", 0,
                [{"start": 0, "end": 1, "text": "hi",
                  "words": [{"word": "hi", "start": 0.1, "end": 0.9}]}])
    # total 300s, chunk 240s -> chunks 0 and 1; seed BOTH so the run is a full resume
    _seed_chunk(str(tmp_path), "apollo_story", 1,
                [{"start": 240, "end": 241, "text": "ya",
                  "words": [{"word": "ya", "start": 240.1, "end": 240.9}]}])

    out = T.transcribe_video("dummy.mp4", str(tmp_path), base="apollo_story",
                              align_backend="none",
                              progress=lambda *_: None)
    merged = _json.load(open(out, encoding="utf-8"))
    assert merged[0]["words"][0]["word"] == "hi"


# --- V2 ---


def test_transcribe_base_defaults_to_input_stem(tmp_path, monkeypatch):
    """base=None -> derived from input filename stem."""
    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    _seed_chunk(str(tmp_path), "myvideo", 0, [{"start": 0, "end": 1, "text": "hi"}])

    def _boom(*a, **k):
        raise AssertionError("extract_chunk should not run on full resume")

    monkeypatch.setattr(T, "extract_chunk", _boom)
    out = T.transcribe_video("/path/to/myvideo.mp4", str(tmp_path),
                             align_backend="none",
                             progress=lambda *_: None)
    assert out.endswith("myvideo.segments_en.json")


def test_transcribe_lang_none_passes_language_none(tmp_path, monkeypatch):
    """lang=None -> Whisper transcribe receives language=None (auto-detect)."""
    import sys

    captured = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["language"] = language
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang=None,
                       align_backend="none",
                       progress=lambda *_: None)
    assert captured["language"] is None


def test_transcribe_lang_en_passed_through(tmp_path, monkeypatch):
    """lang='en' -> Whisper transcribe receives language='en'."""
    import sys

    captured = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["language"] = language
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang="en",
                       align_backend="none",
                       progress=lambda *_: None)
    assert captured["language"] == "en"


# --- ADR-015: adaptive per-chunk VAD ---


def test_adaptive_vad_changes_fingerprint_only_when_on():
    """adaptive_vad=True must be a distinct cache family; OFF keeps the
    historical fingerprint unchanged (no golden regression)."""
    off = T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params())
    on = T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params(),
                                  adaptive_vad=True)
    assert off != on
    # explicit False == omitted (historical fingerprint preserved)
    assert T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params(),
                                    adaptive_vad=False) == off


def test_transcribe_video_adaptive_routes_per_chunk(monkeypatch, tmp_path):
    """adaptive_vad=True: each chunk's VAD flag is decided by its local profile,
    not the global use_vad. A silent profile chunk -> VAD on; a noisy chunk -> bare."""
    import json as _json
    import sys

    captured = {"vad_flags": []}

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)

    def _fake_extract(*a, **k):
        return None

    monkeypatch.setattr(T, "extract_chunk", _fake_extract)

    # chunk profile: clean (has silence) -> route_vad_chunk True
    from video_translate.audio_profile import AudioProfile
    monkeypatch.setattr(T, "analyze_audio", lambda wav: AudioProfile(
        mean_vol=-16.0, max_vol=0.0, silence_intervals=[(0.0, 2.0)], ok=True))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["vad_flags"].append(kw.get("vad_filter"))
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang="en",
                       use_vad=False, adaptive_vad=True,
                       align_backend="none",
                       progress=lambda *_: None)
    # single 10s chunk -> one decode, VAD on (clean profile)
    assert captured["vad_flags"] == [True]


def test_transcribe_video_adaptive_bare_on_noise(monkeypatch, tmp_path):
    """A continuous-noise chunk routes to bare (VAD off) even in adaptive mode."""
    import sys

    captured = {"vad_flags": []}

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)
    from video_translate.audio_profile import AudioProfile
    monkeypatch.setattr(T, "analyze_audio", lambda wav: AudioProfile(
        mean_vol=-16.0, max_vol=-3.0, silence_intervals=[], ok=True))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["vad_flags"].append(kw.get("vad_filter"))
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang="en",
                       use_vad=False, adaptive_vad=True,
                       align_backend="none",
                       progress=lambda *_: None)
    assert captured["vad_flags"] == [False]


# --- T4: WhisperX forced alignment (Spec 22, ADR-028) ---


def test_align_none_keeps_transcribe_fingerprint_unchanged():
    """铁律 3 + 决策 2: --align none 的转写指纹与历史哈希一致，golden 零回归。"""
    base_fp = T.transcribe_fingerprint("large-v3", 240.0, None, T.build_vad_params())
    # the fingerprint function has no align parameter at all (decoupled layer)
    assert "none" not in base_fp
    assert "whisperx" not in base_fp


def test_align_auto_resolves_whisperx_when_capable(monkeypatch):
    """auto（T4 默认化）: CUDA + whisperx 可用 -> 解析为 whisperx。"""
    import torch
    import video_translate.align as AL
    monkeypatch.setattr(AL, "whisperx_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert T._resolve_align_backend("auto") == "whisperx"


def test_align_auto_degrades_to_none_without_cuda(monkeypatch, capsys):
    """auto: 无 CUDA -> 提示并回退 none（默认路径静默降级，非告警）。"""
    import torch
    import video_translate.align as AL
    monkeypatch.setattr(AL, "whisperx_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert T._resolve_align_backend("auto") == "none"
    captured = capsys.readouterr()
    assert "auto" in captured.err.lower()


def test_align_auto_degrades_to_none_without_package(monkeypatch):
    """auto: whisperx 未安装 -> 回退 none。"""
    import torch
    import video_translate.align as AL
    monkeypatch.setattr(AL, "whisperx_available", lambda: False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert T._resolve_align_backend("auto") == "none"


def test_align_pass_writes_and_reuses_cache(tmp_path, monkeypatch, capsys):
    """对齐 pass：首次写独立对齐缓存；二次运行命中缓存（断点续跑，不重转写/不对齐）。"""
    import sys
    import video_translate.align as AL

    outdir = str(tmp_path)
    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(T, "analyze_audio", lambda wav: None)

    # whisperx available
    monkeypatch.setattr(AL, "whisperx_available", lambda: True)
    align_calls = {"n": 0}

    def _fake_align(segments, *a, **k):
        align_calls["n"] += 1
        out = []
        for seg in segments:
            nw = [{"word": w["word"], "start": round(w["start"] + 0.1, 2),
                   "end": round(w["end"] + 0.1, 2)} for w in seg["words"]]
            out.append({"text": seg["text"], "words": nw,
                        "start": nw[0]["start"] if nw else seg["start"],
                        "end": nw[-1]["end"] if nw else seg["end"]})
        return {"segments": out}

    fake = type(sys)("whisperx")
    fake.align = _fake_align
    fake.load_align_model = lambda *a, **k: (object(), object())
    fake.load_audio = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(AL, "release_align_memory", lambda *a, **k: None)

    class FakeWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class FakeSeg:
        def __init__(self):
            self.text = "Hi there"
            self.start = 0.0
            self.end = 1.0
            self.words = [FakeWord("Hi", 0.1, 0.4), FakeWord("there", 0.5, 0.95)]
            self.avg_logprob = -0.3
            self.no_speech_prob = 0.01
            self.compression_ratio = 1.0

    transcribe_calls = {"n": 0}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            transcribe_calls["n"] += 1
            info = type("FakeInfo", (), {"language": "en"})()
            return [FakeSeg()], info

    fake_fw = type(sys)("faster_whisper")
    fake_fw.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    params = dict(base="apollo", lang=None, align_backend="whisperx",
                  progress=lambda *_: None)
    # first run: transcribe 1x + align 1x
    T.transcribe_video("vid.mp4", outdir, **params)
    assert transcribe_calls["n"] == 1 and align_calls["n"] == 1

    # alignment cache file exists (uses the independent align fingerprint)
    fp = T.transcribe_fingerprint("large-v3", 240.0, None)
    import video_translate.align as AL

    align_fp = AL.align_fingerprint(fp, "en", backend="whisperx")
    align_cache = os.path.join(outdir, "apollo", f"apollo.{align_fp}.chunk_0.whisperx.json")
    assert os.path.exists(align_cache)

    # second run: full resume (transcribe 0x) + align cache hit (align 0x)
    T.transcribe_video("vid.mp4", outdir, **params)
    assert transcribe_calls["n"] == 1  # unchanged
    assert align_calls["n"] == 1       # unchanged (cache hit)


def test_align_rewrites_word_timestamps_in_merged_output(tmp_path, monkeypatch):
    """对齐后的词戳应进入 segments_en.json（merge.py 按词戳断句，全链路受益）。"""
    import sys
    import video_translate.align as AL

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(T, "analyze_audio", lambda wav: None)
    monkeypatch.setattr(AL, "whisperx_available", lambda: True)

    def _fake_align(segments, *a, **k):
        out = []
        for seg in segments:
            nw = [{"word": w["word"], "start": round(w["start"] + 0.25, 2),
                   "end": round(w["end"] + 0.25, 2)} for w in seg["words"]]
            out.append({"text": seg["text"], "words": nw,
                        "start": nw[0]["start"], "end": nw[-1]["end"]})
        return {"segments": out}

    fake = type(sys)("whisperx")
    fake.align = _fake_align
    fake.load_align_model = lambda *a, **k: (object(), object())
    fake.load_audio = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(AL, "release_align_memory", lambda *a, **k: None)

    class FakeWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class FakeSeg:
        def __init__(self):
            self.text = "Hi there"
            self.start = 0.0
            self.end = 1.0
            self.words = [FakeWord("Hi", 0.1, 0.4), FakeWord("there", 0.5, 0.95)]

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            info = type("FakeInfo", (), {"language": "en"})()
            return [FakeSeg()], info

    fake_fw = type(sys)("faster_whisper")
    fake_fw.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang=None,
                             align_backend="whisperx", progress=lambda *_: None)
    merged = __import__("json").load(open(out, encoding="utf-8"))
    # +0.25 shift applied
    assert merged[0]["words"][0]["start"] == pytest.approx(0.35)
    assert merged[0]["words"][1]["start"] == pytest.approx(0.75)


# --- 转写文本规范化：Whisper 全大写伪影（`_normalize_caps`） ---


def test_normalize_caps_lowercases_shouted_segment():
    """整段全大写 = Whisper 在高能量语音上的伪影 → 小写化 + 恢复句首大写。

    事故几何：`st` 视频后半段（喊叫/高能量）整段输出全大写，读起来像坏字幕。
    """
    assert T._normalize_caps("THIS IS A LOUD SCENE") == "This is a loud scene"
    assert T._normalize_caps("HELLO. WORLD. AGAIN") == "Hello. World. Again"


def test_normalize_caps_leaves_mixed_case_untouched():
    """只有**整段全大写**才算伪影：混合大小写（正常句子 / 含专名）一律不动。"""
    assert T._normalize_caps("NASA announced the mission") == "NASA announced the mission"
    assert T._normalize_caps("Hello world") == "Hello world"
    assert T._normalize_caps("I said STOP it") == "I said STOP it"


def test_normalize_caps_protects_short_tokens():
    """<4 个字母的段受保护：OK / ID / TV / AI 等合法短 token 不被小写化。"""
    assert T._normalize_caps("OK") == "OK"
    assert T._normalize_caps("ID") == "ID"
    assert T._normalize_caps("TV") == "TV"
    assert T._normalize_caps("AI") == "AI"


def test_normalize_caps_known_boundaries():
    """已知边界（显式记录，以免后人误判为 bug）：

    - 短缩写**拼成整段**且合计 >=4 字母时仍会触发（"AI TV" → "Ai tv"）；
    - 单独成段的 >=4 字母全大写专名会被小写化（"NASA" → "Nasa"）；
    - 标点后**无空格**不算句首（"HELLO.WORLD" → "Hello.world"）。

    实践中"整段只含缩写/专名"的字幕极少，权衡（消除常见伪影 > 保护罕见真全大写）
    后保留此行为。
    """
    assert T._normalize_caps("AI TV") == "Ai tv"
    assert T._normalize_caps("NASA") == "Nasa"
    assert T._normalize_caps("HELLO.WORLD") == "Hello.world"


def test_normalize_caps_edge_cases():
    """空值 / 无字母 / 纯标点原样返回（不抛异常、不改动）。"""
    assert T._normalize_caps("") == ""
    assert T._normalize_caps(None) is None
    assert T._normalize_caps("12345 !!") == "12345 !!"


def test_transcribe_video_does_not_normalize_caps(tmp_path, monkeypatch):
    """连接点**不在** `transcribe_video`：它跑在 `apply_merge` / `fill_gaps` 之前，
    那两步随后会重写 segs_path——若把归一化放这里会被覆盖（历史缺陷）。

    归一化落在转写阶段最末（`asr.run_asr`），接线守卫见 `test_asr_facade.py`。
    """
    import json as _json
    import sys

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    class FakeSeg:
        def __init__(self):
            self.text = "THIS IS LOUD"
            self.start = 1.0
            self.end = 2.0
            self.words = []

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            return [FakeSeg()], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang=None,
                             align_backend="none", progress=lambda *_: None)
    merged = _json.load(open(out, encoding="utf-8"))
    # 刻意原样保留：这一层的归一化会被下游 apply_merge / fill_gaps 覆盖。
    assert merged[0]["text"] == "THIS IS LOUD"

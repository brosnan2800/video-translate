"""Tests for the WhisperX alignment pass (Spec 22, ADR-028).

All tests are pure / mocked — no GPU, no whisperx install, no ffmpeg needed.
"""
import json
import os
import sys

import pytest

from video_translate import align as A


# ---------------------------------------------------------------------------
# 1. availability probe
# ---------------------------------------------------------------------------


def test_whisperx_available_true_when_importable(monkeypatch):
    """If `whisperx` imports, the probe reports available."""
    fake = type(sys)("whisperx")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    assert A.whisperx_available() is True


def test_whisperx_available_false_when_missing(monkeypatch):
    """On Mac / not-installed, import must fail -> False (no crash)."""
    # ensure not present
    monkeypatch.setitem(sys.modules, "whisperx", None)
    assert A.whisperx_available() is False


def test_whisperx_available_false_on_mac(monkeypatch):
    """macOS can never run GPU alignment -> probe returns False even if importable."""
    monkeypatch.setattr(A.sys, "platform", "darwin")
    fake = type(sys)("whisperx")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    assert A.whisperx_available() is False


def test_whisperx_available_true_on_linux(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "linux")
    fake = type(sys)("whisperx")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    assert A.whisperx_available() is True


def test_whisperx_available_true_on_windows(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "win32")
    fake = type(sys)("whisperx")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    assert A.whisperx_available() is True


# ---------------------------------------------------------------------------
# 2. fingerprint (independent layer, decoupled from transcribe fingerprint)
# ---------------------------------------------------------------------------


def test_align_fingerprint_stable():
    a = A.align_fingerprint("large-v3.abcdef", "en")
    b = A.align_fingerprint("large-v3.abcdef", "en")
    assert a == b
    assert isinstance(a, str) and len(a) > 0


def test_align_fingerprint_changes_with_backend():
    base = A.align_fingerprint("large-v3.abcdef", "en", backend="whisperx")
    other = A.align_fingerprint("large-v3.abcdef", "en", backend="stable-ts")
    assert base != other


def test_align_fingerprint_changes_with_language():
    en = A.align_fingerprint("large-v3.abcdef", "en")
    zh = A.align_fingerprint("large-v3.abcdef", "zh")
    assert en != zh


def test_align_fingerprint_changes_with_transcribe_fp():
    a = A.align_fingerprint("large-v3.aaaaaa", "en")
    b = A.align_fingerprint("large-v3.bbbbbb", "en")
    assert a != b


# ---------------------------------------------------------------------------
# 3. invariants: align_segments only rewrites word timestamps
# ---------------------------------------------------------------------------


def _fake_whisperx_align(segments, *args, **kwargs):
    """Mirror the real whisperx 3.8 return shape.

    Two keys: a flat `word_segments` list (the authoritative, in-order words) and
    a `segments` list that has been re-split on sentence boundaries — i.e. it no
    longer lines up 1:1 with what we passed in.
    """
    flat: list[dict] = []
    out = []
    for seg in segments:
        new_words = []
        for w in seg["words"]:
            nw = {
                "word": w["word"],
                "start": round(w["start"] + 0.05, 2),
                "end": round(w["end"] + 0.05, 2),
            }
            new_words.append(nw)
            flat.append(dict(nw))
        out.append({
            "text": seg["text"],
            "words": new_words,
            "start": new_words[0]["start"],
            "end": new_words[-1]["end"],
        })
    return {"segments": out, "word_segments": flat}


def test_align_segments_rewrites_only_word_timestamps(monkeypatch, tmp_path):
    """铁律 1: text / count / order / grouping unchanged; only words[].start/end."""
    fake = type(sys)("whisperx")
    fake.align = _fake_whisperx_align
    fake.load_align_model = lambda *a, **k: (object(), object())
    fake.load_audio = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(A, "whisperx_available", lambda: True)
    monkeypatch.setattr(A, "release_align_memory", lambda *a, **k: None)

    segments = [{
        "text": "Hello world",
        "start": 1.0,
        "end": 2.0,
        "words": [
            {"word": "Hello", "start": 1.05, "end": 1.4},
            {"word": "world", "start": 1.5, "end": 1.95},
        ],
    }]
    result = A.align_segments(segments, "dummy.wav", "en",
                              align_backend="whisperx", progress=lambda *a: None)

    assert len(result) == 1
    assert result[0]["text"] == "Hello world"               # text unchanged
    assert len(result[0]["words"]) == 2                     # count unchanged
    assert result[0]["words"][0]["word"] == "Hello"         # order unchanged
    # timestamps shifted +0.05
    assert result[0]["words"][0]["start"] == pytest.approx(1.10)
    assert result[0]["words"][0]["end"] == pytest.approx(1.45)
    assert result[0]["words"][1]["start"] == pytest.approx(1.55)
    assert result[0]["words"][1]["end"] == pytest.approx(2.00)


def test_align_segments_unmatched_word_count_keeps_dtw(monkeypatch, tmp_path,
                                                       capsys):
    """词数不匹配 -> 该段保留 DTW 词戳 + 告警，其余段正常回写（逐段安全回退）。"""

    def _bad_align(segments, *args, **kwargs):
        out = []
        for seg in segments:
            # produce a DIFFERENT word count for the first seg
            words = [{"word": seg["words"][0]["word"], "start": 0.0, "end": 9.9}]
            out.append({"text": seg["text"], "words": words,
                        "start": 0.0, "end": 9.9})
        return {"segments": out}

    fake = type(sys)("whisperx")
    fake.align = _bad_align
    fake.load_align_model = lambda *a, **k: (object(), object())
    fake.load_audio = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(A, "whisperx_available", lambda: True)
    monkeypatch.setattr(A, "release_align_memory", lambda *a, **k: None)

    segs = [
        {"text": "a b c", "start": 0.0, "end": 1.0,
         "words": [{"word": "a", "start": 0.0, "end": 0.3},
                   {"word": "b", "start": 0.4, "end": 0.7},
                   {"word": "c", "start": 0.8, "end": 1.0}]},
        {"text": "d e", "start": 1.0, "end": 2.0,
         "words": [{"word": "d", "start": 1.0, "end": 1.4},
                   {"word": "e", "start": 1.5, "end": 1.9}]},
    ]
    result = A.align_segments(segs, "x.wav", "en", align_backend="whisperx",
                              progress=lambda *a: None)

    # seg 0 kept DTW words (3 words, original timestamps)
    assert len(result[0]["words"]) == 3
    assert result[0]["words"][1]["word"] == "b"
    assert result[0]["words"][1]["start"] == pytest.approx(0.4)
    # seg 1 was aligned normally (2 words) — but also mismatched (1 vs 2) so it
    # keeps its ORIGINAL DTW words (start 1.0), not the bad_align's 0.0.
    assert len(result[1]["words"]) == 2
    assert result[1]["words"][0]["start"] == pytest.approx(1.0)  # kept DTW
    captured = capsys.readouterr()
    assert "word count" in captured.err.lower() or "mismatch" in captured.err.lower()


def test_align_segments_survives_sentence_resplit(monkeypatch, tmp_path):
    """whisperx >=3.8 re-splits its input on sentence boundaries.

    Regression: aligned["segments"] no longer maps 1:1 onto our input (2 in can
    come back as 3), so pairing the two by zip made every segment look
    mismatched and silently degraded the entire pass to "keep DTW" — alignment
    appeared to succeed while changing nothing. We must walk `word_segments`
    instead and keep OUR segmentation.
    """

    def _resplit_align(segments, *args, **kwargs):
        flat = []
        for seg in segments:
            for w in seg["words"]:
                flat.append({
                    "word": w["word"].strip(),
                    "start": round(w["start"] + 0.07, 2),
                    "end": round(w["end"] + 0.07, 2),
                })
        # Deliberately regroup the flat words into DIFFERENT sentences than the
        # input used (3 words in -> 2 sentences out), so `segments` can never be
        # zipped against our input while `word_segments` stays authoritative.
        mid = max(1, len(flat) // 2)
        regrouped = []
        for chunk in (flat[:mid], flat[mid:]):
            if not chunk:
                continue
            regrouped.append({
                "text": " ".join(w["word"] for w in chunk),
                "words": chunk,
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
            })
        return {"segments": regrouped, "word_segments": flat}

    fake = type(sys)("whisperx")
    fake.align = _resplit_align
    fake.load_align_model = lambda *a, **k: (object(), object())
    fake.load_audio = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(A, "whisperx_available", lambda: True)
    monkeypatch.setattr(A, "release_align_memory", lambda *a, **k: None)

    segs = [
        {"text": "a b c", "start": 0.0, "end": 1.0,
         "words": [{"word": "a", "start": 0.0, "end": 0.3},
                   {"word": " b", "start": 0.4, "end": 0.7},
                   {"word": " c", "start": 0.8, "end": 1.0}]},
        {"text": "d e", "start": 1.0, "end": 2.0,
         "words": [{"word": " d", "start": 1.0, "end": 1.4},
                   {"word": " e", "start": 1.5, "end": 1.9}]},
    ]
    result = A.align_segments(segs, "x.wav", "en", align_backend="whisperx",
                              progress=lambda *a: None)

    # Our segmentation survives even though whisperx regrouped the output.
    assert len(result) == 2
    assert result[0]["text"] == "a b c"
    assert result[1]["text"] == "d e"
    # Word text stays ours (leading spaces preserved); timings come from
    # word_segments (+0.07).
    assert result[0]["words"][1]["word"] == " b"
    assert result[0]["words"][1]["start"] == pytest.approx(0.47)
    assert result[1]["words"][0]["start"] == pytest.approx(1.07)
    # Segment bounds re-derived from the aligned words.
    assert result[0]["start"] == pytest.approx(0.07)
    assert result[1]["end"] == pytest.approx(1.97)


# ---------------------------------------------------------------------------
# 4. GPU memory release
# ---------------------------------------------------------------------------


def test_release_align_memory_calls_cuda_cleanup(monkeypatch):
    """对齐前后应 del + empty_cache（8GB 红线分步执行，决策 3）。"""
    import torch

    calls = {"empty": 0}
    monkeypatch.setattr(torch.cuda, "empty_cache",
                        lambda: calls.__setitem__("empty", calls["empty"] + 1))

    A.release_align_memory()
    assert calls["empty"] == 1


# ---------------------------------------------------------------------------
# 5. graceful degradation at the transcribe_video boundary
# ---------------------------------------------------------------------------


def test_align_none_does_not_import_whisperx(monkeypatch, tmp_path):
    """--align none: 对齐 pass 完全不触发；不引入任何 whisperx 依赖。"""
    import video_translate.transcribe as T

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)

    captured = {"calls": 0}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["calls"] += 1
            return [], None

    fake_fw = type(sys)("faster_whisper")
    fake_fw.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
    # ensure whisperx is NOT importable
    monkeypatch.setitem(sys.modules, "whisperx", None)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang="en",
                             align_backend="none", progress=lambda *_: None)
    assert captured["calls"] == 1
    assert out.endswith("x.segments_en.json")


def test_align_whisperx_unavailable_falls_back_none(monkeypatch, tmp_path, capsys):
    """显式 whisperx 但库缺失 -> 告警 + 回退 none，主流程不出错（铁律 2）。"""
    import video_translate.transcribe as T

    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(T, "extract_chunk", lambda *a, **k: None)
    # whisperx unavailable
    monkeypatch.setitem(sys.modules, "whisperx", None)
    # even if probe is reached, force False
    import video_translate.align as AL
    monkeypatch.setattr(AL, "whisperx_available", lambda: False)

    captured = {"calls": 0}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, language=None, **kw):
            captured["calls"] += 1
            return [], None

    fake_fw = type(sys)("faster_whisper")
    fake_fw.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    out = T.transcribe_video("vid.mp4", str(tmp_path), base="x", lang="en",
                             align_backend="whisperx", progress=lambda *_: None)
    assert captured["calls"] == 1
    captured_out = capsys.readouterr()
    assert "whisperx" in captured_out.err.lower()
    assert out.endswith("x.segments_en.json")

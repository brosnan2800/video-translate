"""ADR-016 (T2a): fill_gaps recovery decode is always bare (vad_filter=False),
regardless of the run's --vad flag. VAD in recovery re-ejects speech-under-noise
that this module exists to recover."""

import sys

from video_translate import fill_gaps as F


def test_fill_gaps_recovery_always_bare(monkeypatch):
    captured = {}

    monkeypatch.setattr(F, "probe_duration", lambda p: 6.0)
    monkeypatch.setattr(F, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(F, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            captured["vad_filter"] = kw.get("vad_filter")
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    segments = [
        {"start": 0.0, "end": 1.0, "text": "hello world"},
        {"start": 5.0, "end": 6.0, "text": "goodbye now"},
    ]
    # gap [1,5] = 4s > min_gap 2.0 -> a hole to force-decode
    F.fill_gaps("vid.mp4", segments, silence_intervals=[], use_vad=True,
                progress=lambda *_: None)

    # recovery decode must be bare even though use_vad=True was passed
    assert captured["vad_filter"] is False

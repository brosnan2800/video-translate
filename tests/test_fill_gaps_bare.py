"""ADR-016 (T2a): fill_gaps recovery decode is always bare (vad_filter=False),
regardless of the run's --vad flag. VAD in recovery re-ejects speech-under-noise
that this module exists to recover."""

import os
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


def test_hf_hub_offline_set_before_gap_vocal_sep(monkeypatch):
    """ADR-032 Bug 3 — offline switch must precede every Demucs load point.

    gap-vocal-sep loads Demucs inside ``recover_hard_gaps``, which runs BEFORE
    WhisperModel is constructed. With ``HF_HUB_OFFLINE`` set only after that
    call, huggingface_hub still issued network HEAD requests and proxy-less
    machines died in a 5x retry loop against huggingface.co — even though the
    model was already cached locally.
    """
    import video_translate.gap_vocal_sep as gvs

    seen = {}

    def fake_recover(*_a, **_k):
        # Snapshot the offline policy AT THE MOMENT Demucs would be loaded.
        seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
        return {}

    monkeypatch.setattr(gvs, "recover_hard_gaps", fake_recover)
    monkeypatch.setattr(F, "probe_duration", lambda p: 20.0)
    monkeypatch.setattr(F, "extract_chunk", lambda *_a, **_k: None)
    monkeypatch.setattr(F, "resolve_device", lambda *_a, **_k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *_a, **_k):
            pass

        def transcribe(self, wav, **kw):
            return [], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    # Hole [1.0, 10.0] = 9s > min_gap 2.0 -> gap-vocal-sep is exercised.
    segments = [
        {"start": 0.0, "end": 1.0, "text": "hello world"},
        {"start": 10.0, "end": 11.0, "text": "goodbye now"},
    ]
    F.fill_gaps("vid.mp4", segments, silence_intervals=[], gap_vocal_sep=True,
                progress=lambda *_: None)

    assert seen.get("offline") == "1", (
        "HF_HUB_OFFLINE must already be '1' when gap-vocal-sep loads Demucs, "
        "otherwise huggingface_hub reaches for the network and hangs (5x retry "
        "timeout) on machines without a proxy — even with the model cached."
    )

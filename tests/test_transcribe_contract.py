"""Contract tests for transcribe planning/merge/resume (Spec 02, ADR-002).

No model or ffmpeg needed: plan_chunks and merge_chunks are pure, and resume is
exercised by pre-seeding chunk_N.json so the model is never loaded.
"""
import os

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
    assert T.DEVICE == "cpu"
    assert T.COMPUTE_TYPE == "int8"
    assert T.BEAM_SIZE == 1 and T.BEST_OF == 1


def test_resume_skips_completed_chunks_without_model(tmp_path, monkeypatch):
    """If every chunk_N.json exists, transcribe_video must not import a model."""
    outdir = str(tmp_path)
    # total 300s, chunk 240s -> chunks 0,1 (durations 240, 60)
    monkeypatch.setattr(T, "probe_duration", lambda p: 300.0)

    def _boom(*a, **k):  # extract must never be called on full resume
        raise AssertionError("extract_chunk called during full resume")

    monkeypatch.setattr(T, "extract_chunk", _boom)

    save_json(os.path.join(outdir, "chunk_0.json"),
              [{"start": 0, "end": 1, "text": "hello"}], indent=0)
    save_json(os.path.join(outdir, "chunk_1.json"),
              [{"start": 240, "end": 241, "text": "world"}], indent=0)

    out = T.transcribe_video("dummy.mp4", outdir, base="apollo_story",
                             progress=lambda *_: None)
    merged = __import__("json").load(open(out, encoding="utf-8"))
    assert [s["text"] for s in merged] == ["hello", "world"]


# --- V2 ---


def test_transcribe_base_defaults_to_input_stem(tmp_path, monkeypatch):
    """base=None -> derived from input filename stem."""
    monkeypatch.setattr(T, "probe_duration", lambda p: 10.0)
    save_json(os.path.join(str(tmp_path), "chunk_0.json"),
              [{"start": 0, "end": 1, "text": "hi"}], indent=0)

    def _boom(*a, **k):
        raise AssertionError("extract_chunk should not run on full resume")

    monkeypatch.setattr(T, "extract_chunk", _boom)
    out = T.transcribe_video("/path/to/myvideo.mp4", str(tmp_path),
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
                       progress=lambda *_: None)
    assert captured["language"] == "en"

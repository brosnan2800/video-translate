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
    # V4 quality pass: beam search (not greedy) + no cross-segment conditioning
    assert T.BEAM_SIZE == 5 and T.BEST_OF == 5
    assert T.CONDITION_ON_PREVIOUS_TEXT is False
    assert T.REPETITION_PENALTY > 1.0
    # V6 (B2): recall-biased VAD so quiet / music-underscored lines still decode
    assert T.VAD_THRESHOLD < 0.5
    assert T.VAD_PARAMS["threshold"] == T.VAD_THRESHOLD


def test_vad_threshold_override_changes_fingerprint():
    """A different VAD threshold must invalidate the chunk cache — otherwise a
    re-run with --vad-threshold would silently reuse the old transcription."""
    base_fp = T.transcribe_fingerprint("large-v3", 240.0, None,
                                       T.build_vad_params())
    tuned_fp = T.transcribe_fingerprint("large-v3", 240.0, None,
                                        T.build_vad_params(0.2))
    assert base_fp != tuned_fp
    # default arg path stays identical to explicit defaults (back-compat)
    assert T.transcribe_fingerprint("large-v3", 240.0, None) == base_fp


def test_build_vad_params_does_not_mutate_module_default():
    T.build_vad_params(0.9)
    assert T.VAD_PARAMS["threshold"] == T.VAD_THRESHOLD


def _seed_chunk(outdir: str, base: str, ci: int, payload) -> None:
    """Seed a chunk cache at the fingerprinted path the runner will look up."""
    fp = T.transcribe_fingerprint("large-v3", 240.0, None)
    save_json(os.path.join(outdir, f"{base}.{fp}.chunk_{ci}.json"),
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
                              progress=lambda *_: None)
    merged = _json.load(open(out, encoding="utf-8"))
    assert captured["word_timestamps"] is True
    assert merged[0]["words"] == [
        {"word": "Hello", "start": 1.05, "end": 1.4},
        {"word": "world", "start": 1.5, "end": 1.95},
    ]


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

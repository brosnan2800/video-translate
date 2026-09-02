"""ADR-034 §6.2 — G1 / G2 re-processing contracts + the independent cache layer.

These are integration tests for the recovery *plumbing* inside ``fill_gaps``:
whisper is faked (no model, no ffmpeg, no video), so every case asserts on the
contract — which windows get decoded, what gets spliced/merged, what the ADR-021
guard rejects, and when the cache short-circuits a re-run.
"""
import copy
import sys

from video_translate import fill_gaps as F


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class W:
    """Fake faster-whisper word."""

    def __init__(self, word: str, start: float, end: float):
        self.word, self.start, self.end = word, start, end


class S:
    """Fake faster-whisper segment (window-local times)."""

    def __init__(self, text, start, end, words=None, nsp=None, alp=None):
        self.text, self.start, self.end = text, start, end
        self.words = list(words or [])
        self.no_speech_prob = nsp
        self.avg_logprob = alp
        self.compression_ratio = None


def _noop(*_a, **_k):
    return None


def _install(monkeypatch, total: float, replies):
    """Patch fill_gaps' I/O + whisper model.

    ``replies(window_start, window_dur)`` returns the fake segments for that
    decode (window-local times; the caller shifts them to absolute). Returns a
    recorder dict with the decoded ``windows`` and a ``decodes`` counter.
    """
    rec: dict = {"windows": [], "decodes": 0}

    monkeypatch.setattr(F, "probe_duration", lambda _p: total)
    monkeypatch.setattr(F, "resolve_device", lambda *_a, **_k: ("cpu", "int8"))

    def _extract(_src, _wav, start, dur, *_a, **_k):
        rec["windows"].append((round(float(start), 2), round(float(dur), 2)))

    monkeypatch.setattr(F, "extract_chunk", _extract)

    class FakeModel:
        def __init__(self, *_a, **_k):
            pass

        def transcribe(self, _wav, **_kw):
            rec["decodes"] += 1
            start, dur = rec["windows"][-1]
            return replies(start, dur), None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    return rec


# A timeline whose single segment spans 0-6s but whose words only cover 0-2s:
# the 2-6s stretch is energetic (silences=[]) yet word-uncovered -> G2 target.
def _g2_timeline():
    return [{"start": 0.0, "end": 6.0, "text": "hello world",
             "words": [{"word": " hello", "start": 0.0, "end": 1.0},
                       {"word": " world", "start": 1.0, "end": 2.0}]}]


# --------------------------------------------------------------------------- #
# G2 — intra-segment word-uncovered recovery (ADR-034 §3.3)
# --------------------------------------------------------------------------- #

def test_g2_merges_recovered_words_back(tmp_path, monkeypatch):
    def replies(start, dur):
        # the decode window is the 2-6s gap (+0.2s pad) => local (0.2, 4.2)
        return [S(" recovered speech", 0.2, dur,
                  [W(" recovered", 0.2, 1.0), W(" speech", 1.0, dur)])]

    rec = _install(monkeypatch, 6.0, replies)
    segs = _g2_timeline()
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], g1=False,
                      outdir=str(tmp_path), base="g2",
                      progress=_noop)

    assert rec["decodes"] == 1, "exactly the suspect sub-window is decoded"
    assert len(out) == 1, "G2 merges WORDS, it does not add a sibling cue"
    seg = out[0]
    assert seg["text"] == "hello world recovered speech"
    assert [w["word"] for w in seg["words"]] == [
        " hello", " world", " recovered", " speech"
    ]
    assert seg["start"] == 0.0 and seg["end"] == 6.0
    # ADR-031 D2 spirit: the amendment is visible downstream
    assert seg["_g2_recovered"] is True


def test_g2_hallucinated_recovery_is_rejected_by_adr021_guard(
        tmp_path, monkeypatch):
    """ADR-034 §3.3: recovered segments MUST pass _is_recovered_hallucination.
    A high no_speech_prob decode is the strongest phantom signal — it must not
    be merged back into the timeline."""

    def replies(start, dur):
        return [S(" Thank you.", 0.2, 1.0,
                  [W(" Thank", 0.2, 0.6), W(" you.", 0.6, 1.0)],
                  nsp=0.9)]  # >= 0.6 -> guard signal C

    rec = _install(monkeypatch, 6.0, replies)
    segs = _g2_timeline()
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], g1=False,
                      outdir=str(tmp_path), base="g2guard",
                      progress=_noop)

    assert rec["decodes"] == 1, "the window was decoded, then rejected"
    seg = out[0]
    assert seg["text"] == "hello world", "timeline untouched"
    assert "_g2_recovered" not in seg
    assert len(seg["words"]) == 2


def test_g2_leaves_fully_covered_segments_alone(tmp_path, monkeypatch):
    """A segment whose words cover its whole span has no suspect sub-window."""

    def replies(_s, _d):  # pragma: no cover - must never be called
        raise AssertionError("nothing should be decoded")

    rec = _install(monkeypatch, 2.0, replies)
    segs = [{"start": 0.0, "end": 2.0, "text": "hello world",
             "words": [{"word": " hello", "start": 0.0, "end": 1.0},
                       {"word": " world", "start": 1.0, "end": 2.0}]}]
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], g1=False,
                      outdir=str(tmp_path), base="g2clean",
                      progress=_noop)
    assert rec["decodes"] == 0
    assert out == segs


def test_g2_silent_gap_between_words_is_not_decoded(tmp_path, monkeypatch):
    """A pause between clauses is silence, not lost speech (B is the judge)."""

    def replies(_s, _d):  # pragma: no cover
        raise AssertionError("silence must not be force-decoded")

    rec = _install(monkeypatch, 10.0, replies)
    segs = [{"start": 0.0, "end": 10.0, "text": "one two",
             "words": [{"word": " one", "start": 0.0, "end": 2.0},
                       {"word": " two", "start": 8.0, "end": 10.0}]}]
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[(2.0, 8.0)],
                      g1=False, outdir=str(tmp_path), base="g2silence",
                      progress=_noop)
    assert rec["decodes"] == 0
    assert out == segs


# --------------------------------------------------------------------------- #
# G1 — silence-sliced local resegment (ADR-034 §3.2)
# --------------------------------------------------------------------------- #

def test_g1_replaces_suspect_with_silence_sliced_decodes(tmp_path, monkeypatch):
    """An energetic window whisper itself scored as non-speech is re-cut on the
    silence gap inside it, and each slice is decoded separately."""
    segs = [{"start": 0.0, "end": 10.0, "text": "mm hm",
             "no_speech_prob": 0.9,
             "words": [{"word": " mm", "start": 0.0, "end": 1.0},
                       {"word": " hm", "start": 1.0, "end": 2.0}]}]

    def replies(start, dur):
        if start < 3.0:  # slice (0,4)
            return [S(" first half", 0.0, 4.0,
                      [W(" first", 0.0, 1.0), W(" half", 1.0, 4.0)])]
        return [S(" second half", 0.2, 4.0,      # slice (6,10)
                  [W(" second", 0.2, 1.0), W(" half", 1.0, 4.0)])]

    rec = _install(monkeypatch, 10.0, replies)
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[(4.0, 6.0)],
                      g2=False, outdir=str(tmp_path), base="g1",
                      progress=_noop)

    assert rec["decodes"] == 2, "one decode per silence slice"
    assert len(out) == 2
    assert all(s.get("_recovered") for s in out)
    assert "mm hm" not in " ".join(s["text"] for s in out)
    assert out[0]["start"] < out[1]["start"], "timeline stays time-sorted"


def test_g1_does_not_fire_without_an_inner_silence_gap(tmp_path, monkeypatch):
    """No silence inside the window => G1 cannot cut (ADR-034 §3.2); the window
    falls through instead of being re-decoded as a single slice."""

    def replies(_s, _d):  # pragma: no cover
        raise AssertionError("G1 must not fire without a silence boundary")

    rec = _install(monkeypatch, 10.0, replies)
    segs = [{"start": 0.0, "end": 10.0, "text": "mm hm",
             "no_speech_prob": 0.9,
             "words": [{"word": " mm", "start": 0.0, "end": 1.0}]}]
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], g2=False,
                      outdir=str(tmp_path), base="g1nogap",
                      progress=_noop)
    assert rec["decodes"] == 0
    assert out == segs


# --------------------------------------------------------------------------- #
# review switch
# --------------------------------------------------------------------------- #

def test_review_disabled_skips_g1_and_g2(tmp_path, monkeypatch):
    def replies(_s, _d):  # pragma: no cover
        raise AssertionError("no review -> no re-decode")

    rec = _install(monkeypatch, 6.0, replies)
    segs = _g2_timeline()
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], review=False,
                      outdir=str(tmp_path), base="noreview",
                      progress=_noop)
    assert rec["decodes"] == 0
    assert out is segs


# --------------------------------------------------------------------------- #
# independent cache layer (ADR-034 §5.2)
# --------------------------------------------------------------------------- #

def test_cache_hit_skips_redecode(tmp_path, monkeypatch):
    def replies(start, dur):
        return [S(" recovered speech", 0.2, dur,
                  [W(" recovered", 0.2, 1.0), W(" speech", 1.0, dur)])]

    rec = _install(monkeypatch, 6.0, replies)
    out1 = F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()),
                       silence_intervals=[], g1=False,
                       outdir=str(tmp_path), base="cache",
                       progress=_noop)
    assert rec["decodes"] == 1
    assert (tmp_path / "cache.review.json").is_file()

    out2 = F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()),
                       silence_intervals=[], g1=False,
                       outdir=str(tmp_path), base="cache",
                       progress=_noop)
    assert rec["decodes"] == 1, "re-run must reuse the cache, not re-decode"
    assert out2 == out1


def test_cache_invalidated_when_timeline_changes(tmp_path, monkeypatch):
    def replies(start, dur):
        return [S(" recovered speech", 0.2, dur,
                  [W(" recovered", 0.2, 1.0), W(" speech", 1.0, dur)])]

    rec = _install(monkeypatch, 6.0, replies)
    F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()),
                silence_intervals=[], g1=False,
                outdir=str(tmp_path), base="cache2",
                progress=_noop)
    assert rec["decodes"] == 1

    changed = copy.deepcopy(_g2_timeline())
    changed[0]["text"] = "hello world (edited)"
    F.fill_gaps("vid.mp4", changed, silence_intervals=[], g1=False,
                outdir=str(tmp_path), base="cache2",
                progress=_noop)
    assert rec["decodes"] == 2, "a changed timeline must not reuse the cache"


def test_cache_is_parameter_sensitive(tmp_path, monkeypatch):
    """Toggling the review switch changes the cache params -> a previous cache
    written with different params must NOT be reused."""
    def replies(start, dur):
        return [S(" recovered speech", 0.2, dur,
                  [W(" recovered", 0.2, 1.0), W(" speech", 1.0, dur)])]

    rec = _install(monkeypatch, 6.0, replies)
    kw = dict(silence_intervals=[], g1=False, outdir=str(tmp_path),
              base="cache3", progress=_noop)

    F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()), **kw)  # review=True
    assert rec["decodes"] == 1
    F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()), **kw)  # hit
    assert rec["decodes"] == 1

    F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()),
                review=False, **kw)                              # params differ
    assert rec["decodes"] == 1, "review=False decodes nothing"

    F.fill_gaps("vid.mp4", copy.deepcopy(_g2_timeline()), **kw)  # miss again
    assert rec["decodes"] == 2, "params changed -> cache must be recomputed"


def test_cache_file_is_written_even_when_nothing_is_suspect(
        tmp_path, monkeypatch):
    """A clean timeline still gets a cache entry so re-runs short-circuit the
    audit entirely."""
    def replies(_s, _d):  # pragma: no cover
        raise AssertionError("clean timeline -> no decode")

    _install(monkeypatch, 2.0, replies)
    segs = [{"start": 0.0, "end": 2.0, "text": "hello world",
             "words": [{"word": " hello", "start": 0.0, "end": 1.0},
                       {"word": " world", "start": 1.0, "end": 2.0}]}]
    out = F.fill_gaps("vid.mp4", segs, silence_intervals=[], g1=False,
                      outdir=str(tmp_path), base="clean",
                      progress=_noop)
    assert out == segs
    assert (tmp_path / "clean.review.json").is_file()

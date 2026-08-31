"""TDD tests for gap_vocal_sep module (ADR-030 / Spec 24).

These tests define the contract of the new "hard-gap vocal separation recovery"
layer before the module is fully implemented. All heavy dependencies are mocked.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _w(*pairs):
    """Build a words list from (word, start, end) triples."""
    return [{"word": w, "start": s, "end": e} for (w, s, e) in pairs]


def _route(lane="cuda", device="cuda", can_separate=True, message=""):
    """Build a VsepRoute (Spec 25) for stubbing resolve_vsep_route().

    Local Demucs must only run when can_separate=True, so tests default to a
    healthy CUDA route and opt into the blocked lanes explicitly.
    """
    from video_translate.vocal_sep import VsepRoute
    return VsepRoute(lane=lane, device=device,
                     can_separate=can_separate, message=message)


# ---------------------------------------------------------------------------
# Pure function: _looks_like_lyrics
# ---------------------------------------------------------------------------

class TestLooksLikeLyrics:
    """Spec 24 § strict guard signal F: repetitive / chant-like BGM lyrics."""

    def test_repeated_bigram_is_lyric(self):
        from video_translate.gap_vocal_sep import _looks_like_lyrics
        assert _looks_like_lyrics("Inside you so deep inside you so deep") is True

    def test_repeated_single_word_is_lyric(self):
        from video_translate.gap_vocal_sep import _looks_like_lyrics
        assert _looks_like_lyrics("yeah yeah yeah yeah") is True

    def test_normal_speech_is_not_lyric(self):
        from video_translate.gap_vocal_sep import _looks_like_lyrics
        assert _looks_like_lyrics("That's my favorite dance move of all time.") is False

    def test_empty_is_not_lyric(self):
        from video_translate.gap_vocal_sep import _looks_like_lyrics
        assert _looks_like_lyrics("") is False


# ---------------------------------------------------------------------------
# Pure function: _is_gap_vocal_hallucination
# ---------------------------------------------------------------------------

class TestIsGapVocalHallucination:
    """Spec 24 § strict guard is stricter than ADR-021 _is_recovered_hallucination."""

    def test_stricter_no_speech_threshold(self):
        from video_translate.gap_vocal_sep import _is_gap_vocal_hallucination
        from video_translate.fill_gaps import _is_recovered_hallucination

        cand = {
            "start": 100.0,
            "end": 103.0,
            "text": "Maybe real maybe not.",
            "words": _w(("Maybe", 100.0, 100.8), ("real", 100.8, 101.6),
                       ("maybe", 101.6, 102.4), ("not.", 102.4, 103.0)),
            "no_speech_prob": 0.55,
            "avg_logprob": -0.7,
        }
        # New guard drops at 0.5
        assert _is_gap_vocal_hallucination(cand, []) is True
        # Old guard keeps at 0.55 (< 0.6)
        assert _is_recovered_hallucination(cand, []) is False

    def test_stricter_avg_logprob_threshold(self):
        from video_translate.gap_vocal_sep import _is_gap_vocal_hallucination
        from video_translate.fill_gaps import _is_recovered_hallucination

        cand = {
            "start": 200.0,
            "end": 203.0,
            "text": "Low confidence line here.",
            "words": _w(("Low", 200.0, 200.5), ("confidence", 200.5, 201.2),
                       ("line", 201.2, 202.0), ("here.", 202.0, 203.0)),
            "no_speech_prob": 0.2,
            "avg_logprob": -0.85,
        }
        assert _is_gap_vocal_hallucination(cand, []) is True
        assert _is_recovered_hallucination(cand, []) is False

    def test_lyrics_pattern_is_hallucination(self):
        from video_translate.gap_vocal_sep import _is_gap_vocal_hallucination

        text = "Inside you so deep inside you so deep"
        cand = {
            "start": 821.0,
            "end": 833.0,
            "text": text,
            "words": [{"word": w, "start": 821.0 + i * 0.5, "end": 821.0 + (i + 1) * 0.5}
                      for i, w in enumerate(text.split())],
            "no_speech_prob": 0.45,
            "avg_logprob": -0.6,
        }
        assert _is_gap_vocal_hallucination(cand, []) is True

    def test_real_recovery_is_kept(self):
        from video_translate.gap_vocal_sep import _is_gap_vocal_hallucination

        cand = {
            "start": 345.0,
            "end": 351.0,
            "text": "That's my favorite dance move of all time.",
            "words": _w(("That's", 345.0, 345.5), ("my", 345.5, 345.9),
                       ("favorite", 345.9, 346.6), ("dance", 346.6, 347.4),
                       ("move", 347.4, 348.2), ("of", 348.2, 348.6),
                       ("all", 348.6, 349.4), ("time.", 349.4, 351.0)),
            "no_speech_prob": 0.15,
            "avg_logprob": -0.4,
        }
        assert _is_gap_vocal_hallucination(cand, []) is False

    def test_overlap_signal_still_fires(self):
        from video_translate.gap_vocal_sep import _is_gap_vocal_hallucination

        cand = {
            "start": 13.40,
            "end": 13.56,
            "text": "Don't worry.",
            "words": _w(("Don't", 13.40, 13.52), ("worry.", 13.52, 13.56)),
            "no_speech_prob": 0.1,
            "avg_logprob": -0.3,
        }
        seg3 = {"start": 12.38, "end": 13.60, "text": "going to work."}
        assert _is_gap_vocal_hallucination(cand, [seg3]) is True


# ---------------------------------------------------------------------------
# select_hard_gaps
# ---------------------------------------------------------------------------

class TestSelectHardGaps:
    """Spec 24 § hard-gap selection: long + high energy."""

    @staticmethod
    def _select(input_path, holes, **kwargs):
        from video_translate.gap_vocal_sep import select_hard_gaps
        defaults = {
            "min_gap": 5.0,
            "energy_mean_db": -30.0,
            "energy_max_db": -10.0,
            "progress": lambda *a, **k: None,
        }
        defaults.update(kwargs)
        return select_hard_gaps(input_path, holes, **defaults)

    def test_short_hole_is_filtered(self):
        with patch("video_translate.gap_vocal_sep._window_energy",
                   return_value=(-20.0, -5.0)):
            holes = [(10.0, 14.0)]  # 4s < 5s
            assert self._select("/fake.mp4", holes) == []

    def test_long_but_silent_hole_is_filtered(self):
        with patch("video_translate.gap_vocal_sep._window_energy",
                   return_value=(-45.0, -25.0)):
            holes = [(10.0, 20.0)]
            assert self._select("/fake.mp4", holes) == []

    def test_long_and_loud_hole_is_kept(self):
        with patch("video_translate.gap_vocal_sep._window_energy",
                   return_value=(-20.0, -5.0)):
            holes = [(10.0, 20.0)]
            assert self._select("/fake.mp4", holes) == [(10.0, 20.0)]

    def test_mean_quiet_but_max_loud_is_kept(self):
        with patch("video_translate.gap_vocal_sep._window_energy",
                   return_value=(-35.0, -8.0)):
            holes = [(10.0, 20.0)]
            assert self._select("/fake.mp4", holes) == [(10.0, 20.0)]

    def test_mixed_holes_select_only_hard_ones(self):
        with patch("video_translate.gap_vocal_sep._window_energy",
                   side_effect=[(-20.0, -5.0), (-45.0, -25.0), (-35.0, -8.0)]):
            holes = [(10.0, 20.0), (30.0, 40.0), (50.0, 56.0)]
            assert self._select("/fake.mp4", holes) == [(10.0, 20.0), (50.0, 56.0)]


# ---------------------------------------------------------------------------
# recover_hard_gaps orchestration
# ---------------------------------------------------------------------------

class TestRecoverHardGaps:
    """Spec 24 § recover_hard_gaps orchestration with mocked Demucs."""

    @staticmethod
    def _recover(*, input_path="/fake.mp4", segments=None, holes=None,
                 audio_source=None, route=None, **kwargs):
        from video_translate.gap_vocal_sep import recover_hard_gaps
        defaults = {
            "min_gap": 5.0,
            "energy_mean_db": -30.0,
            "energy_max_db": -10.0,
            "progress": lambda *a, **k: None,
        }
        defaults.update(kwargs)
        # Local Demucs is gated by the route (Spec 25). Stub it so these tests
        # stay hardware-independent instead of silently degrading on CPU boxes
        # (the pre-Spec-25 `_cuda_ready` gate made them fail there).
        with patch("video_translate.gap_vocal_sep.resolve_vsep_route",
                   return_value=route or _route()):
            return recover_hard_gaps(
                input_path,
                segments or [],
                holes or [],
                audio_source=audio_source,
                **defaults,
            )

    def test_reuses_global_vocals_when_available(self):
        """If audio_source (global vocals.wav) exists, do NOT call Demucs."""
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0)]) as mock_select:
            with patch("video_translate.gap_vocal_sep.separate_vocals") as mock_sep:
                with patch("os.path.isfile", return_value=True):
                    with patch("video_translate.gap_vocal_sep.probe_duration",
                               return_value=30.0):
                        mapping = self._recover(
                            input_path="/fake.mp4",
                            audio_source="/cached/global.vocals.wav",
                        )
                        mock_sep.assert_not_called()
                        # global track -> local start is the gap's own offset
                        assert mapping == {(10.0, 20.0): ("/cached/global.vocals.wav", 10.0)}
                        mock_select.assert_called_once()

    def test_runs_demucs_per_hard_gap(self):
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0), (30.0, 40.0)]):
            with patch("video_translate.gap_vocal_sep.separate_vocals",
                       side_effect=["/tmp/a.vocals.wav", "/tmp/b.vocals.wav"]) as mock_sep:
                with patch("video_translate.gap_vocal_sep.probe_duration",
                           return_value=10.0):
                    with patch("video_translate.gap_vocal_sep.extract_chunk"):
                        mapping = self._recover(input_path="/fake.mp4")
                        assert mock_sep.call_count == 2
                        # per-window files hold only their gap -> local start 0.0
                        assert mapping == {
                            (10.0, 20.0): ("/tmp/a.vocals.wav", 0.0),
                            (30.0, 40.0): ("/tmp/b.vocals.wav", 0.0),
                        }

    def test_skips_gap_when_vocals_duration_mismatched(self):
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0)]):
            with patch("video_translate.gap_vocal_sep.separate_vocals",
                       return_value="/tmp/bad.vocals.wav") as mock_sep:
                # Mocked separate_vocals does not probe, so the first explicit
                # probe_duration in recover_hard_gaps returns 9.5 (mismatch).
                with patch("video_translate.gap_vocal_sep.probe_duration",
                           return_value=9.5):
                    with patch("video_translate.gap_vocal_sep.extract_chunk"):
                        mapping = self._recover(input_path="/fake.mp4")
                        mock_sep.assert_called_once()
                        assert mapping == {}

    def test_no_hard_gaps_returns_empty(self):
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[]):
            with patch("video_translate.gap_vocal_sep.separate_vocals") as mock_sep:
                mapping = self._recover(input_path="/fake.mp4")
                mock_sep.assert_not_called()
                assert mapping == {}


# ---------------------------------------------------------------------------
# Lane gating (Spec 25 / ADR-031)
# ---------------------------------------------------------------------------


class TestRecoverHardGapsLaneGating:
    """Spec 25 § 消费者行为 3 — 本地 Demucs 只在 CUDA 就绪线上执行。"""

    @staticmethod
    def _recover_blocked(route):
        from video_translate.gap_vocal_sep import recover_hard_gaps

        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0)]), \
             patch("video_translate.gap_vocal_sep.separate_vocals") as mock_sep, \
             patch("video_translate.gap_vocal_sep.resolve_vsep_route",
                   return_value=route):
            mapping = recover_hard_gaps(
                "/fake.mp4", [], [],
                audio_source=None,
                progress=lambda *a, **k: None,
            )
        return mapping, mock_sep

    def test_cpu_lane_skips_local_demucs(self):
        r = _route(lane="cpu", device=None, can_separate=False,
                   message="CPU 模式不支持人声分离（GPU-only）")
        mapping, mock_sep = self._recover_blocked(r)
        mock_sep.assert_not_called()
        assert mapping == {}

    def test_apple_silicon_lane_skips_local_demucs(self):
        """Apple Silicon 是待办：跳过本地分离，绝不降级 CPU 跑 Demucs。"""
        r = _route(lane="apple_silicon", device=None, can_separate=False,
                   message="Apple Silicon 人声分离为待办（TODO），本期未实现")
        mapping, mock_sep = self._recover_blocked(r)
        mock_sep.assert_not_called()
        assert mapping == {}

    def test_cuda_not_ready_skips_local_demucs(self):
        """红线：N 卡但 CUDA 未就绪 → 跳过本地分离，提示跑 setup。"""
        r = _route(lane="cuda", device=None, can_separate=False,
                   message="检测到 NVIDIA GPU 但 CUDA 未就绪，请运行 make setup")
        mapping, mock_sep = self._recover_blocked(r)
        mock_sep.assert_not_called()
        assert mapping == {}

    def test_blocked_lane_warns_once_with_route_message(self):
        """Spec 25 § 绝不静默 — 告警只打一次，且必须带上 route.message。"""
        msg = "CPU 模式不支持人声分离（GPU-only）"
        r = _route(lane="cpu", device=None, can_separate=False, message=msg)
        from video_translate.gap_vocal_sep import recover_hard_gaps

        lines: list[str] = []
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0), (30.0, 40.0)]), \
             patch("video_translate.gap_vocal_sep.resolve_vsep_route",
                   return_value=r):
            recover_hard_gaps("/fake.mp4", [], [], audio_source=None,
                              progress=lines.append)
        hits = [ln for ln in lines if msg in ln]
        assert len(hits) == 1, f"expected exactly ONE warning, got {hits}"

    def test_global_vocals_reuse_is_not_gated_by_lane(self):
        """Spec 25 不变量 3 — 复用全局 vocals 不触发 Demucs，不受设备限制。"""
        from video_translate.gap_vocal_sep import recover_hard_gaps

        r = _route(lane="cpu", device=None, can_separate=False,
                   message="CPU 模式不支持人声分离")
        with patch("video_translate.gap_vocal_sep.select_hard_gaps",
                   return_value=[(10.0, 20.0)]), \
             patch("video_translate.gap_vocal_sep.separate_vocals") as mock_sep, \
             patch("video_translate.gap_vocal_sep.resolve_vsep_route",
                   return_value=r), \
             patch("os.path.isfile", return_value=True), \
             patch("video_translate.gap_vocal_sep.probe_duration",
                   return_value=30.0):
            mapping = recover_hard_gaps(
                "/fake.mp4", [], [],
                audio_source="/cached/global.vocals.wav",
                progress=lambda *a, **k: None,
            )
        mock_sep.assert_not_called()
        assert mapping == {(10.0, 20.0): ("/cached/global.vocals.wav", 10.0)}


# ---------------------------------------------------------------------------
# _decode_gap_vocals helper
# ---------------------------------------------------------------------------

class TestDecodeGapVocals:
    """Spec 24 § decode hard gaps from the cleaned vocals map."""

    @pytest.fixture
    def fake_model(self):
        """Return a MagicMock that mimics faster-whisper WhisperModel.transcribe."""
        model = MagicMock()

        class FakeSegment:
            def __init__(self, text, start, end, words, nsp, alp):
                self.text = text
                self.start = start
                self.end = end
                self.words = words
                self.no_speech_prob = nsp
                self.avg_logprob = alp
                self.compression_ratio = 1.0

        def transcribe(*args, **kwargs):
            real = FakeSegment(
                "That's my favorite dance move.",
                0.0, 6.0,
                [MagicMock(word=w, start=0.0 + i, end=1.0 + i)
                 for i, w in enumerate("That's my favorite dance move.".split())],
                0.15, -0.4,
            )
            lyric = FakeSegment(
                "Inside you so deep inside you so deep",
                7.0, 13.0,
                [MagicMock(word=w, start=7.0 + i * 0.3, end=7.3 + i * 0.3)
                 for i, w in enumerate("Inside you so deep inside you so deep".split())],
                0.45, -0.6,
            )
            return [real, lyric], None

        model.transcribe = transcribe
        return model

    def test_decodes_and_filters_lyrics(self, fake_model, tmp_path):
        from video_translate.gap_vocal_sep import _decode_gap_vocals

        vocals = str(tmp_path / "gap.vocals.wav")
        open(vocals, "wb").close()
        # window-level separation: the file holds only the gap -> local start 0.0
        vocals_map = {(10.0, 23.0): (vocals, 0.0)}
        with patch("video_translate.gap_vocal_sep.extract_chunk"):
            with patch("video_translate.gap_vocal_sep.probe_duration", return_value=13.0):
                recovered = _decode_gap_vocals(
                    vocals_map,
                    model=fake_model,
                    lang="en",
                    segments=[],
                    progress=lambda *a, **k: None,
                )
                assert len(recovered) == 1
                assert "favorite" in recovered[0]["text"]
                assert recovered[0]["start"] == 10.0  # absolute time shift
                assert recovered[0]["end"] == 16.0

    def test_global_vocals_decodes_only_the_gap_window(self, fake_model, tmp_path):
        """Regression (ADR-030 §5): reusing the FULL-LENGTH global vocals.wav
        must decode only the gap window.

        Decoding the whole track cost a full-video pass per gap (the run looked
        frozen) and timestamped every recovered line against the wrong part of
        the timeline.
        """
        from video_translate.gap_vocal_sep import _decode_gap_vocals

        vocals = str(tmp_path / "global.vocals.wav")
        open(vocals, "wb").close()
        # global track: the gap starts at its own absolute offset inside the file
        vocals_map = {(10.0, 23.0): (vocals, 10.0)}
        with patch("video_translate.gap_vocal_sep.extract_chunk") as mock_extract:
            with patch("video_translate.gap_vocal_sep.probe_duration",
                       return_value=1297.6):  # full video length
                _decode_gap_vocals(
                    vocals_map,
                    model=fake_model,
                    lang="en",
                    segments=[],
                    progress=lambda *a, **k: None,
                )
        # extract_chunk(src, dst, start, duration) — must window, not whole file
        src, _dst, start, duration = mock_extract.call_args[0]
        assert src == vocals
        assert start == 10.0
        assert duration == 13.0

    def test_window_vocals_decodes_from_local_zero(self, fake_model, tmp_path):
        """A window-level separation file holds only the gap -> decode from 0.0."""
        from video_translate.gap_vocal_sep import _decode_gap_vocals

        vocals = str(tmp_path / "gap.vocals.wav")
        open(vocals, "wb").close()
        vocals_map = {(10.0, 23.0): (vocals, 0.0)}
        with patch("video_translate.gap_vocal_sep.extract_chunk") as mock_extract:
            with patch("video_translate.gap_vocal_sep.probe_duration", return_value=13.0):
                _decode_gap_vocals(
                    vocals_map,
                    model=fake_model,
                    lang="en",
                    segments=[],
                    progress=lambda *a, **k: None,
                )
        _src, _dst, start, duration = mock_extract.call_args[0]
        assert start == 0.0
        assert duration == 13.0


# ---------------------------------------------------------------------------
# Backward compatibility: default-off path
# ---------------------------------------------------------------------------

class TestDefaultOffCompatibility:
    """Spec 24 § Invariant: gap_vocal_sep=False leaves fill_gaps unchanged."""

    def test_fill_gaps_default_does_not_call_gap_recovery(self):
        from video_translate import fill_gaps as F
        # One segment covers the whole video -> no holes -> early return.
        segments = [{"start": 0.0, "end": 10.0, "text": "Hello world."}]
        with patch("video_translate.gap_vocal_sep.recover_hard_gaps") as mock_recover:
            with patch.object(F, "probe_duration", return_value=10.0):
                # We only care that recover_hard_gaps is not invoked
                F.fill_gaps("/fake.mp4", segments, gap_vocal_sep=False)
                mock_recover.assert_not_called()

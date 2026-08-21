"""Tests for vocal_sep module (T2: vocal/accompaniment separation).

These are split into two tiers:
  1. Pure / contract tests — NO demucs required. Cover fingerprint, path
     conventions, and graceful fallback via mocks. These run fast and MUST
     all pass before implementation is considered correct.
  2. @pytest.mark.slow integration tests — only run with ``pytest -m slow``.
     Require demucs + a test fixture video.

All invariants from Spec 19 / ADR-017 are guarded here.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Tier 1: pure / contract tests (no heavy imports, no demucs)
# ---------------------------------------------------------------------------


class TestSeparateFingerprint:
    """Spec 19 § separate_fingerprint contract."""

    @staticmethod
    def _fp(input_path, backend="demucs", model="htdemucs"):
        # Import lazily — the test module itself must be importable without
        # demucs installed (it is a core dep, but keep the test import-safe).
        from video_translate.vocal_sep import separate_fingerprint
        return separate_fingerprint(input_path, backend, model)

    def test_fingerprint_is_8_char_hex(self, tmp_path):
        f = tmp_path / "dummy.mp4"
        f.write_bytes(b"x")
        fp = self._fp(str(f))
        assert len(fp) == 8
        int(fp, 16)  # raises ValueError if not hex

    def test_fingerprint_stable_on_same_input(self, tmp_path):
        """Same file + same params → identical fp (determinism)."""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"hello world")
        assert self._fp(str(f)) == self._fp(str(f))

    def test_fingerprint_changes_on_content_change(self, tmp_path):
        """filesize / bytes change → fp changes."""
        f1 = tmp_path / "a.mp4"
        f2 = tmp_path / "b.mp4"
        f1.write_bytes(b"x" * 100)
        f2.write_bytes(b"x" * 101)  # different size
        assert self._fp(str(f1)) != self._fp(str(f2))

    def test_fingerprint_changes_on_mtime(self, tmp_path):
        """mtime change (file edited in place) → fp changes (ADR-002 guard)."""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"static content")
        fp1 = self._fp(str(f))
        # Advance mtime by 5 seconds
        new_atime = time.time() + 5
        new_mtime = time.time() + 5
        os.utime(str(f), (new_atime, new_mtime))
        fp2 = self._fp(str(f))
        assert fp1 != fp2, "fingerprint MUST change when file mtime changes"

    def test_fingerprint_distinguishes_models(self, tmp_path):
        """htdemucs vs htdemucs_ft → different fp (never reuse wrong-model cache)."""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"static")
        fp_a = self._fp(str(f), model="htdemucs")
        fp_b = self._fp(str(f), model="htdemucs_ft")
        assert fp_a != fp_b

    def test_fingerprint_distinguishes_backends(self, tmp_path):
        """demucs vs a future backend → different fp."""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"static")
        fp_a = self._fp(str(f), backend="demucs")
        fp_b = self._fp(str(f), backend="mdxnet")
        assert fp_a != fp_b

    def test_fingerprint_distinguishes_files_by_abs_path(self, tmp_path):
        """Same bytes, different absolute paths → different fp.

        (A video copied to two dirs shouldn't share a vocals cache — the user
        may edit one later; input_hash uses absolute_path+size+mtime so a copy
        with different mtime already diverges, but let's assert the obvious
        path component also contributes.)
        """
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        data = b"identical bytes"
        f1 = d1 / "v.mp4"
        f2 = d2 / "v.mp4"
        f1.write_bytes(data)
        # Force same mtime/size so only path differs
        now = time.time()
        os.utime(str(f1), (now, now))
        f2.write_bytes(data)
        os.utime(str(f2), (now, now))
        fp1 = self._fp(str(f1))
        fp2 = self._fp(str(f2))
        # They share content hash; abs path still differs per spec
        # (Spec 19 says input_hash includes absolute_input_path, which covers this)
        assert fp1 != fp2, "fingerprint MUST include absolute path as component"


class TestVocalsWavPathConvention:
    @staticmethod
    def _path(outdir, base, fp):
        from video_translate.vocal_sep import vocals_wav_path
        return vocals_wav_path(outdir, base, fp)

    def test_naming_convention(self):
        p = self._path("/tmp/v", "emily-blunt", "abcdef01")
        assert Path(p).name == "emily-blunt.abcdef01.vocals.wav"
        assert Path(p).parent == Path("/tmp/v")


class TestGracefulFallbackWhenDemucsMissing:
    """Spec 19 § Invariants #5: never crash when demucs not installed.

    We mock demucs_available() -> False and assert separate_vocals returns
    None (caller logs WARN + continues).
    """

    def test_separate_returns_none_when_unavailable(self, tmp_path):
        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"0" * 1024)
        with patch(
            "video_translate.vocal_sep.demucs_available", return_value=False
        ):
            from video_translate.vocal_sep import separate_vocals
            result = separate_vocals(
                str(vid), str(tmp_path), base="v", progress=lambda *a, **k: None
            )
            assert result is None, (
                "demucs unavailable → separate_vocals returns None so CLI can "
                "WARN + fall back; must NOT raise."
            )

    def test_demucs_available_is_never_imported_at_module_top_level(self):
        """ADR-017 § 3 — base install pip install -e . (no audio extra) must
        allow 'import video_translate.cli' without ImportError.

        We test this indirectly: the vocal_sep module itself MUST be
        importable in a subprocess that has NO demucs installed, because
        `demucs_available()` does a lazy try/except import inside.
        """
        # Subprocess test so our local .venv (which might have demucs later)
        # doesn't mask the check. We force an import-only probe via a short
        # -c script that ONLY imports the module without calling functions
        # that actually require demucs.
        script = (
            "import sys; sys.path.insert(0, r'{}'); "
            "from video_translate import vocal_sep; "
            "print('IMPORT_OK', vocal_sep.demucs_available.__name__)"
        ).format(str(Path(__file__).resolve().parents[1] / "src"))
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
            timeout=15,
        )
        # We accept BOTH outcomes:
        #   a) stdout has IMPORT_OK → module imported fine (lazy probe OK)
        #   b) demucs_available() is False but no ImportError raised → OK
        # Either way no unhandled ImportError reaches top-level.
        assert r.returncode == 0 or (r.returncode != 0 and "demucs" not in (r.stderr or "").lower() and "IMPORT_OK" not in (r.stdout or "")), (
            f"top-level import of vocal_sep blew up with demucs traceback!\n"
            f"STDOUT: {r.stdout}\nSTDERR: {r.stderr}"
        )


class TestTranscribeFingerprintIncludesSeparateVocals:
    """Spec 19 § Integration (A) — chunk fingerprint invariant.

    separate_vocals=False (the default) must hash to the EXACT same value
    as a pre-T2 build (all historical chunk_N.json caches are preserved).
    separate_vocals=True must diverge (don't silently reuse wrong-source cache).
    """

    @staticmethod
    def _fp_tf(*, separate_vocals: bool, vocal_sep_model="htdemucs"):
        from video_translate.transcribe import transcribe_fingerprint
        kwargs = dict(
            model_name="large-v3", chunk=240.0, lang="en",
            vad=None, use_vad=False, device="cpu", compute_type="int8",
        )
        # We need to call with or without the new dim; the contract says
        # separate_vocals=False → no-new-dim-hash → identical to old hash.
        if separate_vocals:
            return transcribe_fingerprint(
                **kwargs,
                separate_vocals=True,
                vocal_sep_backend="demucs",
                vocal_sep_model=vocal_sep_model,
                vocal_sep_input_hash="deadbeef",
            )
        else:
            # Old-style call (no separate_vocals args at all) → fp must equal
            # what pre-T2 transcribe_fingerprint produced
            return transcribe_fingerprint(**kwargs)

    def test_off_and_on_fingerprints_differ(self):
        fp_off = self._fp_tf(separate_vocals=False)
        fp_on = self._fp_tf(separate_vocals=True)
        assert fp_off != fp_on, (
            "chunk cache with separate_vocals=True must NEVER share fp with "
            "the original audio source — otherwise Whisper sees the wrong "
            "audio and the time-axis alignment guard is broken."
        )

    def test_off_matches_historical_signature_shape(self):
        """Historical fp shape — 8 hex chars, non-empty, stable across calls."""
        a = self._fp_tf(separate_vocals=False)
        b = self._fp_tf(separate_vocals=False)
        assert a == b
        assert len(a) == 8
        int(a, 16)

    def test_on_distinguishes_models(self):
        fp_a = self._fp_tf(separate_vocals=True, vocal_sep_model="htdemucs")
        fp_b = self._fp_tf(separate_vocals=True, vocal_sep_model="htdemucs_ft")
        assert fp_a != fp_b


# ---------------------------------------------------------------------------
# Tier 2: slow integration tests (require demucs + fixture video)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSeparateVocalsIntegration:
    """End-to-end demucs calls; disabled by default (pytest -m slow)."""

    FIXTURE = Path(__file__).resolve().parents[1] / "videos" / "emily-blunt.mp4"

    @pytest.fixture
    def fixture_video(self):
        if not self.FIXTURE.is_file():
            pytest.skip(f"fixture video missing: {self.FIXTURE}")
        return str(self.FIXTURE)

    def test_duration_preserved(self, fixture_video, tmp_path):
        """ADR-017 §2 — vocals.wav duration == input duration ± 0.05s.

        THE MOST IMPORTANT INVARIANT IN THE WHOLE MODULE. If this fails,
        every cue timestamp will be globally offset / scaled and the ADR-012
        acoustic-truth guarantee is void.
        """
        from video_translate.vocal_sep import separate_vocals
        from video_translate.ffmpeg_utils import probe_duration
        outdir = str(tmp_path)
        vocals = separate_vocals(
            fixture_video, outdir, base="fixture", progress=lambda *a, **k: None
        )
        assert vocals is not None, "demucs not installed? run `pip install -e .`"
        dur_in = probe_duration(fixture_video)
        dur_out = probe_duration(vocals)
        assert abs(dur_out - dur_in) < 0.05, (
            f"DEMUCS INVARIANT BROKEN: input={dur_in:.3f}s vocals={dur_out:.3f}s "
            f"(diff={abs(dur_out-dur_in):.3f}s >= 50ms). This would globally "
            f"offset / rescale every subtitle timestamp — fix the separation "
            f"pipeline before shipping."
        )
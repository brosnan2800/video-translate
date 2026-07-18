"""End-to-end smoke over the source video (marked slow; requires model + ffmpeg).

Skipped by default (`make test`). Run with `make test-all` on a machine that has
faster-whisper + the large-v3 model + the source video present.
"""
import os

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _video_path():
    for name in ("steveharvy-the apollo story.mp4",):
        for d in (os.path.join(REPO_ROOT, "videos"), os.path.dirname(REPO_ROOT)):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


@pytest.mark.slow
def test_full_pipeline_produces_four_files(tmp_path):
    video = _video_path()
    if video is None:
        pytest.skip("source video not found (place it under videos/)")
    pytest.importorskip("faster_whisper")
    pytest.importorskip("deep_translator")

    from video_translate.cli import EXIT_OK, main

    rc = main(["run", "--input", video, "--outdir", str(tmp_path),
               "--base", "apollo_story"])
    assert rc == EXIT_OK
    for suffix in (".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        assert os.path.exists(os.path.join(tmp_path, "apollo_story" + suffix))

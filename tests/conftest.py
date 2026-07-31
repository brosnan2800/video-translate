"""Shared pytest fixtures.

Golden data lives in docs/golden/ and is the validated apollo_story baseline.
Tests reference it read-only; the generate stage must reproduce it byte-exact.

docs/golden/ is git-ignored. To regenerate locally:
    .venv/bin/video-translate run videos/apollo_story.mp4 --engine google
    cp videos/apollo_story.segments_en.json docs/golden/
    cp videos/apollo_story.zh_segments.json docs/golden/
    # ... etc
"""
from __future__ import annotations

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(REPO_ROOT, "docs", "golden")
_GOLDEN_MISSING_MSG = (
    "docs/golden/ not found (git-ignored). "
    "Regenerate with the pipeline or skip golden tests: pytest -m 'not golden'"
)


@pytest.fixture(scope="session")
def golden_dir() -> str:
    if not os.path.isdir(GOLDEN_DIR):
        pytest.skip(_GOLDEN_MISSING_MSG)
    return GOLDEN_DIR


@pytest.fixture(scope="session")
def golden_segments_path(golden_dir: str) -> str:
    return os.path.join(golden_dir, "apollo_story.segments_en.json")


@pytest.fixture(scope="session")
def golden_zh_path(golden_dir: str) -> str:
    return os.path.join(golden_dir, "apollo_story.zh_segments.json")


@pytest.fixture(scope="session")
def golden_raw_segments_path(golden_dir: str) -> str:
    """V1 unmerged segments (the raw input to the merge stage)."""
    return os.path.join(golden_dir, "apollo_story.segments_raw.json")


@pytest.fixture(scope="session")
def golden_merged_segments_path(golden_dir: str) -> str:
    """Frozen output of merge_segments(golden_raw_segments_path)."""
    return os.path.join(golden_dir, "apollo_story.merged_segments.json")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture(scope="session")
def read_bytes():
    return _read_bytes

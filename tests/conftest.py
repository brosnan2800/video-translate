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


def pytest_configure(config) -> None:
    """ADR-029 / Spec 23 §2.1: 测试入口必须在项目 ``.venv`` 内。

    ``uv run pytest`` 只有在 pytest **已装进 .venv** 时才用项目环境；否则它
    **静默回退 PATH**（本机实测命中 ``F:\\Python311\\Scripts\\pytest.exe``）。
    而 plain ``uv sync`` 会剪掉 dev extra —— 于是「加了依赖顺手 sync 一下」
    就把测试挪到了另一个解释器上，还照常报绿。

    这里硬失败：宁可停在门口，也不要拿另一个环境的结果当结论。
    """
    from video_translate.toolchain import EntryDriftError, require_project_venv

    try:
        require_project_venv(action="pytest")
    except EntryDriftError as e:
        raise pytest.UsageError(str(e)) from e


@pytest.fixture(scope="session")
def read_bytes():
    return _read_bytes

"""Shared pytest fixtures.

Golden data lives in docs/golden/ and is the validated apollo_story baseline.
Tests reference it read-only; the generate stage must reproduce it byte-exact.
"""
from __future__ import annotations

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(REPO_ROOT, "docs", "golden")


@pytest.fixture(scope="session")
def golden_dir() -> str:
    return GOLDEN_DIR


@pytest.fixture(scope="session")
def golden_segments_path(golden_dir: str) -> str:
    return os.path.join(golden_dir, "apollo_story.segments_en.json")


@pytest.fixture(scope="session")
def golden_zh_path(golden_dir: str) -> str:
    return os.path.join(golden_dir, "apollo_story.zh_segments.json")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture(scope="session")
def read_bytes():
    return _read_bytes

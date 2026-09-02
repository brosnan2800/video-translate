"""JSON I/O helpers with safe defaults.

Fixes two problems in the original scripts: bare `json.load(open(...))` (leaked
file handles, no error context) and inconsistent encoding. All writes are atomic
(write to temp then rename) so a crash mid-write never corrupts an existing file
— important because the pipeline relies on resumable, incrementally-saved JSON.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def load_json(path: str) -> Any:
    """Load JSON from `path` (UTF-8).

    Raises:
        FileNotFoundError: if the file does not exist.
        json.JSONDecodeError: if the content is not valid JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_default(path: str, default: Any) -> Any:
    """Load JSON, returning `default` if the file is missing or corrupt.

    Used for resume checkpoints where a partial/corrupt file should not abort
    the whole run.
    """
    if not os.path.exists(path):
        return default
    try:
        return load_json(path)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: str, data: Any, *, indent: int | None = 2) -> None:
    """Atomically write `data` as UTF-8 JSON to `path`.

    Writes to a temp file in the same directory then os.replace() — guarantees
    the destination is never a half-written file. `ensure_ascii=False` keeps
    Chinese text human-readable on disk.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def write_text(path: str, text: str) -> None:
    """Atomically write UTF-8 text to `path`."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def tee_progress(log_path: str):
    """返回一个 progress 回调：stdout 打印 + 追加落盘（ADR-035 可观测性铁律）。

    用户翻 ``videos/`` 下的产物文件看结果、不看终端——``[audit]`` / ``[contract]``
    审计行必须同时落盘（如 ``<base>.review.log``）。落盘失败绝不阻断管道（仅打印）。
    """
    def _progress(msg: Any, *args: Any, **kwargs: Any) -> None:
        try:
            text = str(msg) % args if args else str(msg)
        except Exception:  # noqa: BLE001 - 格式化失败按原样输出
            text = str(msg)
        print(text, flush=True)
        try:
            directory = os.path.dirname(os.path.abspath(log_path))
            os.makedirs(directory, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text.rstrip("\n") + "\n")
        except Exception:  # noqa: BLE001 - 日志是增强，永不阻断
            pass
    return _progress

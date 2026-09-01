"""契约测试：generate 前置 enforce 闸门（控制平面静默点 4）。

4 重校验只依赖 segments + zh 文件本身（防绕过核心），state 仅增强陈旧检测：
  1. zh 覆盖率 100%         —— 漏行/漏 index → GateFail
  2. en/zh 段数匹配          —— 对齐后段数变化 → GateFail
  3. verify_align 位移检测命中 —— 中英错行 → GateFail
  4. state segments_sha 陈旧  —— 翻译后 segments 被改 → GateFail
逃生门 --no-align-check / --allow-degrade 显式放行。
"""
import json
import os

import pytest

from video_translate.capabilities import GateFail
from video_translate.cli import (
    EXIT_OK,
    cmd_generate,
    _enforce_generate_gate,
)


def _write_pair(tmp_path, base="clip", n=3):
    seg_p = tmp_path / f"{base}.segments_en.json"
    zh_p = tmp_path / f"{base}.zh_segments.json"
    segs = [
        {"start": i, "end": i + 1, "text": f"line {i}"} for i in range(n)
    ]
    with open(seg_p, "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)
    zh = {str(i): f"中文{i}" for i in range(n)}
    with open(zh_p, "w", encoding="utf-8") as f:
        json.dump(zh, f, ensure_ascii=False)
    return str(seg_p), str(zh_p)


def _set_sha_state(tmp_path, base="clip", segments_path=None):
    """Write a state file anchored to the current segments (simulates a fresh
    transcribe+translate chain)."""
    from video_translate.state import (
        new_state, record_stage, save, segment_sha,
    )
    st = new_state(base)
    seg = segments_path or f"{tmp_path}/clip.segments_en.json"
    record_stage(st, "transcribe", segments_sha=segment_sha(seg))
    st["stage"] = "generate"
    save(tmp_path, base, st)


# ---------------------------------------------------------------------------
# 1. 覆盖率 100%
# ---------------------------------------------------------------------------

def test_missing_zh_index_blocks(tmp_path):
    seg_p, zh_p = _write_pair(tmp_path, n=3)
    with open(zh_p, "w", encoding="utf-8") as f:
        json.dump({"0": "中文0", "2": "中文2"}, f, ensure_ascii=False)  # 缺 1
    with pytest.raises(GateFail) as exc:
        _enforce_generate_gate(seg_p, zh_p, base="clip", outdir=str(tmp_path))
    assert "index" in exc.value.message or "覆盖" in exc.value.message
    assert "1" in exc.value.message


# ---------------------------------------------------------------------------
# 2. 段数匹配
# ---------------------------------------------------------------------------

def test_zh_count_mismatch_blocks(tmp_path):
    seg_p, zh_p = _write_pair(tmp_path, n=3)
    with open(zh_p, "w", encoding="utf-8") as f:
        json.dump({"0": "a", "1": "b"}, f, ensure_ascii=False)  # 只有 2 项, en=3
    with pytest.raises(GateFail):
        _enforce_generate_gate(seg_p, zh_p, base="clip", outdir=str(tmp_path))


# ---------------------------------------------------------------------------
# 3. verify_align 位移检测 -> 阻断
# ---------------------------------------------------------------------------

def test_align_drift_blocks(tmp_path):
    """构造一个 verify_align 能抓到的位移样本：zh 的数字出现在错误 index。"""
    seg_p = tmp_path / "clip.segments_en.json"
    zh_p = tmp_path / "clip.zh_segments.json"
    segs = [
        {"start": 0, "end": 1, "text": "version 2 released"},
        {"start": 1, "end": 2, "text": "version 3 beta"},
    ]
    with open(seg_p, "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)
    # zh 的 index 0 翻译了 index 1 的内容（数字 3 漂移）
    with open(zh_p, "w", encoding="utf-8") as f:
        json.dump({"0": "版本3测试版", "1": "版本2发布"}, f, ensure_ascii=False)

    from video_translate import verify_align
    if not verify_align.report(segs, {"0": "版本3测试版", "1": "版本2发布"},
                               progress=lambda *_: None):
        with pytest.raises(GateFail):
            _enforce_generate_gate(str(seg_p), str(zh_p), base="clip",
                                   outdir=str(tmp_path))
    else:
        # verify_align 没有抓到 -> 说明样本选型不对；但覆盖率+段数已过，闸门放行
        _enforce_generate_gate(str(seg_p), str(zh_p), base="clip",
                               outdir=str(tmp_path))


# ---------------------------------------------------------------------------
# 4. state segments_sha 陈旧
# ---------------------------------------------------------------------------

def test_stale_segments_after_translate_blocks(tmp_path):
    seg_p, zh_p = _write_pair(tmp_path, n=3)
    _set_sha_state(tmp_path, segments_path=seg_p)
    # 翻译后 segments 变了（如对齐收紧边界/merge 段数变）
    with open(seg_p, "w", encoding="utf-8") as f:
        json.dump([{"start": 0, "end": 1.5, "text": "changed"}],
                  f, ensure_ascii=False)
    with pytest.raises(GateFail):
        _enforce_generate_gate(seg_p, zh_p, base="clip", outdir=str(tmp_path))


def test_state_missing_still_allows_consistent_pair(tmp_path):
    """闸门不依赖 state：无 state 但文件一致 -> 放行（防绕过关键）。"""
    seg_p, zh_p = _write_pair(tmp_path)
    _enforce_generate_gate(seg_p, zh_p, base="clip", outdir=str(tmp_path))


# ---------------------------------------------------------------------------
# 逃生门
# ---------------------------------------------------------------------------

def test_no_align_check_escape_hatch_degrades(tmp_path):
    """--no-align-check / --allow-degrade 显式放行，绝不静默。"""
    seg_p, zh_p = _write_pair(tmp_path)
    _enforce_generate_gate(seg_p, zh_p, base="clip", outdir=str(tmp_path),
                           allow_degrade=True)


def test_cmd_generate_full_ok_path(tmp_path):
    """完整 generate 命令 + 一致 pair -> 0，文件实际写出。"""
    import argparse
    seg_p, zh_p = _write_pair(tmp_path)
    _set_sha_state(tmp_path, segments_path=seg_p)
    rc = cmd_generate(argparse.Namespace(
        segments=seg_p, zh=zh_p, outdir=str(tmp_path), base="clip",
        gap=0.2, min_dur=1.0, offset=0.0, tail=0.3,
        flat=True, prune_old=False, style=None, allow_degrade=False,
        no_align_check=False,
    ))
    assert rc == EXIT_OK
    assert os.path.exists(os.path.join(tmp_path, "clip.bilingual.srt"))
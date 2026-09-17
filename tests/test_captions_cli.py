"""Spec 29 — `captions` 子命令接线测试。

**零真实网络**：通路预检与抓取在 `video_translate.ytcaptions` 源模块上被替换
（.clinerules/05：patch 源模块属性，不 patch 调用方内部引用）。

覆盖接线与退出语义：产物同契约 / 来源标记 / 缓存零网络 / 通路不通 → exit 8 /
无字幕 → 报错（绝不静默回退本地转写）/ `--list` 不落盘。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_translate import cli


def _snippets() -> list[dict]:
    return [
        {"text": "Hello there.", "start": 0.0, "duration": 1.0},
        {"text": "General Kenobi.", "start": 1.4, "duration": 1.0},
    ]


@pytest.fixture()
def no_net(monkeypatch):
    """抹平通路预检与真实抓取（本文件只测接线）。"""
    monkeypatch.setattr("video_translate.ytcaptions.preflight_network",
                        lambda proxy, timeout=5.0: None)
    monkeypatch.setattr(
        "video_translate.ytcaptions.fetch_transcript",
        lambda vid, langs, proxy=None, allow_auto=True: {
            "snippets": _snippets(), "track": "manual", "lang": "en"})


def _captions(tmp_path: Path, *extra: str) -> int:
    return cli.main(["captions", "dQw4w9WgXcQ", "--outdir", str(tmp_path),
                     "--base", "demo", "--no-proxy", *extra])


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

def test_captions_emits_segments_and_source_marker(tmp_path, no_net):
    assert _captions(tmp_path) == cli.EXIT_OK
    wd = tmp_path / "demo"
    segs = json.loads((wd / "demo.segments_en.json").read_text(encoding="utf-8"))
    assert segs and all({"start", "end", "text"} <= set(s) for s in segs)

    src = json.loads((wd / "demo.asr_source.json").read_text(encoding="utf-8"))
    assert src["kind"] == "youtube-captions"
    assert src["track"] == "manual"
    assert src["has_audio_reference"] is False      # ← verify 据此放行/标红


def test_captions_records_state_anchor(tmp_path, no_net):
    """必须落 state 的 segments_sha —— 否则 generate 的 stale-translation 闸会误判。"""
    assert _captions(tmp_path) == cli.EXIT_OK
    st = json.loads((tmp_path / "demo" / "demo.vt_state.json")
                    .read_text(encoding="utf-8"))
    assert st["stages"]["transcribe"]["status"] == "ok"
    assert st["stages"]["transcribe"].get("segments_sha")


def test_captions_accepts_youtube_url(tmp_path, no_net):
    rc = cli.main(["captions", "https://youtu.be/dQw4w9WgXcQ",
                   "--outdir", str(tmp_path), "--base", "demo", "--no-proxy"])
    assert rc == cli.EXIT_OK


# --------------------------------------------------------------------------- #
# 缓存：同视频重复调用零网络（ADR-042 D9）
# --------------------------------------------------------------------------- #

def test_second_run_hits_cache_without_network(tmp_path, no_net, monkeypatch):
    assert _captions(tmp_path) == cli.EXIT_OK
    assert (tmp_path / "demo" / "demo.captions_cache.json").is_file()

    def _boom(*a, **k):
        raise AssertionError("cache hit must not call fetch_transcript")

    monkeypatch.setattr("video_translate.ytcaptions.fetch_transcript", _boom)
    assert _captions(tmp_path) == cli.EXIT_OK


def test_refresh_forces_refetch(tmp_path, no_net, monkeypatch):
    assert _captions(tmp_path) == cli.EXIT_OK
    calls: list[str] = []
    monkeypatch.setattr(
        "video_translate.ytcaptions.fetch_transcript",
        lambda vid, langs, proxy=None, allow_auto=True: (
            calls.append(vid) or
            {"snippets": _snippets(), "track": "manual", "lang": "en"}))
    assert _captions(tmp_path, "--refresh") == cli.EXIT_OK
    assert calls == ["dQw4w9WgXcQ"]


# --------------------------------------------------------------------------- #
# 失败语义
# --------------------------------------------------------------------------- #

def test_unreachable_network_is_a_gate(tmp_path, monkeypatch):
    """通路不通 → GateFail → **exit 8**（门的语义；绝不静默改跑本地转写）。"""
    from video_translate.capabilities import GateFail

    def _blocked(proxy, timeout=5.0):
        raise GateFail("YouTube is unreachable", "配置 HTTP 代理")

    monkeypatch.setattr("video_translate.ytcaptions.preflight_network", _blocked)
    assert _captions(tmp_path) == cli.EXIT_GATE_FAIL
    # 失败不得留下半成品
    assert not (tmp_path / "demo" / "demo.segments_en.json").exists()


def test_no_transcript_reports_error_without_fallback(tmp_path, monkeypatch):
    """视频没有可用字幕 → 报错（exit 1），**不得**回退本地 Whisper（ADR-042 D2）。"""
    from video_translate.ytcaptions import CaptionUnavailable

    monkeypatch.setattr("video_translate.ytcaptions.preflight_network",
                        lambda proxy, timeout=5.0: None)

    def _none(vid, langs, proxy=None, allow_auto=True):
        raise CaptionUnavailable("no usable transcript", "用 --list 查看")

    monkeypatch.setattr("video_translate.ytcaptions.fetch_transcript", _none)
    assert _captions(tmp_path) == cli.EXIT_RUNTIME


def test_bad_url_is_usage_error(tmp_path):
    assert cli.main(["captions", "not-a-url", "--outdir", str(tmp_path),
                     "--no-proxy"]) == cli.EXIT_ARGS


# --------------------------------------------------------------------------- #
# --list：只列轨道，不落盘
# --------------------------------------------------------------------------- #

def test_list_tracks_writes_no_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("video_translate.ytcaptions.preflight_network",
                        lambda proxy, timeout=5.0: None)
    monkeypatch.setattr(
        "video_translate.ytcaptions.list_transcripts",
        lambda vid, proxy=None: [
            {"lang": "en", "language": "English", "track": "manual"},
            {"lang": "en", "language": "English", "track": "auto"},
        ])
    assert _captions(tmp_path, "--list") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "manual" in out and "auto" in out
    assert not (tmp_path / "demo").exists()          # 未创建任何产物目录

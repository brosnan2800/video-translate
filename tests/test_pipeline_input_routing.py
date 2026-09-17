"""ADR-043 / Spec 24 §6 — `pipeline` 的输入形态自动判定与分发。

**零真实网络**：接口型 ASR 通路的预检与抓取在 `video_translate.ytcaptions`
源模块上被替换（`.clinerules/05`：patch 源模块属性，不 patch 调用方内部引用）。

本文件钉住四条 load-bearing 断言：
  1. URL 输入**分发到接口型 ASR**，**绝不落入 `cmd_run`**（ADR-042 D1 / ADR-043 D4）；
  2. URL 输入**不触碰任何音频工具**（`analyze_audio` 一次都不许被调）—— 那等于
     拿 ffprobe 去打开一个 URL（ADR-043 D7）；
  3. 非 YouTube URL → `exit 2` + 指引，且**不落任何产物**（ADR-043 D3）；
  4. 本地路径分支**行为不变**（回归保护）。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from video_translate import cli

_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
_VID = "dQw4w9WgXcQ"


def _snippets() -> list[dict]:
    return [
        {"text": "Hello there.", "start": 0.0, "duration": 1.0},
        {"text": "General Kenobi.", "start": 1.4, "duration": 1.0},
    ]


@pytest.fixture()
def no_net(monkeypatch):
    monkeypatch.setattr("video_translate.ytcaptions.preflight_network",
                        lambda proxy, timeout=5.0: None)
    monkeypatch.setattr(
        "video_translate.ytcaptions.fetch_transcript",
        lambda vid, langs, proxy=None, allow_auto=True: {
            "snippets": _snippets(), "track": "manual", "lang": "en"})


# --------------------------------------------------------------------------- #
# 分发：URL → 接口型 ASR（且绝不落入 cmd_run）
# --------------------------------------------------------------------------- #

def test_url_dispatches_to_captions_not_run(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_run",
                        lambda a: calls.append("run") or cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_captions",
                        lambda a: calls.append("captions") or cli.EXIT_OK)
    monkeypatch.setattr(cli, "_url_engine_tail",
                        lambda *a, **k: calls.append("tail") or cli.EXIT_OK)

    rc = cli.main(["pipeline", _URL, "--outdir", str(tmp_path),
                   "--base", "demo", "--prompt", "never"])

    assert rc == cli.EXIT_OK
    assert calls == ["captions", "tail"]      # 取字幕 → 编排尾段；run 未参与


def test_local_path_dispatches_to_run(tmp_path, monkeypatch):
    """回归保护：本地路径仍走 `run`，不经接口型 ASR 通路。"""
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_run",
                        lambda a: calls.append("run") or cli.EXIT_OK)
    monkeypatch.setattr(cli, "cmd_captions",
                        lambda a: calls.append("captions") or cli.EXIT_OK)

    rc = cli.main(["pipeline", str(tmp_path / "a.mp4"), "--outdir", str(tmp_path),
                   "--base", "demo", "--prompt", "never"])

    assert rc == cli.EXIT_OK
    assert calls == ["run"]


def test_url_default_landing_matches_captions(tmp_path, monkeypatch):
    """URL 缺省落点 = `videos` / 视频 id —— 与 `captions` 逐字一致（ADR-043 D5），
    否则两条入口落到不同目录，幂等续跑与 captions_cache 复用都不成立。"""
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_captions",
                        lambda a: (seen.update(outdir=a.outdir, base=a.base),
                                   cli.EXIT_OK)[1])
    monkeypatch.setattr(cli, "_url_engine_tail", lambda *a, **k: cli.EXIT_OK)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["pipeline", _URL, "--prompt", "never"]) == cli.EXIT_OK
    assert seen == {"outdir": "videos", "base": _VID}


# --------------------------------------------------------------------------- #
# 端到端：URL 通路的真实接线（零网络）+ 绝不触碰音频
# --------------------------------------------------------------------------- #

def test_url_end_to_end_reaches_translate_stop_point(tmp_path, no_net, monkeypatch):
    """URL 输入应产出同契约段文件 + 来源标记，并停在**翻译停点**（exit 6）——
    与本地路径 `cmd_run` 的停点语义一致。"""
    def _no_audio(*a, **k):
        raise AssertionError("URL 输入绝不可触碰音频工具（ffprobe 打开 URL）")

    monkeypatch.setattr(cli, "cmd_run",
                        lambda a: (_ for _ in ()).throw(
                            AssertionError("URL 不可落入 cmd_run（ADR-042 D1）")))
    monkeypatch.setattr("video_translate.audio_profile.analyze_audio", _no_audio)

    rc = cli.main(["pipeline", _URL, "--outdir", str(tmp_path),
                   "--prompt", "never"])

    assert rc == cli.EXIT_AWAITING_AGENT
    wd = tmp_path / _VID
    segs = json.loads((wd / f"{_VID}.segments_en.json").read_text(encoding="utf-8"))
    assert segs and all({"start", "end", "text"} <= set(s) for s in segs)

    src = json.loads((wd / f"{_VID}.asr_source.json").read_text(encoding="utf-8"))
    assert src["kind"] == "youtube-captions"
    assert src["has_audio_reference"] is False

    # 翻译停点已 armed：task 已落盘（Agent 才有东西可读）
    assert (wd / f"{_VID}.translate_task.json").is_file()


# --------------------------------------------------------------------------- #
# 非 YouTube URL：明确报错（不静默当路径、不静默回退本地转写）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "https://example.com/abcdefghijk",       # 末段恰 11 字符：host 判定挡住
    "https://www.youtube.com/",              # 油管域名但没有视频 id
])
def test_unsupported_url_is_usage_error(tmp_path, url, capsys):
    rc = cli.main(["pipeline", url, "--outdir", str(tmp_path), "--prompt", "never"])

    assert rc == cli.EXIT_ARGS
    err = capsys.readouterr().err
    assert "YouTube" in err and "videos/<name>.mp4" in err
    assert not list(tmp_path.iterdir())      # 不落任何产物


def test_unsupported_url_message_is_not_the_local_one(tmp_path, capsys):
    """措辞必须点明「接口型 ASR 目前仅支持 YouTube」——
    不能让使用者以为是本地路径出了问题。"""
    cli.main(["pipeline", "https://vimeo.com/123456789",
              "--outdir", str(tmp_path), "--prompt", "never"])
    assert "仅支持 YouTube" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# URL 输入下的音频类参数：显式意图无法兑现 → 报错（ADR-038 裁决一）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", ["--vad", "--adaptive-vad", "--separate-vocals"])
def test_url_rejects_audio_only_flags(tmp_path, flag, capsys):
    rc = cli.main(["pipeline", _URL, "--outdir", str(tmp_path),
                   "--prompt", "never", flag])

    assert rc == cli.EXIT_ARGS
    err = capsys.readouterr().err
    assert flag in err and "不适用" in err
    assert not list(tmp_path.iterdir())


def test_url_allows_style_flag(tmp_path, monkeypatch):
    """`--style` 是**翻译风格**，与音频无关 —— 必须放行。"""
    monkeypatch.setattr(cli, "cmd_captions", lambda a: cli.EXIT_OK)
    monkeypatch.setattr(cli, "_url_engine_tail", lambda *a, **k: cli.EXIT_OK)

    assert cli.main(["pipeline", _URL, "--outdir", str(tmp_path),
                     "--prompt", "never", "--style", "literal"]) == cli.EXIT_OK


# --------------------------------------------------------------------------- #
# verify：URL 来源不传 --video（否则 acoustic-unavailable 永不生效）
# --------------------------------------------------------------------------- #

def _armed_for_verify(tmp_path: Path, base: str = "demo") -> Path:
    """造一个「已到 verify 阶段」的产物目录（segments / zh / srt 齐备）。"""
    wd = tmp_path / base
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{base}.segments_en.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "text": "hi"}]), encoding="utf-8")
    (wd / f"{base}.zh_segments.json").write_text(
        json.dumps({0: "你好"}), encoding="utf-8")
    (wd / f"{base}.bilingual.srt").write_text("1\n", encoding="utf-8")
    return wd


def test_url_verify_omits_video(tmp_path, monkeypatch):
    _armed_for_verify(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_verify",
                        lambda a: (seen.update(video=getattr(a, "video", None)),
                                   cli.EXIT_OK)[1])

    cli.main(["pipeline", _URL, "--outdir", str(tmp_path), "--base", "demo",
              "--prompt", "never"])

    assert seen["video"] is None      # 缺 --video + has_audio_reference=false
                                      # ⇒ cmd_verify 产出 acoustic-unavailable


def test_local_verify_still_passes_video(tmp_path, monkeypatch):
    """回归保护：本地路径照旧传 `--video`。"""
    _armed_for_verify(tmp_path)
    video = str(tmp_path / "a.mp4")
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_verify",
                        lambda a: (seen.update(video=getattr(a, "video", None)),
                                   cli.EXIT_OK)[1])

    cli.main(["pipeline", video, "--outdir", str(tmp_path), "--base", "demo",
              "--prompt", "never"])

    assert seen["video"] == video


# --------------------------------------------------------------------------- #
# 入口卫生校验：URL 豁免（ADR-043 D8）
# --------------------------------------------------------------------------- #

def test_hygiene_exempts_youtube_url_with_query():
    """`watch?v=<id>` 的 basename 含 `?` —— 不豁免则入口就 exit 2，
    输入形态判定根本无从执行（Spec 25 的既有缺陷）。"""
    assert cli._path_hygiene_error(argparse.Namespace(input=_URL)) is None
    assert cli._path_hygiene_error(
        argparse.Namespace(input="https://youtu.be/" + _VID + "?t=30")) is None


@pytest.mark.skipif(os.name != "nt", reason="`?` 只在 Windows 文件名里非法")
def test_hygiene_still_rejects_question_mark_in_real_filename():
    """豁免**只针对 URL 形态** —— 真实含 `?` 的文件名仍被拒（Spec 25 不回退）。"""
    assert cli._path_hygiene_error(
        argparse.Namespace(input=r"videos/a?b.mp4")) is not None


def test_hygiene_does_not_derive_base_from_url():
    """URL 输入不该由 `_default_base` 派生 base —— `watch?v=<id>` 会派生出含 `?`
    的 base 而误报（ADR-043 D8）。"""
    assert cli._path_hygiene_error(argparse.Namespace(input=_URL, base=None)) is None

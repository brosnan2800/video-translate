"""CLI 路径 / 文件名卫生校验（Spec 25）。

事故来源（ADR 规则 S3：修 bug 先写复现用例，用事故几何数据）：
    videos/角斗士采访.mp4 经 Windows PowerShell 5.1（ACP=936）传递后变成
    'videos/瑙掓枟澹\\ue0a6噰璁?mp4'。损坏的字符串一路流到
    io_utils.save_json() -> os.replace()，抛出
        OSError: [WinError 123] 文件名、目录名或卷标语法不正确
    堆栈指向 save_json，与病因（终端编码）无关，用户完全无法诊断。

Spec 25 要求：在 main() 入口当场拦截并给出可操作指引，返回 EXIT_ARGS(2)，
而不是让坏参数流到深栈崩溃。

跨平台硬约束：macOS 上 ':' / '?' 是合法文件名字符，Windows 非法字符集
**不得**误伤 Mac。层 1（编码损坏）与平台无关，两边一致。
"""
import os

import pytest

from video_translate.cli import (
    EXIT_ARGS,
    build_parser,
    find_broken_encoding_chars,
    find_illegal_filename_chars,
    main,
)

# 事故几何数据：'角斗士采访.mp4' 被 PS 5.1 (ACP=936) 按 GBK 解码后的真实形态。
#   - U+E0A6 落在 BMP 私用区（GBK 误解 UTF-8 的典型产物）
#   - '.' 被解成 '?'，导致 Path.stem 把 '...?mp4' 整个当成 stem
MOJIBAKE_BASENAME = "瑙掓枟澹\ue0a6噰璁?mp4"
MOJIBAKE_INPUT = f"videos/{MOJIBAKE_BASENAME}"


# --------------------------------------------------------------------------
# 层 1：编码损坏检测（跨平台一致）
# --------------------------------------------------------------------------

def test_mojibake_basename_is_flagged_as_broken_encoding():
    """事故数据必须被判为编码损坏。"""
    assert find_broken_encoding_chars(MOJIBAKE_BASENAME)


@pytest.mark.parametrize("name", [
    "角斗士采访",                 # 事故视频的真实中文名（未损坏）
    "Nobody Can Handle Christopher Walken's STRANGE Hum",
    "loyalty",
    "apollo_story",
    "IF",
])
def test_legit_names_are_not_flagged(name):
    """合法的非 ASCII 文件名（含中文）必须放行——不得误伤 Mac/Windows 正常用户。"""
    assert find_broken_encoding_chars(name) == []


@pytest.mark.parametrize("bad,label", [
    ("a\ue0a6b", "BMP 私用区（GBK 误解 UTF-8 的产物）"),
    ("a\ufffdb", "U+FFFD 替换字符"),
    ("a\ud800b", "未配对代理"),
    ("a\x07b", "C0 控制字符"),
    ("a\x85b", "C1 控制字符"),
    ("a\U000f0000b", "补充私用区"),
])
def test_each_broken_encoding_range_is_detected(bad, label):
    assert find_broken_encoding_chars(bad), label


# --------------------------------------------------------------------------
# 层 2：平台非法文件名字符（按平台取字符集）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ch", list('<>:"|?*'))
def test_windows_illegal_set_is_caught(ch):
    assert ch in find_illegal_filename_chars(f"a{ch}b", platform="nt")


def test_posix_allows_colon_and_question():
    """Mac 硬约束：':' 与 '?' 在 POSIX 合法，Windows 规则不得误伤 Mac。"""
    assert find_illegal_filename_chars("a:b", platform="posix") == []
    assert find_illegal_filename_chars("a?b", platform="posix") == []


def test_posix_still_rejects_path_separator():
    assert "/" in find_illegal_filename_chars("a/b", platform="posix")


def test_default_platform_follows_os_name(monkeypatch):
    """platform 省略时按 os.name 取字符集，Mac 上不套用 Windows 规则。"""
    monkeypatch.setattr(os, "name", "posix")
    assert find_illegal_filename_chars("a:b") == []
    monkeypatch.setattr(os, "name", "nt")
    assert ':' in find_illegal_filename_chars("a:b")


# --------------------------------------------------------------------------
# 入口拦截（Spec 25 §1 / §5）+ 端到端事故回归
# --------------------------------------------------------------------------

def _hygiene(args):
    from video_translate.cli import _path_hygiene_error
    return _path_hygiene_error(args)


def test_pipeline_rejects_mojibake_input_with_exit_args(capsys):
    """事故回归：不再抛 OSError WinError 123，而是 EXIT_ARGS + 可读指引。"""
    rc = main(["pipeline", MOJIBAKE_INPUT])
    assert rc == EXIT_ARGS
    err = capsys.readouterr().err
    assert "[args]" in err
    assert "Fix" in err


def test_pipeline_rejects_mojibake_without_touching_the_filesystem(capsys):
    """拦截必须发生在任何产物写入之前——不得留下半个 vt_state.json。"""
    main(["pipeline", MOJIBAKE_INPUT])
    capsys.readouterr()
    assert not os.path.exists("videos/" + MOJIBAKE_BASENAME + ".vt_state.json")


def test_run_rejects_mojibake_input(capsys):
    rc = main(["run", MOJIBAKE_INPUT])
    assert rc == EXIT_ARGS
    assert "[args]" in capsys.readouterr().err


def test_explicit_base_is_validated_too():
    """显式 --base 同样要校验（不能只拦派生 base）。"""
    args = build_parser().parse_args([
        "generate",
        "--segments", "a.segments_en.json",
        "--zh", "a.zh_segments.json",
        "--outdir", "videos",
        "--base", "bad\ue0a6name",
    ])
    assert _hygiene(args)


def test_legit_cjk_video_path_passes_hygiene(tmp_path):
    """Mac/Windows 上正常的中文视频路径必须放行（不得误伤）。"""
    video = tmp_path / "角斗士采访.mp4"
    video.write_bytes(b"")
    args = build_parser().parse_args(["pipeline", str(video)])
    assert _hygiene(args) is None


def test_legit_cjk_segments_paths_pass_hygiene(tmp_path):
    segs = tmp_path / "角斗士采访.segments_en.json"
    zh = tmp_path / "角斗士采访.zh_segments.json"
    segs.write_text("[]", encoding="utf-8")
    zh.write_text("{}", encoding="utf-8")
    args = build_parser().parse_args([
        "generate",
        "--segments", str(segs),
        "--zh", str(zh),
        "--outdir", str(tmp_path),
    ])
    assert _hygiene(args) is None


def test_ascii_paths_stay_clean(tmp_path):
    """既有 ASCII 路径（golden 回归等）零影响。"""
    video = tmp_path / "apollo_story.mp4"
    video.write_bytes(b"")
    args = build_parser().parse_args(["pipeline", str(video)])
    assert _hygiene(args) is None


def test_windows_drive_spec_outdir_is_not_flagged():
    """盘符根不是文件名：'C:' 的 ':' 不得被当成非法文件名字符误报。

    Review 发现的边界：`--outdir C:\\` 经 basename 得到 'C:'，若不跳过就会
    被 Windows 非法集拦下 ':'，把合法路径误判为非法。
    """
    args = build_parser().parse_args([
        "generate",
        "--segments", "a.segments_en.json",
        "--zh", "a.zh_segments.json",
        "--outdir", "C:\\",
    ])
    assert _hygiene(args) is None


def test_root_outdir_is_not_flagged():
    """'/' 是路径根，basename 为空，跳过校验。"""
    args = build_parser().parse_args([
        "generate",
        "--segments", "a.segments_en.json",
        "--zh", "a.zh_segments.json",
        "--outdir", "/",
    ])
    assert _hygiene(args) is None

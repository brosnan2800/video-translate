"""ADR-035 M1 — artifacts 契约表自检与校验器单测（TDD，表落地前先红后绿）。"""
from __future__ import annotations

import os

import pytest

from video_translate import state
from video_translate.artifacts import (
    CONFIDENCE_FIELDS,
    ContractViolation,
    artifact_file,
    artifact_path,
    enforce_contract,
    get_artifact,
    table_problems,
    validate_artifact,
)
from video_translate.pipeline_def import STAGE_ORDER


# --------------------------------------------------------------------------- #
# 表自检：唯一性 / stage id / 模板 / 字段声明
# --------------------------------------------------------------------------- #

def test_table_self_consistent():
    assert table_problems() == []


def test_stage_order_single_source():
    """state.STAGE_ORDER 必须来自 pipeline_def（ADR-035 命名单一来源）。"""
    assert state.STAGE_ORDER == STAGE_ORDER


def test_segments_raw_carry_is_confidence_fields():
    spec = get_artifact("segments_raw")
    assert spec["carry"] == CONFIDENCE_FIELDS
    assert spec["recompute"] is False
    assert "transcribe" in spec["consumed_by"]


def test_core_artifacts_registered():
    for aid in ("audio_profile", "routing", "segments_raw", "segments",
                "review", "zh", "srt", "audio_source"):
        spec = get_artifact(aid)  # 未登记会 KeyError
        assert spec["id"] == aid


def test_unknown_artifact_rejected():
    with pytest.raises(KeyError):
        get_artifact("no_such_thing")


# --------------------------------------------------------------------------- #
# 命名 / 定位
# --------------------------------------------------------------------------- #

def test_artifact_path_joins_outdir_and_base():
    assert artifact_path("segments", "videos", "IF") == os.path.join(
        "videos", "IF.segments_en.json")
    assert artifact_path("segments_raw", "videos", "IF") == os.path.join(
        "videos", "IF.segments_raw.json")


def test_artifact_path_fmt_placeholders():
    assert artifact_file("detected_lang", "IF", fp="737e1df9") == \
        "IF.737e1df9.detected_lang.json"


def test_state_embedded_artifact_has_no_file():
    with pytest.raises(KeyError):
        artifact_file("audio_profile", "IF")
    with pytest.raises(KeyError):
        artifact_file("routing", "IF")


# --------------------------------------------------------------------------- #
# 校验器
# --------------------------------------------------------------------------- #

def test_validate_missing_required():
    problems = validate_artifact("segments", [{"start": 1.0, "end": 2.0}])
    assert any("missing required field 'text'" in p for p in problems)


def test_validate_ok():
    assert validate_artifact(
        "segments", [{"start": 1.0, "end": 2.0, "text": "hi"}]) == []


def test_validate_carry_opt_in():
    seg = {"start": 1.0, "end": 2.0, "text": "hi"}
    # 默认不查 carry（合并视图等合法缺 carry 的场景）
    assert validate_artifact("segments_raw", [seg]) == []
    # require_carry 才查（raw 生产侧 / 契约测试用）
    problems = validate_artifact("segments_raw", [seg], require_carry=True)
    assert len(problems) == len(CONFIDENCE_FIELDS)


def test_validate_raw_full_carry_pass():
    seg = {"start": 1.0, "end": 2.0, "text": "hi",
           **{f: 1 for f in CONFIDENCE_FIELDS}}
    assert validate_artifact("segments_raw", [seg], require_carry=True) == []


# --------------------------------------------------------------------------- #
# 边界校验：默认 loud warn 不阻断，strict 才升级 GateFail（铁律④⑤）
# --------------------------------------------------------------------------- #

def test_enforce_contract_default_warns_only(capsys):
    problems = enforce_contract("segments", [{"start": 1.0}])
    assert problems
    assert "[contract] segments" in capsys.readouterr().out


def test_enforce_contract_strict_raises_contract_violation():
    with pytest.raises(ContractViolation):
        enforce_contract("segments", [{"start": 1.0}], strict=True)


def test_enforce_contract_clean_is_silent(capsys):
    ok = enforce_contract("segments", [{"start": 1.0, "end": 2.0, "text": "x"}])
    assert ok == []
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# 指纹声明 + 审计落盘
# --------------------------------------------------------------------------- #

def test_fingerprint_inputs_declared_for_cache_owners():
    from video_translate.artifacts import FINGERPRINT_INPUTS, artifact_ids
    assert set(FINGERPRINT_INPUTS) <= set(artifact_ids())
    for aid in ("chunk_cache", "align_cache", "review", "vocals_wav"):
        assert aid in FINGERPRINT_INPUTS, f"{aid} 必须声明指纹输入"


def test_review_log_artifact_registered():
    spec = get_artifact("review_log")
    assert spec["file"] == "{base}.review.log"


def test_tee_progress_prints_and_persists(tmp_path, capsys):
    from video_translate.io_utils import tee_progress
    log = tmp_path / "clip.review.log"
    p = tee_progress(str(log))
    p("[audit] hello %s", "world")
    assert "[audit] hello world" in capsys.readouterr().out
    assert log.read_text(encoding="utf-8").strip() == "[audit] hello world"


def test_tee_progress_bad_log_path_never_raises(capsys):
    from video_translate.io_utils import tee_progress
    # 非法路径（Windows 保留名/不存在盘符）落盘失败只打印，绝不阻断
    p = tee_progress("Z:\\\\no\\\\such\\\\dir\\\\a.log" if os.name == "nt"
                     else "/proc/no/such/a.log")
    p("[audit] still prints")
    assert "[audit] still prints" in capsys.readouterr().out

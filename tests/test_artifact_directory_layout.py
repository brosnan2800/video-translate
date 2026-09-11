"""ADR-037 产物目录布局契约测试。

断言 ``workdir = <outdir>/<base>`` 是全部产物的唯一落点基准，且
``artifact_path`` 的产物一律落在 workdir 内。
"""
import os

from video_translate.artifacts import (
    artifact_file,
    artifact_ids,
    artifact_path,
    workdir,
)


def test_workdir_is_outdir_join_base():
    assert workdir("videos", "if") == os.path.join("videos", "if")


def test_artifact_path_resolves_under_workdir():
    p = artifact_path("segments", "videos", "if")
    assert p == os.path.join(workdir("videos", "if"), "if.segments_en.json")
    assert p.startswith(workdir("videos", "if"))


def test_every_standalone_artifact_path_lives_in_workdir():
    for aid in artifact_ids():
        try:
            fname = artifact_file(aid, "if")
        except KeyError:
            # 内嵌 vt_state.json 的产物无独立文件，跳过
            continue
        p = artifact_path(aid, "videos", "if")
        assert p == os.path.join(workdir("videos", "if"), fname), aid

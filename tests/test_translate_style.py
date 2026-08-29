"""TDD red tests for the translation style tracks (Spec 21 / ADR-027).

Covers:
- STYLE_PERSONAS matrix integrity
- prepare_translate_task(style=...) writes version 3 + style field + correct persona
- golden: no-style task persona is byte-identical to DEFAULT_PERSONA (backward compat)
- multi-style emits N suffixed task files
"""
import json
import os

from video_translate.config import STYLE_PERSONAS, VALID_STYLES
from video_translate.translate import (
    DEFAULT_PERSONA,
    prepare_translate_task,
)


def _write_segments(path, texts):
    segs = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(texts)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)


def test_style_matrix_has_three_tracks():
    assert set(STYLE_PERSONAS.keys()) == set(VALID_STYLES)
    for style in VALID_STYLES:
        sd = STYLE_PERSONAS[style]
        assert sd.persona.strip(), f"{style} persona empty"
        assert sd.guidelines, f"{style} guidelines empty"


def test_style_guidelines_differ():
    g = {s: tuple(STYLE_PERSONAS[s].guidelines) for s in VALID_STYLES}
    assert g["film"] != g["literal"] != g["bilingual_study"] != g["film"]


def test_default_film_persona_is_legacy_default():
    # film 轨基底必须等同于既有 DEFAULT_PERSONA（字节级兼容）
    assert STYLE_PERSONAS["film"].persona == DEFAULT_PERSONA


def test_prepare_task_default_is_version3_film(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    task = os.path.join(tmp_path, "task.json")
    _write_segments(seg, ["hello"])
    prepare_translate_task(seg, task)
    data = json.load(open(task, encoding="utf-8"))
    assert data["version"] == 3
    assert data["style"] == "film"
    # 默认 persona 字节等同于非 style 时代（向后兼容）
    assert data["persona"] == DEFAULT_PERSONA


def test_prepare_task_literal_style(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    task = os.path.join(tmp_path, "task.json")
    _write_segments(seg, ["hello"])
    prepare_translate_task(seg, task, style="literal")
    data = json.load(open(task, encoding="utf-8"))
    assert data["version"] == 3
    assert data["style"] == "literal"
    assert data["persona"] == STYLE_PERSONAS["literal"].persona
    # literal 不应携带 film 的口语化守则
    assert data["persona"] != DEFAULT_PERSONA


def test_prepare_task_explicit_persona_overrides_style(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    task = os.path.join(tmp_path, "task.json")
    _write_segments(seg, ["hello"])
    custom = "自定义译者人设：逐字直译"
    prepare_translate_task(seg, task, persona=custom, style="film")
    data = json.load(open(task, encoding="utf-8"))
    assert data["style"] == "film"
    assert custom in data["persona"]
    assert data["persona"].startswith(custom)


def test_prepare_task_multistyle_emits_suffixed_files(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    _write_segments(seg, ["hello"])
    styles = ["film", "literal"]
    written = prepare_translate_task(seg, None, outdir=str(tmp_path),
                                     base="clip", styles=styles)
    assert written is not None
    for style in styles:
        p = os.path.join(tmp_path, f"clip.{style}.translate_task.json")
        assert os.path.exists(p), f"missing task for style {style}"
        data = json.load(open(p, encoding="utf-8"))
        assert data["style"] == style

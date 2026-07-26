"""Tests for glossary loading (Spec 14, ADR-010).

A glossary maps source terms to preferred Chinese translations so the same name
is rendered consistently across episodes. ``load_glossary`` returns a formatted
context string for the translation persona, or None when missing/empty/unparseable.
"""
import json

from video_translate.glossary import load_glossary


def test_load_txt_arrow(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("Iron Man => 钢铁侠\nCaptain => 美国队长\n", encoding="utf-8")
    out = load_glossary(str(p))
    assert out is not None
    assert "- Iron Man => 钢铁侠" in out
    assert "- Captain => 美国队长" in out


def test_load_txt_colon(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("Iron Man: 钢铁侠\n", encoding="utf-8")
    out = load_glossary(str(p))
    assert "- Iron Man => 钢铁侠" in out


def test_load_json(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"Iron Man": "钢铁侠", "Hulk": "绿巨人"}), encoding="utf-8")
    out = load_glossary(str(p))
    assert "- Iron Man => 钢铁侠" in out
    assert "- Hulk => 绿巨人" in out


def test_comments_and_blanks_ignored(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("# comment\n\nIron Man => 钢铁侠\n   \n", encoding="utf-8")
    out = load_glossary(str(p))
    assert out is not None
    assert out.count("=>") == 1


def test_empty_glossary_returns_none(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("# only comments\n\n", encoding="utf-8")
    assert load_glossary(str(p)) is None


def test_missing_file_returns_none():
    assert load_glossary("/no/such/file.txt") is None


def test_none_path_returns_none():
    assert load_glossary(None) is None


def test_malformed_json_returns_none(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_glossary(str(p)) is None

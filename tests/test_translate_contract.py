"""Contract tests for translate (Spec 03). Uses an injected translate_fn — no
network, no deep_translator, no proxy required."""
import json
import os

from video_translate.translate import translate_segments


def _write_segments(path, texts):
    segs = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(texts)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)


def test_translate_all_with_injected_fn(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    out = os.path.join(tmp_path, "zh.json")
    _write_segments(seg, ["hello", "world"])

    result = translate_segments(seg, out, translate_fn=lambda t: t.upper())
    assert result == {"0": "HELLO", "1": "WORLD"}
    # persisted to disk
    assert json.load(open(out, encoding="utf-8")) == {"0": "HELLO", "1": "WORLD"}


def test_translate_resume_skips_done(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    out = os.path.join(tmp_path, "zh.json")
    _write_segments(seg, ["a", "b", "c"])
    # pre-seed checkpoint: index 0 already done
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"0": "DONE"}, f)

    calls = []

    def fn(t):
        calls.append(t)
        return t + "!"

    result = translate_segments(seg, out, translate_fn=fn)
    assert result == {"0": "DONE", "1": "b!", "2": "c!"}
    assert calls == ["b", "c"]  # index 0 skipped


def test_translate_failure_goes_to_pending(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    out = os.path.join(tmp_path, "zh.json")
    pend = os.path.join(tmp_path, "pending.json")
    _write_segments(seg, ["ok", "boom"])

    def fn(t):
        if t == "boom":
            raise RuntimeError("engine down")
        return "OK"

    result = translate_segments(seg, out, pending_path=pend, translate_fn=fn)
    assert result == {"0": "OK"}
    pending = json.load(open(pend, encoding="utf-8"))
    assert len(pending) == 1 and pending[0]["index"] == 1
    assert pending[0]["text"] == "boom"


def test_translate_writes_empty_pending_when_all_ok(tmp_path):
    seg = os.path.join(tmp_path, "seg.json")
    out = os.path.join(tmp_path, "zh.json")
    pend = os.path.join(tmp_path, "pending.json")
    _write_segments(seg, ["x"])
    translate_segments(seg, out, pending_path=pend, translate_fn=lambda t: t)
    assert json.load(open(pend, encoding="utf-8")) == []

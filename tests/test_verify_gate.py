"""Gap B smoke tests — Phase 4 半自动硬闸门。

覆盖 GAPB_IMPLEMENTATION.md §6 的 4 项 smoke test，外加 checkpoint 对
.vt_verify_gate.json 的闸门消费。无需真实视频：用 monkeypatch 模拟 verify 报告。

  T1 uncovered-audio 自动修，retry=1 转绿 → 退 0
  T2 语义命中立即半自动，retry=0 → 退 3
  T3 声学持续超限 retry=2 熔断 → 退 3，breaker_tripped=True
  T4 全绿 → 退 0
"""
import json

import pytest

import gates.checkpoint as ck
import gates.verify_gate as vg


@pytest.fixture
def videos_tmp(tmp_path, monkeypatch):
    d = tmp_path / "videos"
    d.mkdir()
    # 把 gate-kit 两个模块的产物目录重定向到临时目录，避免触碰真实 videos/
    monkeypatch.setattr(vg, "VIDEOS", d)
    monkeypatch.setattr(ck, "VIDEOS", d)
    monkeypatch.setattr(ck, "CKPT", str(d / ".vt_checkpoint.json"))
    return d


def _write_report(d, base, report):
    (d / f"{base}.verify_report.json").write_text(json.dumps(report), encoding="utf-8")


def _gate(d, base):
    return json.loads((d / f"{base}.vt_verify_gate.json").read_text(encoding="utf-8"))


def _acoustic_only():
    return {"acoustic": {"uncovered_audio": [{"start": 12.0, "end": 18.5}]},
            "content": {}, "presentation": {}}


def _clean():
    return {"acoustic": {"uncovered_audio": []}, "content": {}, "presentation": {}}


def test_T4_clean(videos_tmp, monkeypatch):
    base = "x"
    monkeypatch.setattr(vg, "run_repo_verify",
                        lambda b, v: _write_report(videos_tmp, b, _clean()) or 0)
    rc = vg.run_gate(base, "v.mp4")
    assert rc == vg.EXIT_OK
    assert _gate(videos_tmp, base)["breached"] is False


def test_T2_semantic_half_auto(videos_tmp, monkeypatch):
    base = "x"
    monkeypatch.setattr(vg, "run_repo_verify",
                        lambda b, v: _write_report(videos_tmp, b, {
                            "acoustic": {"uncovered_audio": []},
                            "content": {"semantic_reread": {"fidelity": 0.4, "breached": True}},
                            "presentation": {},
                        }) or 0)
    rc = vg.run_gate(base, "v.mp4", auto_loop=True)
    assert rc == vg.EXIT_GATE_VIOLATION
    g = _gate(videos_tmp, base)
    assert g["lane"] == "semantic"
    assert g["retry"] == 0
    assert g["mode"] == "half_auto"


def test_T1_acoustic_autofix_green(videos_tmp, monkeypatch):
    base = "x"
    calls = {"n": 0}

    def fake_verify(b, v):
        calls["n"] += 1
        report = _acoustic_only() if calls["n"] == 1 else _clean()
        _write_report(videos_tmp, b, report)
        return 0

    monkeypatch.setattr(vg, "run_repo_verify", fake_verify)
    monkeypatch.setattr(vg, "auto_fix", lambda *a, **k: None)  # 模拟已修好
    rc = vg.run_gate(base, "v.mp4", auto_loop=True)
    assert rc == vg.EXIT_OK
    g = _gate(videos_tmp, base)
    assert g["breached"] is False
    assert g["retry"] == 1


def test_T3_breaker(videos_tmp):
    base = "x"
    # --simulate-unfixable：classify 恒定返回 acoustic，验证 retry 上限熔断
    rc = vg.run_gate(base, "v.mp4", auto_loop=True, simulate_unfixable=True)
    assert rc == vg.EXIT_GATE_VIOLATION
    g = _gate(videos_tmp, base)
    assert g["retry"] == vg.MAX_RETRY
    assert g["breaker_tripped"] is True
    assert g["mode"] == "half_auto"


def test_checkpoint_verify_blocks_breached(videos_tmp, monkeypatch):
    base = "x"
    (videos_tmp / f"{base}.vt_verify_gate.json").write_text(json.dumps({
        "base": base, "breached": True, "mode": "half_auto", "lane": "acoustic",
        "auto_fixable": True, "retry": 2, "max_retry": 2, "breaker_tripped": True,
    }))
    ck.save({"stages": {"generate": {"status": "completed"}}})
    (videos_tmp / f"{base}.bilingual.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    with pytest.raises(SystemExit) as exc:
        ck.complete("verify", base)
    assert exc.value.code == 3
    # 人工审批后放行
    ck.approve("verify")
    ck.complete("verify", base)  # 不应再抛 SystemExit


def test_checkpoint_verify_allows_clean(videos_tmp, monkeypatch):
    base = "x"
    (videos_tmp / f"{base}.vt_verify_gate.json").write_text(json.dumps({
        "base": base, "breached": False,
    }))
    ck.save({"stages": {"generate": {"status": "completed"}}})
    (videos_tmp / f"{base}.bilingual.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    ck.complete("verify", base)  # 不应抛 SystemExit

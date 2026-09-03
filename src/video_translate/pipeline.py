"""Pipeline engine — control plane S2 (§3). The ONLY module that knows how to
advance the state machine declared in ``pipeline_def.STAGES``.

Responsibilities (hard cap ~400 lines; overflow goes back into the declarative
table or the capability layer):
  - ``build_ctx``         locate the per-base artifacts on disk
  - ``check_stage``       requires + caps + registered gate -> problems list
  - ``enforce``           check_stage with teeth (raises capabilities.GateFail)
  - ``resolve_position``  where am I / what's next (state first, artifacts as
                          the independent fallback — gates never depend on state)
  - ``next_action``       T8/ADR-033: pure decision for the ``pipeline``
                          advancer (done / decision point / translate stop /
                          transcribe / generate / verify)
  - ``render_next``       the NEXT block appended by run/generate/verify, with
                          a machine-parseable ``--json`` twin
Hard-stop mechanics stay in the stage executors themselves (generate/verify own
their gates); this engine reports and advises, it does not re-implement gates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from . import state as vt_state
from .artifacts import artifact_path
from .capabilities import GateFail, probe as _cap_probe
from .pipeline_def import STAGES, stage

# ctx keys the stages may require -> (description, how-to-repair)
_CTX_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "video": ("source video file", "re-run `video-translate run <video>`"),
    "segments": (
        "<base>.segments_en.json",
        "run transcription: `video-translate run <video>`",
    ),
    "zh": (
        "<base>.zh_segments.json",
        "agent translation: read <base>.translate_task.json, write "
        "<base>.zh_segments.json with 100% index coverage",
    ),
    "srt": (
        "<base>.bilingual.srt",
        "run `video-translate generate --segments ... --zh ...`",
    ),
}


# ---------------------------------------------------------------------------
# Gates (registered checks evaluated by check_stage; executors keep their own
# hard implementations — these mirror them for advance reporting)
# ---------------------------------------------------------------------------

def _gate_zh_covers_segments(ctx: dict[str, Any]) -> list[str]:
    """Mirror of the generate/verify content gate: 100% coverage + 1:1 count."""
    problems: list[str] = []
    seg_path, zh_path = ctx.get("segments"), ctx.get("zh")
    if not seg_path or not zh_path:
        return problems  # missing artifacts are reported by `requires`
    from .io_utils import load_json
    from .translate import validate_zh
    try:
        ok, _missing = validate_zh(seg_path, zh_path)
        if not ok:
            problems.append(
                "zh coverage < 100% — translate every index in "
                "<base>.translate_task.json before generating")
    except Exception as exc:  # noqa: BLE001 - unreadable inputs are a problem
        problems.append(f"cannot validate zh coverage: {exc}")
        return problems
    try:
        segments = load_json(seg_path)
        zh = load_json(zh_path)
        if len(zh) != len(segments):
            problems.append(
                f"segment count mismatch: en={len(segments)} vs zh={len(zh)} "
                "(post-alignment re-segmentation? re-translate after aligning)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cannot compare segment counts: {exc}")
    return problems


GATES: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "zh_covers_segments": _gate_zh_covers_segments,
}


# ---------------------------------------------------------------------------
# Artifact location
# ---------------------------------------------------------------------------

def _find_srt(outdir: str | Path, base: str) -> str | None:
    """Locate the generated bilingual SRT (flat legacy or versioned subdir)."""
    root = Path(outdir)
    candidates = [root / f"{base}.bilingual.srt"]
    sub = root / base
    if sub.is_dir():
        candidates.extend(sorted(sub.glob(f"{base}*.bilingual.srt")))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def build_ctx(outdir: str | Path, base: str,
              video: str | None = None) -> dict[str, Any]:
    """Collect the per-base artifact paths into a context dict for the table."""
    root = Path(outdir)
    return {
        "outdir": str(root),
        "base": base,
        "video": video,
        # ADR-035 M1: 产物路径查契约表（与原字符串逐字一致，零行为变化）
        "segments": artifact_path("segments", root, base),
        "zh": artifact_path("zh", root, base),
        "srt": _find_srt(outdir, base),
    }


def _exists(ctx_key_path: str | None) -> bool:
    return bool(ctx_key_path) and os.path.isfile(ctx_key_path)


# ---------------------------------------------------------------------------
# Stage checking
# ---------------------------------------------------------------------------

def check_stage(stage_id: str, ctx: dict[str, Any], *,
                caps_probe: Callable[[str], bool] = _cap_probe,
                with_gate: bool = True) -> list[str]:
    """All problems that would block entering ``stage_id`` (empty == clear)."""
    spec = stage(stage_id)
    problems: list[str] = []
    for key in spec["requires"]:
        if not _exists(ctx.get(key)):
            desc, fix = _CTX_REQUIREMENTS.get(key, (key, ""))
            problems.append(f"missing {key} ({desc}) — {fix}")
    for name in spec["caps"]:
        if not caps_probe(name):
            from .capabilities import CAPS
            guidance = next((c.guidance for c in CAPS if c.name == name), "")
            problems.append(f"capability '{name}' unavailable — {guidance}")
    if with_gate and spec["gate"]:
        gate_fn = GATES.get(spec["gate"])
        if gate_fn is None:
            problems.append(f"gate '{spec['gate']}' is not registered")
        else:
            problems.extend(gate_fn(ctx))
    return problems


def enforce(stage_id: str, ctx: dict[str, Any], **kw: Any) -> None:
    """check_stage with teeth: raise GateFail(exit 8) on any blocking problem."""
    problems = check_stage(stage_id, ctx, **kw)
    if problems:
        title = stage(stage_id)["title"]
        detail = "\n".join(f"  - {p}" for p in problems)
        raise GateFail(
            f"stage '{stage_id}' ({title}) is blocked",
            f"{detail}\nFix the item(s) above, or pass the explicit escape "
            f"hatch (--allow-degrade / --no-strict) where documented.")


# ---------------------------------------------------------------------------
# Position resolution: artifacts decide completion; state only adds signals
# ---------------------------------------------------------------------------

def _stage_done(stage_id: str, ctx: dict[str, Any],
                st: dict[str, Any]) -> bool:
    spec = stage(stage_id)
    if spec["produces"]:
        return _exists(ctx.get(spec["produces"]))
    if stage_id == "preflight":
        return True  # nothing to prove for an existing base
    if stage_id == "verify":
        return vt_state.stage_status(st, "verify").get("status") == "ok"
    return False


def resolve_position(ctx: dict[str, Any],
                     st: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where the pipeline stands and what the single next action is."""
    if st is None:
        st = vt_state.load(ctx["outdir"], ctx["base"])
    current = STAGES[0]["id"]
    for s in STAGES:
        current = s["id"]
        if not _stage_done(s["id"], ctx, st):
            break
    done = all(_stage_done(s["id"], ctx, st) for s in STAGES)
    verify_status = vt_state.stage_status(st, "verify").get("status")
    pending_agent = (current == "translate") or (
        verify_status == "pending_agent")
    problems = [] if done else check_stage(current, ctx, with_gate=False)
    nxt = None if done else {
        "stage": current,
        "title": stage(current)["title"],
        "cli": stage(current)["cli"],
        "stop_point": stage(current)["stop_point"],
        "why": ("agent translation pending (stop point A)"
                if current == "translate" else
                "pipeline stage not finished yet"),
        "pending_agent": pending_agent,
        "blocked_by": problems,
    }
    return {
        "base": ctx["base"],
        "outdir": ctx["outdir"],
        "current_stage": None if done else current,
        "done": done,
        "pending_agent": pending_agent,
        "next_action": nxt,
        "artifacts": {k: (ctx.get(k) if _exists(ctx.get(k)) else None)
                      for k in ("video", "segments", "zh", "srt")},
        "verify_status": verify_status,
    }


# ---------------------------------------------------------------------------
# T8 / ADR-033 / Spec 24: the idempotent advancer's pure decision function
# ---------------------------------------------------------------------------

def next_action(pos: dict[str, Any], *, prompt_mode: str,
                routing: dict[str, Any] | None) -> str:
    """Pure decision for ``cmd_pipeline`` (Spec 24 §1): zero I/O.

    Returns one of: ``done | stop_decision_point | transcribe |
    stop_translate | generate | verify``.

    Args:
        pos: output of :func:`resolve_position` (only ``done`` and
            ``current_stage`` are read).
        prompt_mode: ``always`` | ``never`` | ``require-profile``
            (Config.prompt; explicit CLI flags already folded into
            ``routing`` by the caller).
        routing: the persisted ``decisions.routing`` value, or ``None`` when
            no decision has been made yet.
    """
    if pos.get("done"):
        return "done"
    cur = pos.get("current_stage")
    if cur == "translate":
        return "stop_translate"
    if cur == "generate":
        return "generate"
    if cur == "verify":
        return "verify"
    # transcribe (or a fresh base): the decision point applies only on the
    # first pass — any persisted routing (explicit OR profile) means a
    # decision was already made (T8: 重跑见 routing 已存在即续 transcribe).
    # require-profile never stops here; its hard gate lives in cmd_run
    # (--require-profile -> _resolve_routing -> exit 8).
    if prompt_mode == "always" and routing is None:
        return "stop_decision_point"
    return "transcribe"


# ---------------------------------------------------------------------------
# Rendering (NEXT block for run/generate tails + `status` output)
# ---------------------------------------------------------------------------

def render_next(pos: dict[str, Any], *, as_json: bool = False) -> str:
    """Human NEXT block, or a machine-parseable JSON blob with ``as_json``."""
    if as_json:
        return json.dumps(pos, ensure_ascii=False, indent=2)
    lines: list[str] = []
    if pos["done"]:
        lines.append("[NEXT] pipeline complete — bilingual SRT verified.")
        if pos.get("verify_status") == "pending_agent":
            lines.append("  (semantic reread still pending: write "
                          "<base>.semantic_reread_result.json)")
        return "\n".join(lines)
    nxt = pos["next_action"]
    lines.append(f"[NEXT] stage={nxt['stage']}"
                 + ("  (STOP POINT — awaiting agent)"
                    if nxt["stop_point"] else ""))
    lines.append(f"  do : {nxt['cli']}")
    for p in nxt["blocked_by"]:
        lines.append(f"  !! : {p}")
    lines.append(f"  status: video-translate status --base {pos['base']} "
                 f"--outdir {pos['outdir']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-discovery (status without --base)
# ---------------------------------------------------------------------------

def discover_bases(outdir: str | Path) -> list[str]:
    """Bases with transcription artifacts, newest segments file first."""
    root = Path(outdir)
    hits = sorted(root.glob("*.segments_en.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    seen: list[str] = []
    for p in hits:
        base = p.name[: -len(".segments_en.json")]
        if base not in seen:
            seen.append(base)
    return seen

"""Per-base pipeline state (<base>.vt_state.json) — control plane §2.1.

The control plane replaces *implicit* progress conventions (file existence,
agent memory) with an explicit, per-base state file. Design:

  - One file per video base, co-located with ``<base>.segments_en.json``.
  - ``decisions`` record each P0 choice with its origin (explicit / default):
    an explicit decision must be *honored* (裁决一); a default may degrade but
    the resolved value + reason are recorded here.
  - ``stages`` carry the ``segments_sha`` fingerprint chain: transcribe records
    the sha it produced; translate records the sha it translated against;
    ``generate`` compares them to hard-stop stale translations (the 40→42
    segment-boundary drift root cause).
  - Gates NEVER depend on this file: generate/verify hard checks only the
    segments + zh files themselves; state merely *enhances* the gates with
    staleness detection. Deleting the state file never disables a gate.

The architecture-reorg branch's three state files (``.preflight_decision`` /
``.vt_checkpoint`` / ``.vt_verify_gate``) are absorbed into the ``decisions`` /
``stages`` / ``verify`` sections of this single file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .io_utils import load_json_default, save_json

# ADR-035 命名单一来源：阶段顺序唯一定义在 pipeline_def.STAGES，这里只引用，
# 不再维护第二份元组（此前两套 stage id 各自为政，属契约收编对象）。
from .pipeline_def import STAGE_ORDER
from .artifacts import workdir

# ADR-035 M2: schema v2 —— 顶层新增声学事实数据段 audio_profile（duration /
# silence_intervals 等，preflight 单一生产、全链只算一次）与 audio_source
# （vocals.wav 显式记录）。v1 文件由 load() 兼容迁移（仅升版本号，不丢内容）。
SCHEMA_VERSION = 2


def state_path(outdir: str | Path, base: str) -> Path:
    """``<outdir>/<base>.vt_state.json`` — co-located with the segments file."""
    return Path(workdir(outdir, base)) / f"{base}.vt_state.json"


def segment_sha(path: str | Path) -> str:
    """Content fingerprint (``sha256:…``) of a segments file.

    The chain anchor: transcribe records what it wrote, translate records what
    it saw, generate compares — a mismatch means the translation is stale.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def new_state(base: str, *, video: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "base": base,
        "stage": STAGE_ORDER[0],
        "decisions": {},
        "stages": {},
    }
    if video:
        state["video"] = video
    return state


def load(outdir: str | Path, base: str) -> dict[str, Any]:
    """Load the state file, returning ``{}`` when missing/corrupt/schema-mismatch.

    Gates tolerate a missing state (they do not depend on it); a *wrong-schema*
    file is treated as absent and rebuilt from artifacts.
    """
    path = state_path(outdir, base)
    raw = load_json_default(str(path), None)
    if not isinstance(raw, dict):
        return {}
    return _migrate_schema(raw)


def _migrate_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """旧 schema 兼容迁移（ADR-035 M2）：v1 仅升版本号（decisions/stages 原样
    保留，ADR-031 D6 的决策不丢）；未知/未来 schema 当缺失（防漂移，走既有的
    从产物重建路径）。"""
    v = raw.get("schema_version")
    if v == SCHEMA_VERSION:
        return raw
    if isinstance(v, int) and 1 <= v < SCHEMA_VERSION:
        migrated = dict(raw)
        migrated["schema_version"] = SCHEMA_VERSION
        return migrated
    return {}


def save(outdir: str | Path, base: str, state: dict[str, Any]) -> str:
    """Atomically persist the state file; returns its path."""
    state = dict(state)
    state.setdefault("schema_version", SCHEMA_VERSION)
    path = str(state_path(outdir, base))
    Path(workdir(outdir, base)).mkdir(parents=True, exist_ok=True)
    save_json(path, state, indent=2)
    return path


# ---------------------------------------------------------------------------
# Mutators (control plane writes these at each transition)
# ---------------------------------------------------------------------------

def current_stage(state: dict[str, Any]) -> str:
    return state.get("stage", STAGE_ORDER[0])


def set_stage(state: dict[str, Any], stage: str) -> None:
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown pipeline stage {stage!r} (not in {STAGE_ORDER})")
    state["stage"] = stage


def record_decision(
    state: dict[str, Any],
    key: str,
    value: Any,
    *,
    origin: str = "default",
    resolved: Any = None,
    reason: str | None = None,
) -> None:
    """Record a P0 decision with its intent grading (裁决一).

    ``origin="explicit"`` means the caller must be honored (else GateFail);
    ``origin="default"`` may degrade but the reason is recorded here.
    """
    entry: dict[str, Any] = {"value": value, "origin": origin}
    if resolved is not None:
        entry["resolved"] = resolved
    if reason is not None:
        entry["reason"] = reason
    state.setdefault("decisions", {})[key] = entry


def record_stage(
    state: dict[str, Any],
    stage: str,
    *,
    status: str = "ok",
    **fields: Any,
) -> None:
    """Record a stage's outcome (sha / n_segments / engine / coverage …)."""
    entry: dict[str, Any] = {"status": status}
    entry.update(fields)
    state.setdefault("stages", {})[stage] = entry


def stage_status(state: dict[str, Any], stage: str) -> dict[str, Any]:
    return state.get("stages", {}).get(stage, {"status": "pending"})


# ---------------------------------------------------------------------------
# Artifact inference (compat with pre-control-plane outputs)
# ---------------------------------------------------------------------------

def infer_stage(outdir: str | Path, base: str) -> str:
    """Infer the current pipeline stage from on-disk artifacts alone.

    Works for old directories that never had a state file: the deepest finished
    stage decides where we are.
    """
    dirp = Path(workdir(outdir, base))
    if (dirp / f"{base}.bilingual.srt").is_file():
        return "verify"  # generate done; verify is next
    if (dirp / f"{base}.zh_segments.json").is_file():
        return "generate"
    if (dirp / f"{base}.segments_en.json").is_file():
        return "translate"
    return "preflight"


def rebuild_state(
    outdir: str | Path,
    base: str,
    *,
    video: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a state dict from existing artifacts (no file written).

    Called when a directory has no valid state file: the gates stay intact
    because they re-derive everything they need from the segments+zh files.
    """
    state = new_state(base, video=video)
    state["stage"] = infer_stage(outdir, base)
    dirp = Path(workdir(outdir, base))
    seg = dirp / f"{base}.segments_en.json"
    if seg.is_file():
        record_stage(state, "transcribe", segments_sha=segment_sha(seg))
    zh = dirp / f"{base}.zh_segments.json"
    if zh.is_file():
        recorded_sha = segment_sha(seg) if seg.is_file() else None
        record_stage(state, "translate", segments_sha=recorded_sha)
    if (dirp / f"{base}.bilingual.srt").is_file():
        record_stage(state, "generate")
    return state


def state_needs_rebuild(outdir: str | Path, base: str) -> bool:
    """True when the state file is absent or its sha anchor is missing."""
    st = load(outdir, base)
    if not st:
        return True
    seg = Path(workdir(outdir, base)) / f"{base}.segments_en.json"
    if seg.is_file():
        ts = st.get("stages", {}).get("transcribe", {})
        if ts.get("segments_sha") != segment_sha(seg):
            return True
    return False


def ensure_state(
    outdir: str | Path,
    base: str,
    *,
    video: str | None = None,
) -> dict[str, Any]:
    """Return the current state, rebuilding + persisting it from artifacts when
    it is missing or its fingerprint anchor is stale (run --skip transcribe on
    an old directory self-heals into the state chain).

    ADR-031 D6: the rebuild heuristic fires on ANY segments-file change —
    including a *legitimate* resegment amendment — which used to wipe the
    recorded ``decisions`` (agent/user P0 choices with origin grading;
    kathy_meta_vlog ended with ``decisions == {}`` after its first resegment).
    Decisions (and the ``video`` path) are therefore carried over into the
    rebuilt state instead of being lost.
    """
    existing = load(outdir, base)
    if not state_needs_rebuild(outdir, base):
        return existing
    prior_video = None
    prior_decisions: dict[str, Any] | None = None
    prior_verify_attempts: int | None = None   # ADR-031 D8: preserve during rebuild
    if isinstance(existing, dict) and existing:
        prior_video = existing.get("video")
        dec = existing.get("decisions")
        if isinstance(dec, dict) and dec:
            prior_decisions = dec
        v = existing.get("stages", {}).get("verify", {})
        if isinstance(v, dict):
            prior_verify_attempts = v.get("attempts")
    st = rebuild_state(outdir, base, video=video or prior_video)
    if prior_decisions:
        st["decisions"] = prior_decisions
    if prior_verify_attempts is not None:
        st.setdefault("stages", {}).setdefault("verify", {})["attempts"] = prior_verify_attempts
    save(outdir, base, st)
    return st


# ---------------------------------------------------------------------------
# ADR-031 D8: verify retry counting (circuit-breaker)
# ---------------------------------------------------------------------------

MAX_VERIFY_ATTEMPTS = 2  # ADR-031 D8: 2 repair cycles, then circuit-breaker.


def increment_verify_attempts(outdir: str | Path, base: str) -> int:
    """Increment and persist ``stages.verify.attempts``; returns new value.

    Called at the top of ``cmd_verify``. The counter is part of *state*
    (enhancement only); the real gate still comes from segments+zh files.
    """
    st = ensure_state(outdir, base)
    st.setdefault("stages", {}).setdefault("verify", {})
    st["stages"]["verify"]["attempts"] = (
        st["stages"]["verify"].get("attempts", 0) + 1
    )
    save(outdir, base, st)
    return st["stages"]["verify"]["attempts"]


def reset_verify_attempts(outdir: str | Path, base: str) -> None:
    """Reset ``stages.verify.attempts`` to 0 (called by ``run`` / full pipeline).

    Local re-runs (``resegment``, ``generate``) must **not** reset — only a
    fresh ``run`` counts as a new translation task.
    """
    st = ensure_state(outdir, base)
    st.setdefault("stages", {}).setdefault("verify", {})
    st["stages"]["verify"]["attempts"] = 0
    save(outdir, base, st)


# ---------------------------------------------------------------------------
# ADR-032: P0 audio-profile snapshot + P0 -> P1 routing decision
# ---------------------------------------------------------------------------
# The P0 -> P1 hand-off used to live only in prose (AGENTS.md), so a skipped
# profile left no trace and `run` silently fell back to a bare run. Persisting
# both the recommendation and the final routing makes the hand-off auditable and
# lets `run` recover the route even when the decision point was bypassed.

AUDIO_PROFILE_KEY = "audio_profile"
ROUTING_KEY = "routing"


def _decision_value(
    outdir: str | Path, base: str, key: str
) -> dict[str, Any] | None:
    """Return the persisted ``decisions.<key>`` value, or None when absent."""
    st = load(outdir, base)
    entry = st.get("decisions", {}).get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return value if isinstance(value, dict) else None


def record_audio_profile(
    outdir: str | Path,
    base: str,
    recommendation: Any,
) -> dict[str, Any]:
    """Persist the P0 profile recommendation into ``decisions.audio_profile``.

    Args:
        recommendation: an ``AudioProfileRecommendation`` (anything exposing
            ``to_dict()``); a plain mapping is accepted as-is.

    Returns:
        The persisted payload.
    """
    data = (recommendation.to_dict() if hasattr(recommendation, "to_dict")
            else dict(recommendation))
    st = ensure_state(outdir, base)
    record_decision(st, AUDIO_PROFILE_KEY, data, origin="profile")
    save(outdir, base, st)
    return data


def get_audio_profile(outdir: str | Path, base: str) -> dict[str, Any] | None:
    """Return the persisted audio-profile recommendation, or None."""
    return _decision_value(outdir, base, AUDIO_PROFILE_KEY)


def record_routing(
    outdir: str | Path,
    base: str,
    *,
    style: str,
    vad: bool,
    adaptive_vad: bool,
    separate_vocals: bool,
    vad_threshold: float | None = None,
    origin: str = "profile",
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist the final three routing decisions into ``decisions.routing``.

    ``origin`` records who decided: ``"profile"`` when the Agent fell back to the
    audio-profile recommendation (decision-point timeout or a bypassed decision
    point), ``"explicit"`` when the user picked at the decision point or passed
    an explicit CLI flag.
    """
    value: dict[str, Any] = {
        "style": style,
        "vad": bool(vad),
        "adaptive_vad": bool(adaptive_vad),
        "separate_vocals": bool(separate_vocals),
        "vad_threshold": vad_threshold,
    }
    st = ensure_state(outdir, base)
    record_decision(st, ROUTING_KEY, value, origin=origin, reason=reason)
    save(outdir, base, st)
    return value


def get_routing(outdir: str | Path, base: str) -> dict[str, Any] | None:
    """Return the persisted routing decision, or None when never recorded."""
    return _decision_value(outdir, base, ROUTING_KEY)


# ---------------------------------------------------------------------------
# ADR-035 M2: 声学事实数据段（artifact id "audio_profile"，单一生产 = preflight）
# ---------------------------------------------------------------------------
# 与 decisions.audio_profile（ADR-032 的 P0 推荐决策）不同：这里是 analyze_audio
# 的原始声学事实（duration / 静音区间 / 电平），全链只算一次落盘，transcribe /
# verify 读它，禁止重算（artifacts.ARTIFACTS["audio_profile"].recompute = False）。

AUDIO_DATA_KEY = "audio_profile"
AUDIO_SOURCE_KEY = "audio_source"


def record_acoustics(
    outdir: str | Path,
    base: str,
    prof: Any,
    *,
    noise: str = "-30dB",
    d: float = 0.3,
) -> dict[str, Any] | None:
    """把 analyze_audio 的声学事实落盘（生产者唯一入口）。

    探测参数 ``noise`` / ``d`` 是 artifact 身份的一部分：消费者（verify）仅在
    参数一致时复用，避免"用 -30dB 的缓存回应 -25dB 的提问"。probe 失败返回
    None，不写 state。
    """
    if prof is None or not getattr(prof, "ok", False):
        return None
    from .audio_profile import _silence_fraction

    duration = getattr(prof, "duration", None)
    silences = [tuple(iv) for iv in (prof.silence_intervals or [])]
    data: dict[str, Any] = {
        "duration": duration,
        "mean_db": prof.mean_vol,
        "max_db": prof.max_vol,
        "silence_fraction": (_silence_fraction(silences, duration)
                             if duration else None),
        "silence_intervals": silences,
        "noise": noise,
        "d": d,
    }
    st = ensure_state(outdir, base)
    st[AUDIO_DATA_KEY] = data
    save(outdir, base, st)
    return data


def get_acoustics(outdir: str | Path, base: str) -> dict[str, Any] | None:
    """读取已落盘的声学事实；缺失/损坏返回 None（永不阻断，铁律⑤）。"""
    data = load(outdir, base).get(AUDIO_DATA_KEY)
    return data if isinstance(data, dict) else None


def record_audio_source(
    outdir: str | Path,
    base: str,
    *,
    vocals_wav: str,
    vsep_backend: str | None = None,
    vsep_model: str | None = None,
    vsep_input_hash: str | None = None,
) -> dict[str, Any]:
    """显式记录 T2 人声分离产物关联（artifact "audio_source"）。

    此前下游靠 ``separate_fingerprint`` 反推 vocals.wav 归属，路径不在任何
    状态里——现在显式落盘，resegment / verify / fill_gaps 直接读。
    """
    data: dict[str, Any] = {
        "vocals_wav": vocals_wav,
        "vsep_backend": vsep_backend,
        "vsep_model": vsep_model,
        "vsep_input_hash": vsep_input_hash,
    }
    st = ensure_state(outdir, base)
    st[AUDIO_SOURCE_KEY] = data
    save(outdir, base, st)
    return data


def get_audio_source(outdir: str | Path, base: str) -> dict[str, Any] | None:
    data = load(outdir, base).get(AUDIO_SOURCE_KEY)
    return data if isinstance(data, dict) else None

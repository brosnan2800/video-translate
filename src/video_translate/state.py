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

SCHEMA_VERSION = 1

# The linear pipeline the control plane enforces. Extra stages (e.g. a future
# OCR burn-in) extend this tuple; set_stage validates against it.
STAGE_ORDER: tuple[str, ...] = (
    "preflight",
    "transcribe",
    "translate",
    "generate",
    "verify",
)


def state_path(outdir: str | Path, base: str) -> Path:
    """``<outdir>/<base>.vt_state.json`` — co-located with the segments file."""
    return Path(outdir) / f"{base}.vt_state.json"


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
    if raw.get("schema_version") != SCHEMA_VERSION:
        return {}
    return raw


def save(outdir: str | Path, base: str, state: dict[str, Any]) -> str:
    """Atomically persist the state file; returns its path."""
    state = dict(state)
    state.setdefault("schema_version", SCHEMA_VERSION)
    path = str(state_path(outdir, base))
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
    dirp = Path(outdir)
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
    dirp = Path(outdir)
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
    seg = Path(outdir) / f"{base}.segments_en.json"
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
    an old directory self-heals into the state chain)."""
    if not state_needs_rebuild(outdir, base):
        return load(outdir, base)
    st = rebuild_state(outdir, base, video=video)
    save(outdir, base, st)
    return st
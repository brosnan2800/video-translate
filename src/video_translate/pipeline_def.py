"""Declarative pipeline definition — control plane S2 (§3).

STAGES is PURE DATA: it describes what the pipeline looks like (stages, their
required inputs, capabilities, gates) but contains zero execution logic.
``pipeline.py`` is the only module that interprets this table; ``cli.py`` only
renders its verdicts. Keeping the table declarative means the state machine can
be audited (and re-ordered) without touching engine code.

Each stage:
  id          stable identifier, shared with state.STAGE_ORDER
  title       human-readable name for status / NEXT blocks
  requires    ctx keys that must exist BEFORE the stage can run
              ("video" = source file, "segments" = <base>.segments_en.json,
               "zh" = <base>.zh_segments.json)
  caps        capability ids (capabilities.CAPS) that must be available
  gate        optional registered check id (pipeline.GATES) evaluated before
              entering the stage; failing problems block with guidance
  produces    ctx key of the artifact this stage writes ("" for none)
  cli         how the stage is executed (CLI command or agent instruction)
  stop_point  True for the agent/human collaboration stops (停点 A 翻译)
"""

from __future__ import annotations

from typing import Any

STAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "preflight",
        "title": "环境自检 (doctor)",
        "requires": [],
        "caps": [],
        "gate": None,
        "produces": "",
        "cli": "uv run video-translate doctor",
        "stop_point": False,
    },
    {
        "id": "transcribe",
        "title": "转写 (Whisper + 断句合并 + 漏音补洞)",
        "requires": ["video"],
        "caps": ["ffmpeg", "ffprobe", "model:large-v3"],
        "gate": None,
        "produces": "segments",
        "cli": "uv run video-translate run <video>",
        "stop_point": False,
    },
    {
        "id": "translate",
        "title": "Agent 翻译 (停点 A)",
        "requires": ["segments"],
        "caps": [],
        "gate": None,
        "produces": "zh",
        "cli": "(agent) 翻译 <base>.translate_task.json -> <base>.zh_segments.json (100% 覆盖全部 index)",
        "stop_point": True,
    },
    {
        "id": "generate",
        "title": "字幕生成 (enforce 闸门内置)",
        "requires": ["segments", "zh"],
        "caps": ["ffmpeg"],
        "gate": "zh_covers_segments",
        "produces": "srt",
        "cli": "uv run video-translate generate --segments ... --zh ... --outdir ... --base ...",
        "stop_point": False,
    },
    {
        "id": "verify",
        "title": "三 Lane 门禁 (声学/内容/表现/语义)",
        "requires": ["segments", "zh", "video"],
        "caps": ["ffmpeg", "ffprobe"],
        "gate": "zh_covers_segments",
        "produces": "",
        "cli": "uv run video-translate verify --segments ... --zh ... --video ...",
        "stop_point": False,
    },
)

_STAGE_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in STAGES}


def stage(stage_id: str) -> dict[str, Any]:
    return _STAGE_BY_ID[stage_id]


def stage_ids() -> tuple[str, ...]:
    return tuple(s["id"] for s in STAGES)

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
  caps        **通用** capability ids (capabilities.CAPS `common=True`) that must
              be available — 引擎特定前置由 Provider 自报注入 ctx（ADR-038 D7）
  cli_by_source  可选：按 ctx["_source"] 覆盖 `cli` 串（ADR-043 D2 —— 同一阶段
              在不同输入形态下命令不同，但产出与阶段语义不变）
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
        "title": "环境自检 + 音频画像 + 决策点 (doctor --video)",
        "requires": [],
        "caps": [],
        "gate": None,
        "produces": "",
        "cli": "uv run video-translate doctor --video <video>",
        "stop_point": False,
    },
    {
        "id": "transcribe",
        "title": "转写 (Whisper + 断句合并 + 漏音补洞)",
        "requires": ["video"],
        # ADR-038 D7：本表只声明**通用**前置。引擎特定前置（model:<name> / cuda /
        # whisperx / demucs）由当前 Provider 的 prerequisites() 自报，在
        # pipeline.build_ctx 组装 ctx 时注入（`_engine_caps`）——换 ASR 引擎
        # 不需要改这张表。
        "caps": ["ffmpeg", "ffprobe"],
        "gate": None,
        "produces": "segments",
        # ADR-043 D2：本阶段有**两种输入形态**，产出同为一个 `segments`（契约切点
        # 不变），只是取法不同。表仍是纯数据 —— 按 ctx["_source"] 选串由
        # `pipeline.stage_cli` 完成，执行分支在 `cli.cmd_pipeline`。
        "cli": "uv run video-translate run <video>",
        "cli_by_source": {
            "url": "uv run video-translate captions <url>",
        },
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
        # ADR-042 D7：`video` 不再列为**硬前置** —— 接口型 ASR 方案本就没有本地
        # 音频，此时 verify 仍应运行（内容 / 表现 lane 与几何子检查都不依赖音频），
        # 由 `acoustic-unavailable` issue 如实标注「依赖音频的声学检查未执行」。
        "requires": ["segments", "zh"],
        "caps": ["ffmpeg", "ffprobe"],
        "gate": "zh_covers_segments",
        "produces": "",
        "cli": "uv run video-translate verify --segments ... --zh ... [--video ...]",
        "stop_point": False,
    },
)

_STAGE_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in STAGES}


def stage(stage_id: str) -> dict[str, Any]:
    return _STAGE_BY_ID[stage_id]


def stage_ids() -> tuple[str, ...]:
    return tuple(s["id"] for s in STAGES)


# ADR-035 命名单一来源：阶段顺序即 STAGES 的 id 序。state.STAGE_ORDER 由此引用，
# 不再维护第二份元组（此前两套 stage id 各自为政，属契约收编对象）。
STAGE_ORDER: tuple[str, ...] = stage_ids()

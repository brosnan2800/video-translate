"""ADR-035 — 流水线数据契约总线：声明式 artifacts 表（唯一事实来源）。

阶段间传什么数据，不再散落在各模块的文件名字符串与字段猜测里，全部登记在这张
纯数据表。命名、定位、校验一律查表。五条铁律见
``docs/adr/035-pipeline-data-contract.md`` §2.2（单一生产 / copy-then-override /
命名单一来源 / 边界校验 / state 永不做 gate）。

本模块是纯数据 + 纯函数：无 I/O、无模型、无 ffmpeg，可被全量单测覆盖。
"""
from __future__ import annotations

import os
from typing import Any, TypedDict

from .pipeline_def import STAGE_ORDER


class Artifact(TypedDict):
    """一份流水线数据的契约条目（声明式，纯数据）。"""

    id: str                       # 唯一规范名（全项目唯一）
    file: str                     # 文件名模板（{base} 占位）；空串 = 内嵌 vt_state.json
    fields: tuple[str, ...]       # 全部已知字段（命名单一来源）
    required: tuple[str, ...]     # 写入时必须存在的结构字段（validate 依据）
    carry: tuple[str, ...]        # 必须穿透下游变换的字段（Z2 下经 _raw_indices 回查）
    produced_by: str              # 唯一生产者 stage id（STAGE_ORDER 之一）
    consumed_by: tuple[str, ...]  # 消费者 stage id
    recompute: bool               # False = 禁止下游重算


# 段级置信度字段：必须穿透下游变换、永不聚合/覆盖（ADR-035 §2.3 Z2）。
# merge 合并段时丢弃它们正是本契约要杜绝的事故（G1/G3 与 review 信号 A）。
# 注：verify 曾有一道「低置信道」读同一批字段并参与 strict gate，该道已由 ADR-041
# 整体移除（用 ASR 自评字段巡检 ASR 产物属「自证」，非独立验证）；本 carry 契约保留，
# 因为①层 review 的信号 A 仍依赖它。
CONFIDENCE_FIELDS: tuple[str, ...] = (
    "no_speech_prob", "avg_logprob", "compression_ratio",
)

# ADR-040 单行不变量：`segments_raw[].text` / `segments[].text` / `zh` 的每个 value
# 都是**单行文本**（不含 \r / \n）。内容换行由 `text_utils.to_single_line` 在写入
# 边界（transcribe / translate / generate）压平；cue 的多行只能来自 `srt_utils.block`
# 的 lines（中英分行 / display-merge 折行），不能来自内容本身。


ARTIFACTS: tuple[Artifact, ...] = (
    # ---- 内嵌 vt_state.json 的全局/运行级参数（file 为空 = 不落独立文件） ----
    {
        "id": "audio_profile",
        "file": "",
        "fields": ("duration", "mean_db", "max_db", "silence_fraction",
                   "silence_intervals"),
        "required": (),
        "carry": (),
        "produced_by": "preflight",
        "consumed_by": ("transcribe", "verify"),
        "recompute": False,
    },
    {
        "id": "routing",
        "file": "",
        "fields": ("style", "vad", "adaptive_vad", "separate_vocals",
                   "vad_threshold"),
        "required": (),
        "carry": (),
        "produced_by": "preflight",
        "consumed_by": ("transcribe",),
        "recompute": False,
    },
    {
        "id": "audio_source",
        "file": "",
        "fields": ("vocals_wav", "vsep_backend", "vsep_model", "vsep_input_hash"),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("transcribe", "verify"),
        "recompute": False,
    },
    {
        "id": "verify_result",
        "file": "",
        "fields": ("status", "attempts", "issues", "align_drift"),
        "required": (),
        "carry": (),
        "produced_by": "verify",
        "consumed_by": ("verify",),
        "recompute": False,
    },
    {
        "id": "env_snapshot",
        "file": "",
        "fields": ("model", "device", "compute_type", "threads", "toolchain",
                   "cli_flags"),
        "required": (),
        "carry": (),
        "produced_by": "preflight",
        "consumed_by": ("transcribe", "translate", "generate", "verify"),
        "recompute": False,
    },
    # ---- 独立落盘的产物 ----
    {
        "id": "segments_raw",
        "file": "{base}.segments_raw.json",
        "fields": ("start", "end", "text", "words") + CONFIDENCE_FIELDS,
        "required": ("start", "end", "text"),
        "carry": CONFIDENCE_FIELDS,
        "produced_by": "transcribe",
        "consumed_by": ("transcribe", "verify"),
        "recompute": False,
    },
    {
        "id": "segments",
        "file": "{base}.segments_en.json",
        "fields": ("start", "end", "text", "words", "_raw_indices"),
        "required": ("start", "end", "text"),
        "carry": ("_raw_indices",),
        "produced_by": "transcribe",
        "consumed_by": ("translate", "generate", "verify"),
        "recompute": False,
    },
    {
        "id": "review",
        "file": "{base}.review.json",
        "fields": ("schema", "segments_digest", "params", "merged",
                   "g1_windows", "g2_windows", "unrecoverable"),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("transcribe",),
        "recompute": False,
    },
    {
        "id": "zh",
        "file": "{base}.zh_segments.json",
        "fields": ("index", "zh"),
        "required": (),
        "carry": (),
        "produced_by": "translate",
        "consumed_by": ("generate", "verify"),
        "recompute": False,
    },
    {
        "id": "translate_task",
        "file": "{base}.translate_task.json",
        "fields": ("persona", "guidelines", "to_translate", "full_transcript",
                   "source", "style"),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("translate",),
        "recompute": False,
    },
    {
        "id": "agent_pending",
        "file": "{base}.agent_pending.json",
        "fields": ("index", "en", "start", "end"),
        "required": (),
        "carry": (),
        "produced_by": "translate",
        "consumed_by": ("translate",),
        "recompute": False,
    },
    {
        "id": "detected_lang",
        "file": "{base}.{fp}.detected_lang.json",
        "fields": ("lang",),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("translate", "generate"),
        "recompute": False,
    },
    {
        "id": "chunk_cache",
        "file": "{base}.{fp}.chunk_{ci}.json",
        "fields": ("segments", "info"),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("transcribe",),
        "recompute": True,  # 断点缓存可重建，但删除即重转写（AGENTS.md 红线）
    },
    {
        "id": "align_cache",
        "file": "{base}.{align_fp}.chunk_{ci}.whisperx.json",
        "fields": ("words",),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("transcribe",),
        "recompute": True,  # 独立缓存层，不进转写指纹（ADR-028）
    },
    {
        "id": "vocals_wav",
        "file": "{base}.{fp}.vocals.wav",
        "fields": ("path", "duration_in", "duration_out"),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("transcribe", "verify"),
        "recompute": False,
    },
    {
        "id": "generate_opts",
        "file": "{base}.generate_opts.json",
        "fields": ("gap", "min_dur", "offset", "tail", "style",
                   "display_merge", "dm_gap", "dm_max_dur", "dm_max_chars",
                   "dm_max_zh", "dm_short_dur", "dm_short_words"),
        "required": (),
        "carry": (),
        "produced_by": "generate",
        "consumed_by": ("verify",),
        "recompute": False,
    },
    {
        "id": "display_merge",
        "file": "{base}.display_merge.json",
        "fields": ("params", "total_segments", "display_cues",
                   "merged_groups", "groups", "rejected"),
        "required": (),
        "carry": (),
        "produced_by": "generate",
        "consumed_by": ("verify",),
        "recompute": False,
    },
    {
        "id": "srt",
        "file": "{base}/{base}.bilingual.srt",  # 产物子目录 + _vN 升版，定位走 pipeline._find_srt
        "fields": ("bilingual", "zh", "en", "txt"),
        "required": (),
        "carry": (),
        "produced_by": "generate",
        "consumed_by": ("verify",),
        "recompute": False,
    },
    {
        "id": "review_log",
        "file": "{base}.review.log",
        "fields": ("audit_lines",),
        "required": (),
        "carry": (),
        "produced_by": "transcribe",
        "consumed_by": ("verify",),
        "recompute": False,
    },
)

# ADR-035: 缓存指纹声明（"哪些参数构成该 artifact 缓存的指纹"的唯一登记处）。
# carry 字段仅穿透下游、不进指纹；未登记的 artifact 无独立缓存指纹。
# 现状收编：transcribe chunk 指纹 / review digest / vocal_sep fingerprint 此前
# 各自为政，现统一在此声明（运行时指纹实现逐步对齐此表）。
FINGERPRINT_INPUTS: dict[str, tuple[str, ...]] = {
    "chunk_cache": ("fp", "ci", "model", "lang", "vad", "align"),
    "align_cache": ("align_fp", "ci"),
    "review": ("segments_digest", "params"),
    "vocals_wav": ("fp",),
}

_ARTIFACT_BY_ID: dict[str, Artifact] = {a["id"]: a for a in ARTIFACTS}


# --------------------------------------------------------------------------- #
# 命名 / 定位（命名单一来源）
# --------------------------------------------------------------------------- #

def get_artifact(artifact_id: str) -> Artifact:
    """按规范名取契约条目；未登记的名字直接报错（禁止模块自造）。"""
    try:
        return _ARTIFACT_BY_ID[artifact_id]
    except KeyError:
        raise KeyError(
            f"unknown artifact id {artifact_id!r} — register it in "
            f"artifacts.ARTIFACTS (ADR-035: naming has a single source)"
        ) from None


def artifact_ids() -> tuple[str, ...]:
    return tuple(a["id"] for a in ARTIFACTS)


def artifact_file(artifact_id: str, base: str, **fmt: Any) -> str:
    """文件名（不含目录）。模板占位符：{base} 及条目自定义（{fp}/{ci}/{align_fp}）。"""
    spec = get_artifact(artifact_id)
    if not spec["file"]:
        raise KeyError(
            f"artifact {artifact_id!r} is embedded in vt_state.json — "
            f"no standalone file name"
        )
    return spec["file"].format(base=base, **fmt)


def workdir(outdir: str | os.PathLike[str], base: str) -> str:
    """ADR-037: 产物根目录 = ``<outdir>/<base>/``.

    所有阶段产物（中间产物 / 缓存 / state / 最终字幕）的唯一落点基准。
    ``outdir`` 语义不变（默认 = 视频所在目录），base 子目录隔离单个视频的全部产出。
    禁止任何模块自造 ``os.path.join(outdir, base, ...)``（D1 路径单一来源）。
    """
    return os.path.join(str(outdir), base)


def artifact_path(artifact_id: str, outdir: str | os.PathLike[str],
                  base: str, **fmt: Any) -> str:
    """产物绝对路径 = workdir / 模板。带 glob/版本语义的产物（srt）由其定位器解析。"""
    return os.path.join(workdir(outdir, base), artifact_file(artifact_id, base, **fmt))


# --------------------------------------------------------------------------- #
# 校验器（边界校验铁律）
# --------------------------------------------------------------------------- #

def validate_artifact(artifact_id: str, data: Any, *,
                      require_carry: bool = False) -> list[str]:
    """返回契约违约清单（空 = 合规）。纯函数，无 I/O。

    ``required`` 字段缺失 = 结构违约；``require_carry=True`` 时进一步检查
    ``carry`` 字段（用于 raw 段生产侧与跨阶段契约测试；合并视图等合法缺
    carry 的场景不要开启）。
    """
    spec = get_artifact(artifact_id)
    problems: list[str] = []
    items = data if isinstance(data, list) else [data]
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(
                f"{artifact_id}[{i}]: expected dict, got {type(item).__name__}")
            continue
        for f in spec["required"]:
            if f not in item:
                problems.append(f"{artifact_id}[{i}]: missing required field {f!r}")
        if require_carry:
            for f in spec["carry"]:
                if f not in item:
                    problems.append(f"{artifact_id}[{i}]: missing carry field {f!r}")
    return problems


class ContractViolation(RuntimeError):
    """``enforce_contract(strict=True)`` 命中契约违约时抛出。

    本模块是最低层（只依赖 pipeline_def），不 import 上层的 capabilities——
    CLI 的 ``--strict`` 路径负责把本异常映射为 ``GateFail``（exit 8）。
    """


def enforce_contract(artifact_id: str, data: Any, *,
                     strict: bool = False, require_carry: bool = False,
                     progress: Any = print) -> list[str]:
    """阶段边界校验。违约默认 **loud warn 不阻断**（铁律⑤：state/契约永不做 gate）；
    ``strict=True``（CLI --strict）才升级为 ``ContractViolation``（由 CLI 层映射为
    GateFail / exit 8）。返回违约清单。"""
    problems = validate_artifact(artifact_id, data, require_carry=require_carry)
    if not problems:
        return []
    shown = "; ".join(problems[:5])
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    msg = f"[contract] {artifact_id}: {shown}{more}"
    if strict:
        raise ContractViolation(
            msg + " — 按 ADR-035 数据契约修复上述缺失字段后重跑")
    progress(msg)
    return problems


def raw_sources(
    seg: dict[str, Any],
    raw_segments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Z2 回查：按 ``_raw_indices`` 取合并视图段的源段（携带置信度）。

    无指针 / 未提供 raw / 越界时返回空列表——调用方回退用段自身字段（兼容
    恢复段、resegment 产物等自带置信度的段）。
    """
    if not raw_segments:
        return []
    idxs = seg.get("_raw_indices")
    if not isinstance(idxs, list):
        return []
    out: list[dict[str, Any]] = []
    for i in idxs:
        try:
            r = raw_segments[int(i)]
        except (IndexError, TypeError, ValueError):
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# 表自检（测试与启动期防漂移）
# --------------------------------------------------------------------------- #

def table_problems() -> list[str]:
    """契约表自身的一致性问题（空 = 表健康）。"""
    problems: list[str] = []
    seen: set[str] = set()
    for a in ARTIFACTS:
        aid = a["id"]
        if aid in seen:
            problems.append(f"duplicate artifact id {aid!r}")
        seen.add(aid)
        if a["produced_by"] not in STAGE_ORDER:
            problems.append(f"{aid}: unknown produced_by {a['produced_by']!r}")
        for sid in a["consumed_by"]:
            if sid not in STAGE_ORDER:
                problems.append(f"{aid}: unknown consumed_by {sid!r}")
        if a["file"] and "{base}" not in a["file"]:
            problems.append(f"{aid}: file template must contain {{base}}")
        for f in a["required"]:
            if f not in a["fields"]:
                problems.append(f"{aid}: required field {f!r} not declared in fields")
        for f in a["carry"]:
            if f not in a["fields"]:
                problems.append(f"{aid}: carry field {f!r} not declared in fields")
        if not isinstance(a["recompute"], bool):
            problems.append(f"{aid}: recompute must be bool")
    return problems

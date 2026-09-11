"""ADR-038 — ASR 层接口：可插拔 ASRProvider（第一步：只建接口，行为零变化）。

本模块只**定义接口与类型**，并给出第一个实现（Whisper 的薄包装）。
第一步**不改任何现有调用路径**——``cli.cmd_transcribe`` 仍直接调
``transcribe.transcribe_video``；本模块是旁挂的接口层，无人调用也不影响行为
（ADR-038 D5「第一步行为零变化」）。

分层目标（ADR-038 D1/D3）：

    ① ASR 方案层（本模块是其**引擎接口**边界）
        人声分离 → 转写 → 对齐 → 幻觉过滤 → 断句合并 → 漏音补洞 → review
        ↑ Provider 只管「音频 → 原始段」这一段
    ② 字幕交付层
        42 字符切分 → 表现层

Provider 边界 =「音频 → 原始段」，**不含**合并 / 补洞等方案后处理：那些是
「围绕引擎的方案逻辑」，换引擎时复用（仅阈值需重标定）；若塞进引擎接口，
每个新 Provider 都得重写一遍整理逻辑。

契约：``RawSegment`` 必须满足 ``artifacts`` 的 ``segments_raw`` required
（start/end/text）；``words`` 与置信度字段为**引擎可选产出**——缺失时下游信号
自动 inert（ADR-020 向后兼容口径）。产物契约零变化（ADR-038 D4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

# 引擎产出的原始段。必须满足 artifacts "segments_raw" 的 required 契约
# （start/end/text）；words 与置信度字段为引擎可选产出。
RawSegment = dict[str, Any]


@dataclass(frozen=True)
class TranscriberConfig:
    """引擎无关的通用转写参数（ADR-038 D3 / Spec 25）。

    默认值**必须与 ``transcribe.transcribe_video`` 的形参默认值逐字段等价**
    ——``tests/test_asr_provider.py`` 用 ``inspect.signature`` 锁定，防两处漂移。
    """

    model: str = "large-v3"
    chunk: float = 240.0
    threads: int | None = None
    lang: str | None = None
    vad_threshold: float | None = None
    use_vad: bool = False
    no_speech_threshold: float = 0.0
    temperature: list[float] | None = None
    device: str | None = None
    compute_type: str | None = None
    adaptive_vad: bool = False
    # T2 (ADR-017) 人声分离：audio_source 为 None 时用原输入
    audio_source: str | None = None
    separate_vocals: bool = False
    vocal_sep_backend: str = "demucs"
    vocal_sep_model: str = "htdemucs"
    vocal_sep_input_hash: str | None = None
    # T4 (ADR-028 / Spec 22) 强制声学对齐
    align_backend: str = "auto"
    align_allow_degrade: bool = False


@dataclass(frozen=True)
class TranscribeResult:
    """引擎转写结果（Spec 25）。"""

    segments_path: str            # 落盘的 raw segments 路径
    segments: list[RawSegment]    # 段数据（编排层继续后处理用，避免调用方再读盘）
    detected_lang: str | None = None


@runtime_checkable
class ASRProvider(Protocol):
    """ASR 引擎接口（ADR-038 D3/D7）：音频 → 原始段。

    - ``name``            引擎稳定标识（日志 / state 用）。
    - ``prerequisites()`` 本引擎的就绪要求（能力 id 元组），供 doctor / 闸门做
                          「通用体检 + Provider 自报体检」（ADR-038 D7）。
    - ``transcribe()``    唯一必实现方法。
    """

    name: str

    def prerequisites(self) -> tuple[str, ...]: ...

    def transcribe(
        self,
        input_path: str,
        outdir: str,
        *,
        base: str,
        config: TranscriberConfig,
        progress: Callable[..., None] = print,
    ) -> TranscribeResult: ...


class FasterWhisperProvider:
    """第一个实现：薄包装 ``transcribe.transcribe_video``（行为零变化）。

    本类**不复制任何逻辑**，只做两件事：

    1. 把 ``TranscriberConfig`` 逐字段展开成 ``transcribe_video`` 的关键字参数；
    2. 回读落盘的段数据与 ``detected_lang`` sidecar，填充 ``TranscribeResult``。

    读取 sidecar 的原因：``transcribe_video`` 只返回段文件路径，语言落在
    ``{base}.{fp}.detected_lang.json``（fp 是其内部指纹、调用方不可见），
    故按 glob 取最新的同 base sidecar。
    """

    name = "faster-whisper"

    def prerequisites(self) -> tuple[str, ...]:
        """与 ``capabilities.CAPS`` 现有命名对齐（ADR-038 D7）。"""
        return ("model:large-v3", "cuda", "whisperx", "demucs")

    def transcribe(
        self,
        input_path: str,
        outdir: str,
        *,
        base: str,
        config: TranscriberConfig,
        progress: Callable[..., None] = print,
    ) -> TranscribeResult:
        from .io_utils import load_json
        from .transcribe import transcribe_video

        segments_path = transcribe_video(
            input_path, outdir, base=base,
            model_name=config.model,
            chunk=config.chunk,
            threads=config.threads,
            lang=config.lang,
            vad_threshold=config.vad_threshold,
            use_vad=config.use_vad,
            no_speech_threshold=config.no_speech_threshold,
            temperature=config.temperature,
            device=config.device,
            compute_type=config.compute_type,
            adaptive_vad=config.adaptive_vad,
            audio_source=config.audio_source,
            separate_vocals=config.separate_vocals,
            vocal_sep_backend=config.vocal_sep_backend,
            vocal_sep_model=config.vocal_sep_model,
            vocal_sep_input_hash=config.vocal_sep_input_hash,
            align_backend=config.align_backend,
            align_allow_degrade=config.align_allow_degrade,
            progress=progress,
        )
        segments = load_json(segments_path) or []
        return TranscribeResult(
            segments_path=segments_path,
            segments=segments,
            detected_lang=_read_detected_lang(outdir, base),
        )


def _read_detected_lang(outdir: str, base: str) -> str | None:
    """读回 ``{base}.{fp}.detected_lang.json``（取最新的同 base sidecar）。

    文件由 ``transcribe_video`` 落盘，格式 ``{"language": "<lang>"}``。
    不存在 / 损坏 / 解析失败一律返回 ``None``——语言只是元信息，绝不阻断。
    """
    from .artifacts import workdir
    from .io_utils import load_json

    try:
        d = Path(workdir(outdir, base))
        candidates = sorted(d.glob(f"{base}.*.detected_lang.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for p in candidates:
        try:
            data = load_json(str(p))
        except Exception:  # noqa: BLE001 - sidecar 损坏不该阻断转写结果
            continue
        if isinstance(data, dict) and data.get("language"):
            return str(data["language"])
    return None

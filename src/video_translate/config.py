"""Configuration resolution.

Priority (highest wins): CLI args > environment variables > project-level
`.video-translate.toml` > built-in defaults.

Only project-level config is supported (no user-level layer). V5 (ADR-014)
adds ``device``/``compute_type`` (default "auto") — CUDA is resolved when an
NVIDIA GPU is present, otherwise the historical cpu/int8 behaviour is unchanged.

V2: adds engine/persona/merge_* fields; lang defaults to None (auto-detect);
proxy defaults to None (auto-detect via proxy.detect_proxy). The literal
``"auto"`` for lang is normalised to None.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

from .toolchain import init_toolchain

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

CONFIG_FILENAME = ".video-translate.toml"
DEFAULT_HF_CACHE = os.path.expanduser("~/.cache/huggingface")

DEFAULT_PERSONA = (
    "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感：忠实原意、表达自然、"
    "保留说话人语气与情绪。遇到俚语/文化梗用贴近中文口语的等价表达，不要直译。"
)

# --- T3 / ADR-027: 翻译风格三轨矩阵 ---
# film：影视二创（默认，等价于历史 DEFAULT_PERSONA 基调）
# literal：忠实直译，保真优先于流畅
# bilingual_study：双语精读，直译为主 + 生僻词括号注记
VALID_STYLES = ("film", "literal", "bilingual_study")


@dataclass
class StyleDef:
    """单条翻译风格的完整定义。"""
    persona: str
    guidelines: list[str]


STYLE_PERSONAS: dict[str, StyleDef] = {
    "film": StyleDef(
        persona=DEFAULT_PERSONA,
        guidelines=[
            "口语优先：用现代中文口语表达，避免书面腔与翻译腔。",
            "意译优先：遇到英语 idiom / 文化梗，用中文观众能秒懂的等价说法，不要逐字直译。",
            "保留语气：说话人的幽默、愤怒、迟疑、激动都要在译文里听得出来。",
            "节奏感：字幕是给人『念出来』的，断句要顺口，单条控制在 1-2 个短句。",
            "诗歌/歌词/rap：靠 source 字段引导（如『诗歌，需押韵与意象还原』），在信达雅基础上追求韵律。",
        ],
    ),
    "literal": StyleDef(
        persona=(
            "你是一位严谨的技术/学术译者。你的首要目标是信息保真：译文必须忠实于"
            "原文的逻辑结构、修饰关系与限定条件，绝不为流畅而省略或改写。"
        ),
        guidelines=[
            "保真优先：准确传达原文意思，不增译、不减译、不意译掉限定条件。",
            "结构对齐：尽量保留原文的句子结构与主谓宾顺序，便于逐句对照。",
            "术语严谨：专有名词、学术/法律/技术术语严格忠实，必要时保留英文原词并加括号。",
            "逻辑从句：定语从句、条件句、让步状语等修饰关系必须清晰可辨。",
            "不口语化：除非原文就是口语，否则不要用俚语或过于随意的表达。",
        ],
    ),
    "bilingual_study": StyleDef(
        persona=(
            "你是一位教学型双语译者。以『直译为主、辅以注记』的方式帮助中文读者精读"
            "英文原文，兼顾可读性与学习价值。"
        ),
        guidelines=[
            "直译为主：译文贴近原文结构与词义，便于回映英文。",
            "生词注记：对生僻词、熟词生义、文化专有项，在译文后用括号补注（如：『bank（河岸，此处非银行）』）。",
            "句式可回映：尽量让中文断句与英文句法对应，方便对照学习。",
            "保留术语：专业术语首次出现可附英文原词。",
            "不追求文采：清晰度与准确性高于修辞。",
        ],
    ),
}


@dataclass
class Config:
    """Resolved runtime configuration."""

    model: str = "large-v3"
    chunk: float = 240.0
    lang: str | None = None          # V2: None = auto-detect (Whisper)
    proxy: str | None = None         # V2: None = auto-detect / direct
    src: str = "en"
    tgt: str = "zh-CN"
    hf_cache_dir: str = DEFAULT_HF_CACHE
    # V5 (ADR-014) fields
    device: str = "auto"             # "auto" -> CUDA if available, else cpu
    compute_type: str = "auto"       # "auto" -> int8_float16 on cuda, else int8
    # V2 fields
    engine: str = "agent"
    persona: str = DEFAULT_PERSONA
    merge_enabled: bool = True
    merge_max_dur: float = 8.0
    merge_max_gap: float = 0.5
    merge_max_chars: int = 42
    # V3 fields
    glossary: str | None = None     # path to glossary file (txt/json), injected into persona
    # V6 (B3) fields
    source: str | None = None       # free-text provenance/背景 hint for the translator
    full_transcript: bool = True    # ship whole transcript in the agent task file
    # T2 (ADR-017 / Spec 19): vocal separation preprocessing
    separate_vocals: bool = False   # --separate-vocals / VT_SEPARATE_VOCALS
    demucs_model: str = "htdemucs"  # --demucs-model / VT_DEMUCS_MODEL
    # ADR-030 / Spec 24: hard-gap vocal separation recovery
    gap_vocal_sep: bool = False
    gap_vocal_sep_min_gap: float = 5.0
    gap_vocal_sep_energy_mean_db: float = -30.0
    gap_vocal_sep_energy_max_db: float = -10.0
    gap_vocal_sep_no_speech_thr: float = 0.5
    gap_vocal_sep_avg_logprob_thr: float = -0.8
    # T3 (ADR-027 / Spec 21): translation style track
    style: str = "film"             # --style / VT_STYLE / [translate].style
    # T4 (ADR-028 / Spec 22): forced-acoustic-alignment backend.
    # "auto" (default, T4 默认化): whisperx when CUDA + whisperx available, else none.
    align: str = "auto"             # --align / VT_ALIGN / [transcribe].align
    _sources: dict[str, str] = field(default_factory=dict, repr=False)


# TOML sections flattened into Config fields. [hf] cache_dir -> hf_cache_dir.
_TOML_SECTIONS = ("transcribe", "translate", "hf", "llm", "merge")


def load_toml(path: str) -> dict[str, Any]:
    """Load a `.video-translate.toml` into a flat dict, or {} if missing.

    Sections [transcribe]/[translate]/[hf]/[llm]/[merge] are flattened into
    single keys. [hf] cache_dir -> hf_cache_dir.
    """
    if not os.path.exists(path) or tomllib is None:
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    flat: dict[str, Any] = {}
    for section in _TOML_SECTIONS:
        for k, v in (data.get(section) or {}).items():
            key = "hf_cache_dir" if (section == "hf" and k == "cache_dir") else k
            flat[key] = v
    return flat


_FLOAT_ENV = {
    "chunk", "merge_max_dur", "merge_max_gap",
    "gap_vocal_sep_min_gap",
    "gap_vocal_sep_energy_mean_db",
    "gap_vocal_sep_energy_max_db",
    "gap_vocal_sep_no_speech_thr",
    "gap_vocal_sep_avg_logprob_thr",
}
_INT_ENV = {"merge_max_chars"}
_BOOL_ENV = {"merge_enabled", "full_transcript", "separate_vocals", "gap_vocal_sep"}


def _coerce_env(attr: str, raw: str) -> Any:
    if attr in _FLOAT_ENV:
        return float(raw)
    if attr in _INT_ENV:
        return int(raw)
    if attr in _BOOL_ENV:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if attr == "style":
        val = raw.strip().lower()
        if val not in VALID_STYLES:
            raise ValueError(
                f"invalid style '{raw}': must be one of {VALID_STYLES}"
            )
        return val
    if attr == "align":
        val = raw.strip().lower()
        # T4 (Spec 22): invalid value -> warn + fall back to "auto" (never crash).
        if val not in ("auto", "none", "whisperx"):
            print(f"[config] WARNING: invalid VT_ALIGN '{raw}', "
                  f"falling back to 'auto'", file=sys.stderr)
            return "auto"
        return val
    return raw


def resolve_config(
    cli_overrides: dict[str, Any] | None = None,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve a Config from defaults <- toml <- env (.env / os.environ) <- CLI overrides."""
    cwd = cwd or os.getcwd()
    if env is None:
        init_toolchain(cwd)
        env = dict(os.environ)
    cfg = Config()

    # 1. TOML (project-level)
    toml_vals = load_toml(os.path.join(cwd, CONFIG_FILENAME))
    for k, v in toml_vals.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
            cfg._sources[k] = "toml"

    # 2. Environment
    env_map = {
        "model": "VT_MODEL", "chunk": "VT_CHUNK", "lang": "VT_LANG",
        "proxy": "VT_PROXY", "src": "VT_SRC", "tgt": "VT_TGT",
        "hf_cache_dir": "HF_HOME",
        "device": "VT_DEVICE", "compute_type": "VT_COMPUTE_TYPE",
        "engine": "VT_ENGINE", "persona": "VT_PERSONA",
        "merge_max_dur": "VT_MERGE_MAX_DUR", "merge_max_gap": "VT_MERGE_MAX_GAP",
        "merge_max_chars": "VT_MERGE_MAX_CHARS", "glossary": "VT_GLOSSARY",
        "source": "VT_SOURCE", "full_transcript": "VT_FULL_TRANSCRIPT",
        "separate_vocals": "VT_SEPARATE_VOCALS",
        "demucs_model": "VT_DEMUCS_MODEL",
        "gap_vocal_sep": "VT_GAP_VOCAL_SEP",
        "gap_vocal_sep_min_gap": "VT_GAP_VOCAL_SEP_MIN_GAP",
        "gap_vocal_sep_energy_mean_db": "VT_GAP_VOCAL_SEP_ENERGY_MEAN_DB",
        "gap_vocal_sep_energy_max_db": "VT_GAP_VOCAL_SEP_ENERGY_MAX_DB",
        "gap_vocal_sep_no_speech_thr": "VT_GAP_VOCAL_SEP_NO_SPEECH_THR",
        "gap_vocal_sep_avg_logprob_thr": "VT_GAP_VOCAL_SEP_AVG_LOGPROB_THR",
        "style": "VT_STYLE",
        "align": "VT_ALIGN",
    }
    for attr, envkey in env_map.items():
        if envkey in env and env[envkey]:
            setattr(cfg, attr, _coerce_env(attr, env[envkey]))
            cfg._sources[attr] = "env"
    # Standard HTTPS_PROXY/HTTP_PROXY fallback for proxy (only if VT_PROXY unset)
    if cfg.proxy is None:
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            if env.get(k):
                cfg.proxy = env[k]
                cfg._sources["proxy"] = "env"
                break

    # 3. CLI overrides (only non-None values)
    for k, v in (cli_overrides or {}).items():
        if v is None or not hasattr(cfg, k):
            continue
        if k == "style":
            if v not in VALID_STYLES:
                raise ValueError(
                    f"invalid style '{v}': must be one of {VALID_STYLES}"
                )
        if k == "align":
            # T4 (Spec 22): invalid value -> warn + fall back to "auto".
            if v not in ("auto", "none", "whisperx"):
                print(f"[config] WARNING: invalid --align '{v}', "
                      f"falling back to 'auto'", file=sys.stderr)
                v = "auto"
        setattr(cfg, k, v)
        cfg._sources[k] = "cli"

    # Normalise lang="auto" -> None (auto-detect)
    if cfg.lang == "auto":
        cfg.lang = None

    return cfg
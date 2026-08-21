# Spec 06 — Config, toolchain, setup & doctor

Module: `config.py` + `toolchain.py` (+ `setup`/`doctor` in `cli.py`).

## Resolution priority (highest wins)
```
CLI args  >  runtime os.environ  >  .env.local  >  .env.<platform> (.env.win/.env.mac/.env.linux)  >  .env  >  .video-translate.toml  >  built-in defaults
```
Only **project-level** config is supported (no user-level layer), matching the
user's decision. `resolve_config()` records the winning source per key in
`Config._sources` for debuggability.

## Toolchain auto-injection (.env support)
- `VT_FFMPEG_DIR`: Directory containing `ffmpeg`/`ffprobe` binaries. Automatically prepended to `PATH`.
- `VT_CUDA_DIR`: Directory containing CUDA runtime DLLs (e.g. `cublas64_12.dll`). Automatically prepended to `PATH` and added to `os.add_dll_directory` on Windows.
- `VT_DEVICE`: `auto` (resolves to `cuda` if NVIDIA GPU + libraries present, else `cpu`).
- `VT_COMPUTE_TYPE`: `auto` (resolves to `int8_float16` on cuda, `int8` on cpu).

## Config fields & defaults
| Field             | Default                     | Env var             | TOML section.key    |
|-------------------|-----------------------------|---------------------|---------------------|
| `model`           | `large-v3`                  | `VT_MODEL`          | `[transcribe] model`|
| `chunk`           | `240.0`                     | `VT_CHUNK`          | `[transcribe] chunk`|
| `lang`            | `None` (auto-detect)        | `VT_LANG`           | `[transcribe] lang` |
| `proxy`           | `None` (auto-detect/direct) | `VT_PROXY`          | `[translate] proxy` |
| `src`             | `en`                        | `VT_SRC`            | `[translate] src`   |
| `tgt`             | `zh-CN`                     | `VT_TGT`            | `[translate] tgt`   |
| `hf_cache_dir`    | `~/.cache/huggingface`      | `HF_HOME`           | `[hf] cache_dir`    |
| `device`          | `auto`                      | `VT_DEVICE`         | — (CLI `--device`)  |
| `compute_type`    | `auto`                      | `VT_COMPUTE_TYPE`   | — (CLI `--compute-type`) |
| `engine`          | `agent`                     | `VT_ENGINE`         | — (CLI `--engine`)  |
| `persona`         | (信达雅+口语感 default)      | `VT_PERSONA`        | `[llm] persona`     |
| `merge_enabled`   | `True`                      | —                   | `[merge] merge_enabled` |
| `merge_max_dur`   | `8.0`                       | `VT_MERGE_MAX_DUR`  | `[merge] merge_max_dur` |
| `merge_max_gap`   | `0.5`                       | `VT_MERGE_MAX_GAP`  | `[merge] merge_max_gap` |
| `merge_max_chars` | `42`                        | `VT_MERGE_MAX_CHARS`| `[merge] merge_max_chars` |

`device`/`compute_type` are intentionally **not** configurable — forced to
`cpu`/`int8` (ADR-001). Env casts: `chunk`/`merge_max_*` → float, `merge_max_chars`
→ int, `merge_enabled` → bool. `lang="auto"` normalises to `None`.

V2 proxy resolution: `proxy=None` triggers `proxy.detect_proxy()` — order
`--no-proxy` → `--proxy` → `VT_PROXY` → `HTTPS_PROXY`/`HTTP_PROXY` → TCP probe
127.0.0.1:7890 → `None` (direct). Standard `HTTPS_PROXY`/`HTTP_PROXY` are read as
a fallback only when `VT_PROXY` is unset (ADR-007).

## `.video-translate.toml` example
```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "auto"          # auto-detect (default)

[translate]
src   = "en"
tgt   = "zh-CN"

[llm]
persona = "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感……"

[merge]
merge_enabled   = true
merge_max_dur   = 8.0
merge_max_gap   = 0.5
merge_max_chars = 42     # 剪映单行上限

[hf]
cache_dir = "~/.cache/huggingface"
```

## `setup` behavior
- If `_model_cached(model)` → print "reusing, no download", exit 0.
- Else set up HTTP proxy, then instantiate `WhisperModel(model, cpu, int8)` which
  triggers the HF download (~3GB for large-v3). Exit 3 on download failure, 4 on
  SOCKS proxy.
- HF cache is **shared** at `~/.cache/huggingface`; the model is downloaded once
  per machine and reused across projects.

## `doctor` behavior
Prints readiness of ffmpeg / ffprobe / HF cache dir / model-cached, the forced
device+compute_type, NVIDIA-CUDA presence, and whether faster-whisper &
deep-translator import. Always returns 0 (diagnostic, non-fatal).

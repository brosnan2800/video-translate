# Spec 06 — Config, setup & doctor

Module: `config.py` (+ `setup`/`doctor` in `cli.py`).

## Resolution priority (highest wins)
```
CLI args  >  environment variables  >  .video-translate.toml  >  built-in defaults
```
Only **project-level** config is supported (no user-level layer), matching the
user's decision. `resolve_config()` records the winning source per key in
`Config._sources` for debuggability.

## Config fields & defaults
| Field          | Default                     | Env var    | TOML section.key    |
|----------------|-----------------------------|------------|---------------------|
| `model`        | `large-v3`                  | `VT_MODEL` | `[transcribe] model`|
| `chunk`        | `240.0`                     | `VT_CHUNK` | `[transcribe] chunk`|
| `lang`         | `en`                        | `VT_LANG`  | `[transcribe] lang` |
| `proxy`        | `http://127.0.0.1:7890`     | `VT_PROXY` | `[translate] proxy` |
| `src`          | `en`                        | `VT_SRC`   | `[translate] src`   |
| `tgt`          | `zh-CN`                     | `VT_TGT`   | `[translate] tgt`   |
| `hf_cache_dir` | `~/.cache/huggingface`      | `HF_HOME`  | `[hf] cache_dir`    |

`device`/`compute_type` are intentionally **not** configurable — forced to
`cpu`/`int8` (ADR-001). `chunk` is cast to `float` when read from env.

## `.video-translate.toml` example
```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "en"

[translate]
proxy = "http://127.0.0.1:7890"
src   = "en"
tgt   = "zh-CN"

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

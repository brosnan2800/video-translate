# Spec 26 — Demucs 模型缓存落点与 doctor 覆盖

模块：`src/video_translate/vocal_sep.py`、`src/video_translate/cli.py`、`src/video_translate/fill_gaps.py`。
配套 ADR：[ADR-032](../adr/032-demucs-model-cache-locality.md)。

## 目的

把 Demucs 人声分离模型（htdemucs）的缓存**确定性落回项目内**（零 C 盘），并让 `doctor`
对它的缓存状态给出**真实可查**的三态报告，杜绝"包在、CUDA 在、模型却没有"的假绿灯。
同时固化一条通用规则：项目里**每一个需要下载权重的模型**，都必须同时具备
「项目内确定性落点」与「doctor 缓存检查」，未来新增模型零成本接入。

## 缓存落点契约

| 项 | 值 |
|---|---|
| 缓存根 | `<repo>/models/torch`（`vocal_sep.demucs_cache_dir()`） |
| demucs 4.x 权重（huggingface_hub） | `<cache>/hub/models--adefossez--HTDemucs/snapshots/<rev>/` |
| demucs 3.x 权重（torch.hub，兼容） | `<cache>/hub/checkpoints/` |
| 绑定方式 | `HF_HOME` + `TORCH_HOME` 同时指向缓存根 |
| 系统盘写入 | **禁止**（TOOLCHAIN §6 / MAJOR_VERSION_PLAN R5） |

查找路径必须来自 `demucs_cache_dir()`——**不依赖任何环境变量**，保证 doctor 与实际
下载落点由同一函数派生（ADR-032 不变量「落点与查找点同构」）。

## 模块接口

```python
def demucs_cache_dir() -> str:
    """Directory where the Demucs model is stored (project-local, not C:\\)."""


def _bind_demucs_cache() -> None:
    """Bind BOTH download backends to the project-local cache dir.

    demucs >= 4.0 downloads via huggingface_hub (honors HF_HOME);
    demucs 3.x downloads via torch.hub (honors TORCH_HOME). We bind both so
    the weights land in <repo>/models/torch regardless of demucs generation.
    Idempotent: safe to call before every separation.
    """


def _find_weight_file(
    roots: Sequence[str],
    *,
    suffixes: Sequence[str],
    name_needle: str,
    min_bytes: int,
) -> tuple[bool, str]:
    """Generic project-local weight lookup + completeness gate.

    THE standard接入点 for every downloadable model in this project: point it at
    the model's project-local cache root(s) and it reports complete / incomplete
    / missing. Wiring a new model through this helper gives `doctor` coverage for
    free, so a future model can never again sit on the C: drive — or be absent
    entirely — while doctor prints a green OK.

    Matching is done on the FULL path, because huggingface_hub stores weights
    under content-hash filenames (e.g. ``955717e8.safetensors``) with the model
    name appearing only in a parent directory. The largest match wins, so a
    partial file left by an aborted download cannot mask a complete copy.
    """


def _demucs_model_cached(
    model_name: str = "htdemucs",
    *,
    min_bytes: int | None = None,
) -> tuple[bool, str]:
    """Is the Demucs model cached in-repo and file-complete?

    Returns (cached_ok, path_or_reason). ``path_or_reason`` is the concrete
    cache path when found, else a human-readable reason ("missing" /
    "incomplete (… bytes < …)"), so `doctor` can print a deterministic fix.
    """
```

## 算法

### 1. 落点绑定（`_bind_demucs_cache`）

1. `os.environ["HF_HOME"] = demucs_cache_dir()`；
2. `os.environ["TORCH_HOME"] = demucs_cache_dir()`；
3. `os.makedirs(demucs_cache_dir(), exist_ok=True)`；
4. 幂等：重复调用结果一致（单测锁定）。

> 关键：只绑 `TORCH_HOME` **不足以**约束 demucs 4.x（它走 huggingface_hub，读 `HF_HOME`）。
> 只绑 `HF_HUB_CACHE` 则会与 `cli._hf_cache_dir()`（读 `HF_HOME`）分叉。故取 `HF_HOME`。

### 2. 缓存完整性检查（`_demucs_model_cached`）

在 `demucs_cache_dir()` 下查找模型目录（支持 demucs 4.x 的
`hub/models--adefossez--HTDemucs/snapshots/*/` 与 demucs 3.x 的 `hub/checkpoints/`）：

1. 定位权重文件（`.safetensors` 或 `.pt`，名字含 `htdemucs`）；
2. 若同时存在配置（`htdemucs.yaml`）或权重后缀可识别，进入大小校验；
3. 权重大小 < `min_bytes`（默认 50 MB）→ 判**残缺**，返回 `(False, "incomplete (… bytes < …)")`；
4. 未找到任何权重 → 判**缺失**，返回 `(False, "missing")`；
5. 否则返回 `(True, <concrete path>)`。

阈值标定：实测 `htdemucs` 的 `955717e8.safetensors` ≈ 84 MB，取 50 MB 为保守下限
（同 E3 对 large-v3 取 2 GiB 下限的思路：截断下载不得被误判为已缓存）。

### 3. doctor 展示

`checks` 增加一行，文案区分三态：

| 状态 | 输出 |
|---|---|
| 完整 | `[OK ] htdemucs model cached (project-local)` |
| 缺失 | `[MISS] htdemucs model cached : missing (<cache dir>)` |
| 残缺 | `[MISS] htdemucs model cached : incomplete (… bytes < 50 MB)` |

缺失/残缺时，doctor 的 `[FIX]` 段打印确定性命令：

```
[FIX] htdemucs model missing or incomplete. Run:
      make setup     # or: video-translate setup
```

### 4. 离线开关顺序（`fill_gaps`）

`os.environ.setdefault("HF_HUB_OFFLINE", "1")` 必须**早于** `recover_hard_gaps(...)`，
确保 gap-vocal-sep 触发的 Demucs 加载一律走本地缓存、不发起网络请求。

## 默认值

| 参数 | 默认值 | 说明 |
|---|---|---|
| `demucs_cache_dir()` | `<repo>/models/torch` | 项目内缓存根 |
| `_DEMUCS_MODEL_MIN_BYTES` | `50 * 1024 ** 2`（50 MB） | htdemucs 权重完整性下限 |

## CLI & Config

本次**不新增**任何 CLI flag 或 config 字段——只修正既有行为的落点与可观测性：

- `doctor` 输出新增 htdemucs 缓存行（见上表）；
- `--separate-vocals` / `--gap-vocal-sep` 行为不变，仅落点与离线语义修正。

## 测试计划（TDD）

### `tests/test_vocal_sep.py`（扩展 `TestDemucsCacheIsProjectLocal`）

- `test_bind_sets_hf_home_to_project_local`：`HF_HOME` 被绑定到 `demucs_cache_dir()`。
- `test_bind_sets_torch_home_to_project_local`：既有断言保留（向后兼容）。
- `test_bind_binds_both_backends`：两个变量同时等于缓存根（防止只绑一个回归）。
- `test_bind_is_idempotent`：重复调用结果一致。
- `test_cache_dir_never_points_at_user_dir`：缓存根不含 `Users` / 系统盘用户目录。

### `tests/test_doctor.py`（新增 `TestDoctorDemucsModelCache`）

- `test_demucs_model_cached_true_when_weights_present`：造出满足下限的权重 → `(True, path)`。
- `test_demucs_model_cached_false_when_missing`：空目录 → `(False, "missing")`。
- `test_demucs_model_cached_false_when_truncated`：权重小于下限 → `(False, incomplete)`
  （复刻 E3 对 large-v3 的截断下载校验）。
- `test_doctor_prints_htdemucs_line`：doctor 输出含 htdemucs 缓存行。
- `test_doctor_strict_with_missing_demucs_exits_7`：strict 模式缺模型 → `EXIT_DOCTOR_FAIL`。

### `tests/test_fill_gaps_bare.py` / `tests/test_gap_vocal_sep.py`（新增）

- `test_hf_hub_offline_set_before_gap_vocal_sep`：桩掉 `recover_hard_gaps`，断言其被调用时
  `os.environ["HF_HUB_OFFLINE"] == "1"`（锁定 Bug 3 不回归）。

### 回归

- `tests/test_cli_model_cache.py`：`_model_cached` 语义零变化（向后兼容）。
- 全量 `pytest` 绿。

## 不变量

- **零 C 盘**：htdemucs 权重默认落 `<repo>/models/torch/...`，不得写入系统盘用户目录。
- **落点与查找点同构**：doctor 查找路径由 `demucs_cache_dir()` 派生，与实际下载落点同源。
- **doctor 无假绿灯**：`vocal separation available` 之外，必须有 htdemucs 缓存三态报告。
- **离线先于使用**：`HF_HUB_OFFLINE` 早于任何 Demucs/Whisper 加载点。
- **向后兼容**：`_model_cached`（large-v3）行为与既有单测不变。

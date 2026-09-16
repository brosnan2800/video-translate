"""模型缓存的单一来源：目录定位 + 完整性探测 + 路径解析。

此前 ``<repo>/models`` 的推导与「模型是否就位」的判断分散在三处（``cli`` 的
doctor / setup、``transcribe`` 的加载前解析、``vocal_sep`` 的 demucs TORCH_HOME），
而 ``capabilities`` 的模型就绪探测必须反过来 import ``cli`` ——「控制面通用层」依赖
「入口壳」，方向是错的（此前靠延迟 import 勉强绕过循环）。

本模块把这些收敛到一处：

  - 目录单一来源：``REPO_ROOT`` / ``LOCAL_MODEL_DIR`` / ``hf_cache_dir()``
  - 完整性口径（E3）：``model_cached()`` / ``find_incomplete_model_bins()``
  - 路径解析：``resolve_model_path()``（HF 仓库 id ↔ 项目内本地目录）

**不含**下载（归 ``setup``）与加载（归 Provider / ``transcribe``）。探测判据与
``setup`` 的自愈判据同源，保证「doctor 说 OK」与「加载不会撞损坏快照」一致。
"""
from __future__ import annotations

import os

from .config import DEFAULT_HF_CACHE

# 项目内模型目录 <repo>/models/<name>：允许用户把模型直接放进仓库（例如从镜像
# 拷贝），完全绕开 HF Hub 与系统盘用户缓存。
# model_cache.py 位于 <repo>/src/video_translate/ → 三层向上即仓库根。
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_MODEL_DIR = os.path.join(REPO_ROOT, "models")

# Milestone 3 / E3：完整的 large-v3 model.bin ≈ 3.09 GB。小于此下界的 model.bin
# 是截断 / 损坏的下载，必须按「未缓存」处理——否则 setup 不会自愈、运行期还会
# 加载损坏快照。测试可 monkeypatch 本值，避免构造 3 GB 桩文件。
MODEL_MIN_BYTES = 2 * 1024 ** 3  # 2 GiB


def hf_cache_dir() -> str:
    """HF 缓存目录，经 toolchain 注册表解析（与启动期解析结果一致）。

    直接在调用点读 ``HF_HOME`` 会与启动时解析出的值不一致（未设 env、之后才加载
    ``.env``），这正是过去「模型缺失，请重新下载」误报的来源。
    """
    from .toolchain import model_dir

    return model_dir("hf_cache") or DEFAULT_HF_CACHE


def _hub_snapshot_dirs(model_name: str) -> list[str]:
    """HF hub 下与 ``model_name`` 相关的快照目录列表。

    两条探测（``model_cached`` / ``find_incomplete_model_bins``）共用，避免同一套
    「needle → snapshots → snap」遍历写两遍。
    """
    hub = os.path.join(hf_cache_dir(), "hub")
    if not os.path.isdir(hub):
        return []
    needle = model_name.replace("/", "--").lower()
    out: list[str] = []
    for d in os.listdir(hub):
        if needle not in d.lower():
            continue
        snap_root = os.path.join(hub, d, "snapshots")
        if not os.path.isdir(snap_root):
            continue
        out.extend(os.path.join(snap_root, s) for s in os.listdir(snap_root))
    return out


def model_cached(model_name: str = "large-v3", *,
                 min_bytes: int | None = None) -> bool:
    """模型是否已就位且文件完整（项目内目录 OR HF 缓存）。

    ``min_bytes`` 为 None 时读取模块级 :data:`MODEL_MIN_BYTES`（**调用时**读取，
    便于测试下调而不必构造 3 GB 桩文件）。
    """
    if min_bytes is None:
        min_bytes = MODEL_MIN_BYTES
    mbin = os.path.join(LOCAL_MODEL_DIR, model_name, "model.bin")
    if os.path.isfile(mbin) and os.path.getsize(mbin) >= min_bytes:
        return True
    for d in _hub_snapshot_dirs(model_name):
        mbin = os.path.join(d, "model.bin")
        if os.path.isfile(mbin) and os.path.getsize(mbin) >= min_bytes:
            return True
    return False


def find_incomplete_model_bins(model_name: str = "large-v3") -> list[str]:
    """列出**存在但小于** :data:`MODEL_MIN_BYTES` 的 ``model.bin`` 路径。

    ``setup`` 自愈用（E3）：下载前先删除截断的 ``model.bin``，避免随后加载到
    损坏快照。
    """
    found: list[str] = []
    cand = os.path.join(LOCAL_MODEL_DIR, model_name, "model.bin")
    if os.path.isfile(cand) and os.path.getsize(cand) < MODEL_MIN_BYTES:
        found.append(cand)
    for d in _hub_snapshot_dirs(model_name):
        mbin = os.path.join(d, "model.bin")
        if os.path.isfile(mbin) and os.path.getsize(mbin) < MODEL_MIN_BYTES:
            found.append(mbin)
    return found


def resolve_model_path(model_name: str) -> str:
    """项目内有完整模型目录则返回该目录，否则原样返回（HF 仓库 id）。

    让 ``model_name="large-v3"`` 解析到 ``<repo>/models/large-v3``（含 model.bin），
    而不是强制走 HF Hub 或系统盘缓存。faster-whisper 的 ``WhisperModel`` 两种入参
    都接受。
    """
    if os.path.sep not in model_name and not os.path.isabs(model_name):
        cand = os.path.join(LOCAL_MODEL_DIR, model_name)
        if os.path.isfile(os.path.join(cand, "model.bin")):
            return cand
    return model_name

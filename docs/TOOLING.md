# TOOLING.md — 工具与依赖管理（操作手册）

> **本文件只讲「怎么操作」** —— 装什么、验什么、加新工具怎么做。
>
> **单一维护源分工**（避免同一内容在多处漂移）：
> - **为什么这么决策** → 对应 ADR（不可变历史，不在此复述）：
>   E1 [ADR-023](adr/023-dependency-uv-lock.md) · E2 [ADR-024](adr/024-ffmpeg-auto-download.md)
>   · E3 [ADR-025](adr/025-model-cache-self-heal.md) · E4 [ADR-026](adr/026-cuda-venv-torch-lib.md)
> - **规则源头 R1–R7** → [`MAJOR_VERSION_PLAN.md`](../MAJOR_VERSION_PLAN.md) §3.2
> - **环境搭建分步操作** → [`TOOLCHAIN.md`](../TOOLCHAIN.md)
> - **命令入口统一 `uv run`** → [Spec 23](specs/23-environment-location.md) / [ADR-029](adr/029-command-entry-uv-run.md)
>
> 里程碑 3（环境确定性工程）的四项成果：E1 `uv.lock` 可复现 / E2 ffmpeg 自动下载 /
> E3 模型缓存校验自愈 / E4 CUDA venv 优先。

---

## 0. 一图看懂（新机器 / 新 Agent）

```
新 clone 项目
   │
   ├─ uv run video-translate setup            ← ① uv sync（依赖，uv.lock 固化）+ ② 预拉模型到 <repo>/models/
   │
   ├─ uv run video-translate setup --ffmpeg   ← ffmpeg 便携版自动下载到 tools/（E2）
   │
   ├─ uv run video-translate doctor           ← 全绿校验：ffmpeg / CUDA·CPU / 模型缓存
   │     └─ CUDA 来源标注 → venv-torch / env / none（E4 自动探测）
   │
   └─ uv run video-translate pipeline <视频>  ← 单一入口幂等推进（ADR-033 / Spec 24）
```

**关键原则**：环境一步到位、禁止自由发挥。就绪动作统一走 `uv run video-translate setup`，
**不散落缓存、不全盘搜、不手动改路径**。

---

## 1. 日常操作速查

| 我要做什么 | 命令 | 依据 |
|---|---|---|
| 装依赖 + 预拉模型 | `uv run video-translate setup` | E1 [ADR-023](adr/023-dependency-uv-lock.md) |
| 补 ffmpeg 便携版 | `uv run video-translate setup --ffmpeg` | E2 [ADR-024](adr/024-ffmpeg-auto-download.md) |
| 补 NLTK 对齐语料（GPU 用户） | `uv run video-translate setup --align` | [ADR-028](adr/028-whisperx-alignment-pass.md) |
| 全量自检 | `uv run video-translate doctor` | [Spec 20](specs/20-env-readiness.md) |
| 模型残缺自愈重下 | `uv run video-translate setup`（自动删 <2GiB 的 `model.bin` 后重拉） | E3 [ADR-025](adr/025-model-cache-self-heal.md) |
| 改了 `pyproject.toml` 之后 | `uv lock` → 与 `pyproject.toml` **同 commit** 提交 | R3 |
| 验 lockfile 是否最新 | `uv lock --check`（须 0 退出） | R3 |
| 跑测试 | `uv run pytest` | — |

> **⚠️ `Makefile` 已移除**（AGENTS.md 红线 R7）。历史文档中出现的 `make setup` /
> `make doctor` / `make test` / `make clean`，一律等价为
> `uv run video-translate setup` / `uv run video-translate doctor` / `uv run pytest`。

---

## 2. 新增一个外部工具 / 依赖的标准套路

> 引入新二进制或新 Python 依赖时照此执行，避免重蹈「散落缓存 / 版本飘移」覆辙。

1. **Python 依赖**：写进 `pyproject` 顶层 `dependencies` → `uv lock` → 同 commit 提交 lockfile（R1 + R3）。
2. **外部二进制**：
   - 在 `toolchain.py` 加 `ensure_<tool>(dest="tools/<tool>")`：按平台选源 + 走代理 + 幂等（已存在跳过）。
   - `cli.py` 的 `setup` 加 `--<tool>` 旗标；`doctor` 缺失时打印 `run: video-translate setup --<tool>`。
   - `tools/<tool>/` 加入 `.gitignore`（R4）。
   - `TOOLCHAIN.md` 的探测步骤收敛为「PATH → setup 自动下载」，删掉任何「全盘搜」步骤（E2 先例）。
3. **模型权重**：默认落 `<repo>/models/<name>/`（零 C 盘），加 ≥ 下限的完整性校验 + 自愈（E3 先例，R5）。
4. **语料 / 数据资产**（如 whisperx 对齐所需的 NLTK `punkt` / `punkt_tab`）：
   落 `<repo>/models/nltk_data/`，`setup --align` 幂等下载，`doctor` 缺失即打印修复命令；
   **代码侧在调用前主动把该目录注册进库的搜索路径**（`register_nltk_path()` 写入 `nltk.data.path`）。
   这样运行时不依赖 `NLTK_DATA` 之类的环境变量，且 `doctor` 与真实运行看到同一份状态 ——
   否则会出现「doctor 报缺失、实际却能用」的假告警（踩过）。关键性不亚于二进制：whisperx
   缺语料时抛的 LookupError 会被逐段回退吞掉，表现为「对齐跑完了但一个时间戳都没变」。
5. **CUDA / 运行时库**：优先自动探测 venv 内自带库（venv-torch），显式 `VT_CUDA_DIR` 作覆盖，
   其次系统 `CUDA_PATH`；每个候选项都必须**实际含有 CUDA DLL** 才算命中（E4 先例 —— 否则
   `CUDA_PATH=F:\Program Files` 这类无关目录会被当成 CUDA 目录）。
6. **文档同步**：本文件 + `TOOLCHAIN.md` + `AGENTS.md` 红线表同步更新；跨平台降级路径必须写清。

---

## 3. 验证清单（CI / 人工）

```bash
uv run video-translate setup            # uv sync + 模型预拉，一次成功
uv lock --check                          # 0 退出
uv run video-translate doctor            # ffmpeg [OK] / CUDA source: venv-torch / 模型 [OK] / whisperx OK
uv run pytest                            # 全量绿
```

- E2 单测：`tests/test_toolchain.py`（下载 mock、zip 解压、`.env.local` 写入、幂等跳过）。
- E3 单测：构造小尺寸假 `model.bin` 验证检测/自愈路径；加载失败退出码 `EXIT_MISSING_DEP(3)`。
- E4 单测：解析顺序（显式 `VT_CUDA_DIR` → venv-torch 自动探测 → 系统 `CUDA_PATH`）；
  且**无关目录（不含 CUDA DLL）必须被拒绝**，系统 `CUDA_PATH` 不得抢在 venv-torch 之前。
- 对齐（T4）：`uv run video-translate setup --align` 幂等可重复执行；`doctor` 显示 `whisperx: OK`；
  **在没有 `NLTK_DATA` 环境变量时**对齐仍能自动定位项目内语料（不出现 LookupError ——
  出现即意味着语料缺失导致对齐静默失效）。

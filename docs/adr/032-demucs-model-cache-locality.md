# ADR-032 — Demucs 模型缓存落点绑定修正与 doctor 模型缓存覆盖

- 状态：接受
- 日期：2026-08-30
- 关联：ADR-017（人声分离预处理）、ADR-021（恢复段幻觉守卫）、ADR-025（E3 模型缓存校验）、ADR-030（Gap Vocal Separation）、Spec 19、Spec 24、**Spec 26**、`vocal_sep.py`、`cli.py`、`fill_gaps.py`
- 触发案例：`Nobody Can Handle Christopher Walken's STRANGE Hum.mp4`（下文简称 Nobody）

## 背景

在 Nobody 上执行 `transcribe --gap-vocal-sep`（未带 `--separate-vocals`）时出现
`Connection to huggingface.co timed out` 并重试 5 次。排查暴露出三个叠加缺陷，前两个
早在 14:13 首次跑 `--separate-vocals` 时就已存在，只是一直没有暴露。

### Bug 1（根因）：缓存落点绑定绑错了环境变量

`vocal_sep.py` 的注释与实现都建立在一个**过时假设**上：

```
# Demucs downloads its weights via torch.hub, which honors TORCH_HOME.
```

而 `pyproject.toml` 钉的是 `demucs>=4.0.1`。**demucs 4.x 已改用 huggingface_hub
下载模型**（证据：报错 URL 为 `huggingface.co/adefossez/HTDemucs/resolve/main/htdemucs.yaml`，
权重为 safetensors 格式），honor 的是 **`HF_HOME`**（进而 `HF_HOME/hub`），
**不是 `TORCH_HOME`**。

结果：

- `<repo>/models/torch/` **始终为空**；
- htdemucs 权重实际落在 `C:\Users\<user>\.cache\huggingface\hub\models--adefossez--HTDemucs\`
  （实测 `955717e8.safetensors` ≈ 84 MB + `htdemucs.yaml`）；
- 直接违反 `TOOLCHAIN.md` §2.4 / §6 与 `MAJOR_VERSION_PLAN.md` **R5「零 C 盘」** 红线。

### Bug 2：doctor 假绿灯

doctor 的 `checks` 只覆盖 ffmpeg / ffprobe / HF 缓存目录存在性 / large-v3，
对 Demucs 仅做 `demucs_available()`（import 包）+ `resolve_vsep_route()`（lane）。
**从不检查 htdemucs 权重是否缓存、缓存在哪、是否完整**，于是稳定打印：

```
demucs (htdemucs): OK — CUDA lane; vocal separation available (--separate-vocals)
```

这是假绿灯：包在、CUDA 在，权重却散落在系统盘且无人校验。E3（ADR-025）的
`_model_cached` 完整性校验**只覆盖了 large-v3，漏了 htdemucs**。

### Bug 3：离线开关设置在使用点之后

`fill_gaps.py` 中 `recover_hard_gaps(...)`（gap-vocal-sep 的 Demucs 分离）执行于
`os.environ.setdefault("HF_HUB_OFFLINE", "1")` **之前**，导致 gap-vocal-sep 加载
Demucs 时 huggingface_hub 仍联网 HEAD 检查 → 无代理环境必然超时。

### 现象链

| 时间 | 事件 | 为什么当时没炸 |
|---|---|---|
| 14:13 | `--separate-vocals` 全局分离成功 | 当时网络通，模型下载到 C 盘 HF 缓存；Bug 1 让它没进项目，Bug 2 让 doctor 没报 |
| 现在 | `transcribe --gap-vocal-sep` 超时 | 无 `--separate-vocals` → `audio_source=None` → 走局部分离 → 需要加载 Demucs 模型 → Bug 3 让它联网检查 → 网络不通 |

## 决策

### 1. 双绑定：`HF_HOME` + `TORCH_HOME`（Bug 1）

`_bind_demucs_cache()` 同时绑定两个环境变量，覆盖两代 demucs 的下载后端：

- **`HF_HOME`** —— 面向 demucs 4.x 的 huggingface_hub 路径（权重落 `HF_HOME/hub/models--adefossez--HTDemucs/`）；
- **`TORCH_HOME`** —— 保留，兼容 demucs 3.x 的 torch.hub 路径（`TORCH_HOME/hub/checkpoints/`）。

两者均指向 `demucs_cache_dir()`（`<repo>/models/torch`），与既有 `TOOLCHAIN.md` §6
表格口径一致，**无需改动文档中的落点声明**。

**为什么选 `HF_HOME` 而不是 `HF_HUB_CACHE`**：`cli._model_cached` / `_hf_cache_dir()`
读的是 `HF_HOME`。若只设 `HF_HUB_CACHE`，huggingface_hub 的落点与 doctor 的查找路径
会分叉，重演"能下载但找不到"的口径不一致。设 `HF_HOME` 让落点与查找点天然同构。

风险评估：`_resolve_model_path` 对 large-v3 是**项目内优先**（`models/large-v3` 存在时
直接离线加载，不走 HF），因此 `HF_HOME` 变更不影响既有 large-v3 加载；即使回退到 HF
下载，落到项目内也符合 R5。

### 2. doctor 覆盖 htdemucs 缓存（Bug 2）

新增 `_demucs_model_cached() -> tuple[bool, str]`，查找路径取自 **`demucs_cache_dir()`**——
一个不依赖任何环境变量的**确定性项目内路径**（回应"不管哪里用都找这个路径"的诉求），
而非 `_hf_cache_dir()`（那个会随 `HF_HOME` 漂移）。

检查三态：完整 / 残缺（权小于下限）/ 缺失，并给出确定性修复命令。

### 3. 离线开关提前（Bug 3）

把 `os.environ.setdefault("HF_HUB_OFFLINE", "1")` 提到 `recover_hard_gaps(...)` 之前，
确保任何 Demucs/Whisper 加载点之前都已是离线语义。

### 4. 防再犯 gate（不是只写文档）

三道可执行的闸：

1. **泛化模型缓存检查器**：把 `_model_cached` 的"目录 + 权重文件 + 大小下限"逻辑抽成
   可复用形式，未来新增模型零成本接入 doctor；
2. **R6 准入清单加两条硬性项**：新增模型/权重必须①落项目内并声明绑定哪个环境变量，
   ②同步新增 doctor 缓存检查 + 单测；
3. **回归单测锁定**：锁定"零 C 盘落点"与"doctor 覆盖所有模型缓存"，防止回退。

## 不变量

- **零 C 盘**：任何模型/权重下载，默认落点必须是 `<repo>/models/…`，不得落到系统盘用户目录。
- **落点与查找点同构**：doctor 用来查找模型缓存的路径，必须与实际下载落点由同一个
  函数/`deterministic` 路径派生，不得各自读取可能分叉的环境变量。
- **doctor 不得假绿灯**：每一个"能力可用"声明（如 `vocal separation available`）
  都必须有对应的缓存/资产检查；`OK` 必须建立在资产实际存在且完整之上。
- **离线开关先于使用点**：任何 `HF_HUB_OFFLINE` / 离线语义的设置，必须早于所有
  可能触发网络请求的加载点。
- **向后兼容**：`_model_cached` 现有签名与行为不变（large-v3 语义零变化）。

## 后果

- 修改 `src/video_translate/vocal_sep.py`：`_bind_demucs_cache` 双绑定 + 修正过时注释。
- 修改 `src/video_translate/cli.py`：新增 `_demucs_model_cached`，doctor `checks` 增加
  htdemucs 行，泛化模型缓存检查。
- 修改 `src/video_translate/fill_gaps.py`：`HF_HUB_OFFLINE` 提前。
- 新增 `tests/test_vocal_sep.py` 与 `tests/test_doctor.py` 用例（TDD：先红后绿）。
- 新增 `docs/adr/032-*.md`（本文）与 `docs/specs/26-demucs-model-cache.md`。
- 更新 `MAJOR_VERSION_PLAN.md`（R6）、`TOOLCHAIN.md`（§2.4 / §6.2）、`docs/TOOLING.md`。
- 一次性运维：迁移 `C:\Users\<user>\.cache\huggingface\hub\models--adefossez--HTDemucs`
  到项目内缓存目录。

## 已知限制

- demucs 未来若再次更换下载后端（如改走 `HF_HUB_CACHE` 优先），需同步复核绑定策略；
  单测已锁定落点为项目内，落点漂移会被测试拦住。
- htdemucs 完整性下限按实测（safetensors ≈ 84 MB）取保守值，跨模型变体
  （`htdemucs_ft` 等）需重新标定。

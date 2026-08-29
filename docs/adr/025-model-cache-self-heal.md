# ADR-025 — 模型缓存完整性校验 + 残缺自愈

- **Status**: Accepted（已落地，E3）
- **Date**: 2026-08-25
- **关联**: `MAJOR_VERSION_PLAN.md` E3、§3.2 R5、`TOOLCHAIN.md` §2.3、`docs/TOOLING.md` §3、ADR-013（WhisperX 对齐，已由 ADR-028 落地）
- **落地**: `cli.py` 的 `_model_cached`、`cmd_setup` 自愈、`transcribe.py` 加载异常捕获、`EXIT_MISSING_DEP`

## 背景

旧 `_model_cached` 只检查 `model.bin` **是否存在**。下载中断时会产生残缺的半截文件，
被误判为「已缓存」，随后 `run` 在转写深处加载模型时崩溃，报错对小白不友好。

这与 Voice-Pro 已解决的「下载自愈」形成差距：Voice-Pro 统一 `model/` 目录 + 中断/损坏
自动修复。本项目同样面临「首次体验两大崩溃点」——下载断、加载崩——但旧逻辑没有
确定性出路。

## 决策

**`_model_cached` 增加大小下限校验，`setup` 检测到残缺即自愈重下，`run` 加载失败定向提示修复命令。**

1. **完整性校验**：`_model_cached` 不只查存在，还要求 `model.bin` 大小 ≥ 2 GiB
   （large-v3 完整 ≈ 3.09 GB；< 2 GB 视为残缺）。
2. **自愈**：`cmd_setup` 下载前自动删除「存在但 < 下限」的 `model.bin`
   （项目 `models/` 与 HF 共享 cache 均扫），随后重拉完整权重。
3. **加载失败兜底**：`transcribe.py` 的 `WhisperModel(...)` 构造包异常捕获，失败时
   打印 `fix: video-translate setup`，以 `EXIT_MISSING_DEP(3)` 退出，不裸 traceback。
4. **项目本地优先（零 C 盘）**：模型默认落项目根 `models/large-v3/`（含 `model.bin`），
   随项目拷贝、不读写系统用户目录；仅当项目根 `models/` 缺失才回退 `HF_HOME`。

## 理由

- **自愈闭环**：下载断 → 下次 `setup` 检出残缺 → 删后重下；加载崩 → 明确修复命令。
  两大崩溃点都有确定性出路，首次成功率逼近 100%。
- **项目本地优先**：避免模型缓存散落 C 盘用户目录（用户换机 / 清理的痛点），
  权重随项目走，与 `tools/ffmpeg/` 同理（R4/R5 协同）。
- **零误杀**：2 GiB 下限远小于完整 3.09 GB，不会误删正常缓存；正常缓存幂等不受影响。

## 后果

- 正面：首次体验确定性修复；`doctor` 可如实反映模型缓存状态（残缺会提示重下）。
- 负面 / 注意：项目本地策略使每项目一份权重（3~10 GB 重复占盘，与 Voice-Pro 同取舍，
  但换来零 C 盘污染）；HF 共享 cache 仅作回退覆盖，不再默认跨项目共享。
- 规范入口：`docs/TOOLING.md` §3；单测见 `tests/test_cli_model_cache.py`
  （构造小尺寸假 model.bin 验证检测/自愈；加载失败退出码）。

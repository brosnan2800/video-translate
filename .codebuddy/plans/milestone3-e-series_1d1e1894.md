---
name: milestone3-e-series
overview: 按 MAJOR_VERSION_PLAN.md 里程碑 3 的 E 系列顺序（E1→E2→E3→E4）开发：E1 完整 uv 迁移（生成 uv.lock、Makefile setup 改 uv sync、文档口径统一），E2 doctor 强化（FFmpeg/CUDA/模型路径显式、按 exit code 5 失败的 KILLED 检查、--no-model 开关、--json 输出），E3 文档化 init_toolchain 的 .env 层级、环境变量与模型解析机制，E4 CLI 与文档补工具链引导（doctor 警告指向 setup、--ffmpeg-dir/--cuda-dir 透传、troubleshooting）。每个任务完成后跑全量 pytest，文档随代码同步提交。
todos:
  - id: e1-uv-migration
    content: E1 完整 uv 迁移：uv lock 生成提交、Makefile setup 改 uv sync、文档口径统一
    status: completed
  - id: e2-ffmpeg-auto
    content: E2 ffmpeg 自动下载：ensure_ffmpeg、setup --ffmpeg、doctor 提示、TOOLCHAIN 收敛、.gitignore
    status: completed
    dependencies:
      - e1-uv-migration
  - id: e3-model-selfheal
    content: E3 模型缓存校验自愈：大小下限、setup 重下、run 异常退出码、单测
    status: completed
    dependencies:
      - e2-ffmpeg-auto
  - id: e4-cuda-order
    content: E4 CUDA 解析顺序：venv torch 优先、doctor 标注、.env.win 清理、单测
    status: completed
    dependencies:
      - e3-model-selfheal
  - id: full-pytest-docs
    content: 每任务跑全量 pytest 并同步提交文档，验证全绿
    status: completed
    dependencies:
      - e1-uv-migration
      - e2-ffmpeg-auto
      - e3-model-selfheal
      - e4-cuda-order
---

## 用户需求

按 MAJOR_VERSION_PLAN.md 里程碑 3 的 E 系列顺序（E1→E2→E3→E4）开发环境确定性工程，对标 Voice-Pro 最佳实践，目标是新机器 clone 后 `make setup` 一次成功率逼近 100%，Agent 无自由发挥空间。每个任务落地须遵守 AGENTS.md §1 红线表与 PLAN §3.2 依赖规则 R1-R7，先写测试（TDD），文档随代码同步，每任务完成后跑全量 pytest。

## 产品概述

通过四项工程改造，把项目的安装与环境自检从"易飘移、易散落缓存、易误判"升级为"可复现、自包含、可自愈"：锁文件固化依赖、ffmpeg 便携版自动下载、模型缓存完整性校验与自愈、CUDA 解析优先 venv torch。

## 核心功能

- E1 完整 uv 迁移：生成并提交 uv.lock，Makefile setup 主路径改 uv sync，pip 仅作回退说明，三份文档安装口径统一。
- E2 ffmpeg 自动下载便携版：toolchain 新增 ensure_ffmpeg，setup 子命令支持 --ffmpeg，doctor 缺失提示，TOOLCHAIN 收敛两步探测，tools/ 进 .gitignore。
- E3 模型缓存完整性校验+自愈：_model_cached 加大小下限校验，setup 残缺自愈重下，run 阶段异常捕获给出修复命令并以 EXIT_MISSING_DEP 退出。
- E4 CUDA 解析顺序 venv torch/lib 优先：init_toolchain 顺序改为 venv torch/lib 自动探测→VT_CUDA_DIR 显式覆盖→CPU 降级，doctor 标注来源，移除 .env.win 硬编码示例。

## 技术栈

- 现有项目：Python 包（src/video_translate），CLI 用 argparse，测试用 pytest。
- 包管理：uv（新增 uv.lock，主路径 uv sync），pyproject.toml 已配 [tool.uv.index] 清华 cu124 镜像与 [tool.uv.sources] 平台 wheel 选择。
- 文档：README.md、AGENTS.md、TOOLCHAIN.md 三份口径保持同步（R6）。

## 实现方案

### 总体策略

按 E1→E2→E3→E4 顺序落地，每步 TDD 先写测试再改实现，全程复用现有 `_model_cached`、`init_toolchain`、`cmd_setup`/`cmd_doctor` 结构，不引入新重型依赖（R7），不改模型自动下载默认行为（R3）。

### 关键技术决策

- E1：完整 uv 迁移。`Makefile setup` 改为优先 `uv sync`（检测 uv 不存在打印指引并回退 `pip install -e .`），`uv lock` 生成 uv.lock 与 pyproject 成对提交（避免版本飘移红线）。保留 requirements.txt 作为 pip 兜底但不作为主路径。
- E2：ensure_ffmpeg 按 sys.platform 先选平台再选源（Windows gyan.dev zip / Linux 静态 tar.xz / macOS evermeet zip），走既有 proxy.detect_proxy，禁止跨平台复用包；下载/解压/写 .env.local（gitignore 内）幂等。doctor 在 ffmpeg [MISS] 输出 `run: video-translate setup --ffmpeg`。TOOLCHAIN §2.1「全盘搜」从文档删除降级人工兜底。
- E3：_model_cached 增加 model.bin < 2GB 视为残缺（large-v3 约 3.09GB）；cmd_setup 命中残缺时删 snapshot 目录重下；transcribe 构造 WhisperModel 包异常捕获，打印 `模型加载失败（可能缓存损坏）。修复：video-translate setup` 并以 EXIT_MISSING_DEP 退出，避免裸 traceback。
- E4：init_toolchain CUDA 解析顺序 ① venv torch/lib（import torch 定位）② 显式 VT_CUDA_DIR（仍最高优先）③ 无则 CPU 降级；doctor 标注来源 venv-torch/env/none；.env.win.example 移除 pyvideotrans 硬编码。

### 性能与可靠性

- ensure_ffmpeg 与模型下载均走代理且幂等，避免重复联网（E2/E3 热路径仅在缺失时触发）。
- _model_cached 增加大小校验为 O(1) stat 调用，不引入遍历开销；HF cache 遍历保持原有逻辑。
- 所有改动保持现有 pytest 套件绿色；新增 mock 测试不真联网（E2 下载 mock、E3 残缺 model.bin、E4 解析顺序）。

## 实现备注

- 严格遵守红线：不得改 agent 引擎触网（R1）、不得破坏现有 pytest、不得引入新重型依赖（R7）。
- 复用既有 `proxy.detect_proxy`、`load_env`、`.gitignore` 模式；tools/ 与 models/ 同理不进 git。
- 日志复用现有 print 风格，错误信息含可执行修复命令，避免 dump 大 payload。
- 文档同步：每任务改完代码即更新对应文档段落，禁止代码与文档分离提交。

## 架构设计

基于现有分层：cli（命令入口）→ toolchain（环境注入）→ transcribe（模型加载）。E 系列均在现有模块内增强，不新增架构模式。数据流：make setup → uv sync + cli setup（模型/ffmpeg）→ doctor 自检全绿。

## 目录结构

```
video-translate/
├── uv.lock                         # [NEW] uv 锁文件，与 pyproject 成对提交
├── Makefile                        # [MODIFY] setup 目标改 uv sync 优先，pip 回退
├── pyproject.toml                  # [MODIFY] 确认依赖与 requirements.txt 对齐（如需）
├── .gitignore                      # [MODIFY] 增加 tools/ 排除
├── .env.win.example                # [MODIFY] 移除 pyvideotrans 硬编码 CUDA 示例
├── README.md                       # [MODIFY] Quickstart 安装口径统一为 uv 优先
├── AGENTS.md                       # [MODIFY] Phase 0 强制 make setup，模型/工具链引导
├── TOOLCHAIN.md                    # [MODIFY] §2.1 收敛两步探测，§2.2 CUDA 顺序，§3.1 安装口径
├── src/video_translate/
│   ├── toolchain.py                # [MODIFY] 新增 ensure_ffmpeg；E4 CUDA 解析顺序 venv torch 优先
│   ├── cli.py                      # [MODIFY] cmd_setup 加 --ffmpeg 与残缺自愈；cmd_doctor 提示/标注；_model_cached 大小校验；transcribe 异常捕获
│   └── transcribe.py               # [MODIFY] WhisperModel 构造包异常捕获退出码
└── tests/
    ├── test_toolchain.py           # [MODIFY] 加 ensure_ffmpeg mock、E4 解析顺序单测
    └── test_cli_model_cache.py     # [NEW] E3 残缺 model.bin 检测/自愈路径单测
```

## 关键代码结构

```python
# toolchain.py 新增接口（示意签名）
def ensure_ffmpeg(dest: str = "tools/ffmpeg") -> str | None:
    """按平台下载便携 ffmpeg 并解压，返回 bin 目录；已存在则幂等跳过。"""

# cli.py _model_cached 增强签名意图
def _model_cached(model_name: str = "large-v3", *, min_bytes: int = 2 * 1024**3) -> bool:
    """存在 model.bin 且大小 >= min_bytes 才视为完整缓存。"""
```
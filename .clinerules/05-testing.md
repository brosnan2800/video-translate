# 测试规范（Testing）

## 分层

- **常规 suite**（`uv run pytest -q`，pytest 配置已带 `-m 'not slow'`）：
  纯函数 + mock 产物，秒级，禁止真实模型/GPU/网络/长睡眠。
- **slow 标记**：确实要跑真实转写/翻译的端到端测试 → `@pytest.mark.slow`
  （默认 deselect；当前 3 个）。
- **GPU/模型实弹验证**：不进 suite——走 `%TEMP%` 副本冒烟脚本，跑完即删，
  绝不触碰 `videos/` 真实产物。

## 写法约定

- 测试文件 `tests/test_<模块/特性>.py`；fixture 用 `tmp_path` 造产物
  （segments/zh/vt_state JSON），参照 `tests/test_verify_gate.py` 的 art fixture。
- **monkeypatch 打在源头上**：
  - patch 源模块属性：`video_translate.transcribe.transcribe_window`
  - patch cli 模块属性：`cli.analyze_audio` / `cli.probe_volume_window` /
    `cli.probe_duration`
  - CLI 测试涉及 ffmpeg 存在性检查时，**显式 patch `cli._require_ffmpeg`**
    ——禁止依赖「前面的测试碰巧初始化过 toolchain 缓存」（执行顺序耦合 = 偶发红）。
- 合成数据优于真数据：ffmpeg 输出用合成 stderr 喂 `parse_*`。
- **事故回归测试必须用真实事故几何**并在 docstring 引用出处
  （如 `Super busy.[39.26-40.16] vs Is he...[39.96-43.94] 重叠 0.2s`、
  `We'll be right back. nsp=0.906`、jimmy.mp4 的 Don't worry. 系列）。
- docstring 引用 ADR 编号（如 `ADR-031 D1`），让测试可追溯。

## 验收纪律

- 提交前 `uv run pytest -q` **全量**绿；只跑改动文件不算。
- 红灯先问「测试错还是实现错」，禁止为绿改断言语义。
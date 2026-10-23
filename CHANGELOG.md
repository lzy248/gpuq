# Changelog / 变更日志

All notable changes are recorded here. / 所有重要变更记录于此。

## Unreleased

### English

- Support single-node multi-GPU jobs with `--gpus N` / `--gpu-count N`, atomic
  group assignment, per-card reservations and OOM learning, and distributed-rank
  memory sampling. Existing jobs and submissions remain single-GPU by default.
- Allow a long-running non-exclusive auto probe to release unused launch-time
  reservation after a configurable process-memory stability window, while retaining a
  1 GiB fluctuation margin by default.
- Keep observed peaks as a dynamic reservation floor and restart the stability window
  conservatively after daemon restart or missing process-memory samples.
- Prevent backfill onto a GPU with an active exclusive queue job.

### 中文

- 新增 `--gpus N` / `--gpu-count N` 支持单机多卡任务，一次领取整组 GPU，按卡预留和
  学习 OOM 显存需求，并采样分布式 rank 的显存。已有任务和默认提交仍使用单卡。
- 非独占的长时 auto probe 在通过可配置的进程显存稳定窗口后，可以释放未使用的
  启动时预留；默认仍保留 1GiB 波动空间。
- 已观测峰值持续作为动态预留下界；daemon 重启或进程显存样本缺失后保守地重新
  计算稳定窗口。
- 禁止向仍有 active exclusive 队列任务的 GPU 回填新任务。

## [0.2.0] - 2026-07-21

### English

- Made automatic VRAM discovery the default: submit without `--memory`.
- Added unbounded retry for recognized CUDA/HIP OOM only, with learned launch-free,
  observed-peak, and failed-allocation floors.
- Added terminal `oom_exceeds_device` detection when no eligible single GPU can fit.
- Kept numeric memory estimates and bounded `--retry-on-oom --max-retries` compatible.
- Added an interactive 20-row list pager with arrow navigation and an alternate screen
  that leaves terminal scrollback untouched.
- Added backward-compatible SQLite migration and new auto-memory diagnostics.
- Fixed Python 3.10 configuration loading with a conditional `tomli` dependency.
- Added bilingual documentation, SVG branding, CI, issue template, package metadata,
  security and contribution policies.

### 中文

- 默认启用自动显存发现：提交时可以不写 `--memory`。
- 只有识别到 CUDA/HIP OOM 才不限次数重试，并综合启动空闲显存、观测峰值和
  失败分配量学习下界。
- 当所有允许单卡都无法容纳时，以 `oom_exceeds_device` 明确终止。
- 保留数字显存估计及有界 `--retry-on-oom --max-retries` 的兼容行为。
- 新增默认每页 20 条的交互列表、方向键翻页和不污染终端历史的备用屏幕。
- 新增向后兼容的 SQLite 迁移及 auto 显存诊断字段。
- 通过条件依赖 `tomli` 修复 Python 3.10 配置读取。
- 补齐双语文档、SVG 品牌标志、CI、issue 模板、包元数据、安全与贡献政策。

## [0.1.0] - 2026-07-20

- Initial personal opportunistic GPU queue with persistent SQLite jobs, stable memory
  sampling, allowlists, priorities, dependencies, cancellation, logs, notifications,
  bounded OOM retry, and a systemd user service.
- 首个个人机会式 GPU 队列版本：包含 SQLite 持久任务、稳定显存采样、GPU 白名单、
  优先级、依赖、取消、日志、通知、有界 OOM 重试和 systemd 用户服务。

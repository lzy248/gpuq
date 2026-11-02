# Changelog / 变更日志

All notable changes are recorded here. / 所有重要变更记录于此。

## Unreleased

## [0.3.0] - 2026-11-02

### English

- Add opt-in shared local/remote GPU scheduling with three-member etcd quorum, leased master election,
  standby takeover and delayed preferred-master failback. Replicate input snapshots, persist execution
  deduplication, preserve retry budgets and pause new authorization without quorum. Add deployment guides
  and real three-member etcd integration tests; each task remains on one host.
- Add bounded at-least-once node-loss retries: by default when other runnable work drains or after one hour,
  at most once per job. Isolate replacement executions, reject stale results and cancel old work on return.
  Expose policy switches and persistent retry/history audit; shared external writes may be duplicated.

- Render `show` text/JSON and `list --json` timestamps in the CLI host’s local timezone with explicit offsets; keep UTC storage/logs.

- Add unified SSH commands, incremental content-addressed uploads and isolated task snapshots,
  reusable remote Python environments and model caches, explicit checkpoint inclusion, and detached daemon startup.
- Default to fair project admission with persisted turns and configurable project/total concurrency caps.
  Positive priorities retain precedence; running tasks are not preempted. Keep legacy FIFO as an option.
- Preserve all settings during daemon CLI overrides and scan all active jobs when enforcing concurrency/reservations.

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

- 新增可选本地/远程统一 GPU 池，三成员 etcd 多数派选主、备节点接管和首选主稳定回切；
  复制输入、持久执行去重、跨节点保留重试预算，缺多数派停止新授权。补充部署手册与真实
  三成员集成测试；同一任务仍在单台主机执行。
- 失联任务默认在其他可运行工作清空或一小时后有界重排，每任务最多一次；提供关闭/超时/次数配置，
  独立执行目录、旧结果隔离、旧节点回归取消与持久审计。此策略可能重复训练和共享外部写入。

- `show` 文本/JSON 和 `list --json` 时间按 CLI 所在主机本地时区显示并带 offset；存储/日志保持 UTC。

- 新增统一 SSH 调用、按内容增量上传与独立任务快照，复用远程 Python 环境和模型缓存，
  支持续训 checkpoint 显式传输及后台 daemon 启动。
- 默认按项目公平分配新启动机会，轮次持久化，可设置项目/总并发上限；正优先级优先，
  不抢占正在运行的训练，保留原 FIFO 模式。
- daemon 参数覆盖保留其余配置；并发与资源预留扫描全部活跃任务。

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

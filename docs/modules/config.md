# 配置与运行路径

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/config.py](../../src/gpuq/config.py)。

## 职责与边界

把 TOML、显式路径和环境变量变成冻结的 `Settings`。本模块不打开 DB、不采样 GPU；
`ensure_state_dirs` 才会创建目录。调用方是 [CLI](cli.md)、[scheduler](scheduler.md)
和通知前重新加载配置的 [runner](runner.md)。

## 接口与数据

新增 [gpuq].default_queue="local"（默认）或 "cluster"：普通命令默认队列。显式 --local/--remote/--cluster
优先；init 仍是本地初始化，cluster init/status 始终面向池。池拓扑由 cluster.load_cluster 读取。


[cluster] 不进入 Settings，由 [cluster.load_cluster](cluster.md) 单独读取；完整字段、默认值和
优先顺序见[集群配置表](../cluster_usage.md#gpuq-节点配置)。全局模式/总上限来自控制存储中的 init 策略，
不使用 [gpuq] 对应值；节点资源阈值仍使用 Settings。节点 work_dir 独立于 state_dir。


`load_settings(*, state_dir=None, config_path=None) -> Settings`。
支持 `[gpuq]` 表，也支持顶层键；没有配置文件时使用默认值。

| 字段 | 默认值 / 单位 |
|---|---|
| default_queue | local；可选 cluster，显式 --local/--remote/--cluster 覆盖 |
| state_dir | `~/.local/state/gpuq` |
| scheduling_mode | `fair`；可选 `fifo` |
| max_running_jobs | 0（不设总并发上限）；>=0，所有优先级都受限 |
| default_project_limit | 1；>=1，仅初始化新项目的普通任务并发上限 |
| poll_seconds | 10.0 秒，必须 > 0 |
| default_reserve_mb | 1024 MB，每卡，>= 0 |
| default_stable_samples | 3，>= 1 |
| auto_probe_hold_seconds | 300.0 秒，>= 0 |
| auto_probe_stable_samples | 12，>= 1 |
| auto_probe_stability_tolerance_mb | 128 MB，>= 0 |
| auto_probe_release_margin_mb | 1024 MB，>= 0 |
| exclusive_max_used_mb | 768 MB |
| exclusive_max_utilization | 5（百分比） |
| exclusive_shared_gpus | 空 tuple；显式允许独占任务与队列外进程共享的物理卡 |
| max_orphan_retries | 0（关闭）；>= 0，每任务独立持久预算 |
| nvidia_smi | `nvidia-smi` 可执行文件 |
| notify_command | None；字符串经 shlex.split，数组逐元素变成 argv |
| config_path | 实际选择的配置路径；默认文件不存在且未显式指定时为 None |

派生路径：`database_path=state_dir/queue.sqlite3`，`logs_dir=state_dir/logs`，
`lock_path=state_dir/gpuqd.lock`。

## 关键流程与不变量

配置路径优先级：显式 `config_path` > `GPUQ_CONFIG` > 默认配置路径。
状态目录优先级：显式 `state_dir` > TOML 的 `state_dir` > `GPUQ_STATE_DIR` > 默认目录。
注意 TOML state_dir 高于环境变量。路径 expanduser 后 resolve。

Python 3.11+ 用 tomllib，3.10 用 tomli。加载器对正值/非负值做检查；构造 Settings
本身没有同等验证，测试可以直接构造。配置不是自动热加载：daemon 启动时取快照；
runner 通知路径重新加载，通过 scheduler 继承的 GPUQ_CONFIG 使用自定义配置。

## 失败与限制

无效 TOML/数字会抛异常。`exclusive_max_used_mb`/`exclusive_max_utilization` 当前仅 int
转换，不提供完整范围校验，不能宣称所有字段都有严格类型和范围验证。
示例 TOML 和 init 输出是常用项子集，未列出的字段使用上表默认值。
CLI 中 init 目标路径例外见 [CLI](cli.md)。daemon 覆盖保留所有未修改字段。
本地配置的 [remotes.NAME] 由 [SSH](remote.md) 读取，不进入 Settings；
远程队列使用远程配置，不能把本地 [gpuq] 配置隐式复制到远程。

## 修改与验证

新增项一起更新 Settings、load_settings、必要验证、CLI 重建/模板、示例配置与本文默认表。
若影响 worker/通知，验证 config_path 环境传递；不要把配置只加到 dataclass。
运行 `pytest tests/test_cli.py tests/test_exclusive_external_sharing.py tests/test_orphan_recovery.py`。
项目模式/并发校验和 daemon 覆盖保留还需运行 `pytest tests/test_projects.py`。
当前没有独立 test_config.py；新增配置测试应使用临时文件并隔离环境变量。

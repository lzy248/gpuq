# 简单 GPU 池：配置一次，直接提交

[English](pool_usage.en.md) · [工程总览](README.md) · [CLI 手册](cli_reference.md)

本功能在当前源码中提供，已发布的 v0.3.0 不包含简单池模式。两端需安装包含本功能的相同源码版本。

一台主机保存全局队列，本地和远程 GPU 共同执行任务。只需 gpuq、SQLite（Python 自带）和 SSH，
**不需要 etcd、见证节点或额外网络服务**。配置 default_queue 后，平时只管 `gpuq submit`，
不必手动选择机器或卡。主机离线暂停新派发，已启动的训练继续，恢复原主机后对账并继续调度。

## 持久配置

下面以本机 a、远程 b 为例，使用默认配置路径 `~/.config/gpuq/config.toml`。
SSH 主机名、解释器和用户名替换为实际值。主机需要 SSH 到 worker 传输入/读日志，worker 需要
SSH 到主机访问全局队列；不需要打开额外服务端口。两边先配置好非交互 SSH 与主机密钥验证。

主机 a：

```toml
[gpuq]
default_queue = "cluster"
default_project_limit = 2

[cluster]
backend = "sqlite"
node_id = "a"
namespace = "training"
work_dir = "~/.local/share/gpuq/clusters/training"
gpu_allowlist = [0, 1]

[cluster.nodes]
a = "local"
b = "worker_b"

[remotes.worker_b]
host = "user@b.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

远程 b：

```toml
[gpuq]
default_queue = "cluster"

[cluster]
backend = "ssh"
coordinator = "master_a"
node_id = "b"
namespace = "training"
work_dir = "~/.local/share/gpuq/clusters/training"
gpu_allowlist = [0, 1]
python = "/opt/conda/envs/train/bin/python"

[remotes.master_a]
host = "user@a.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

| 配置 | 默认值 / 用法 |
|---|---|
| gpuq.default_queue | local；设 cluster 后普通提交、查询、管理和 daemon 都使用池 |
| cluster.backend | 有 endpoints 时 etcd，否则 sqlite；worker 显式用 ssh |
| cluster.coordinator | ssh 模式必填，指向主机的远程 profile 名称 |
| cluster.node_id | 主机名；固定唯一，不随任务改变 |
| cluster.namespace | default；同一池两端一致 |
| cluster.work_dir | ~/.local/share/gpuq/clusters/namespace；持久工作区，主机额外存 control.sqlite3 |
| cluster.gpu_allowlist | 未设置表示本节点全部卡；数组限定贡献卡；空数组只做控制，不执行训练 |
| cluster.nodes | 默认仅当前节点 local；提交主机列出所有要复制输入的节点及 profile |
| cluster.lease_seconds | 15 秒，至少 10；租约失效检测，不代表自动选举其他主机 |
| cluster.python / requirements | python3 / 未设置；该执行节点的 Python 与可选快照内哈希锁文件 |
| gpuq.default_project_limit | 1；主机 init 时写入池策略，之后仅用于新项目初始上限 |

配置文件可单独命名为 `pool.toml`，用 `gpuq --config /path/pool.toml ...` 或 `GPUQ_CONFIG`
指定；包含 remotes 的同一个 TOML 就是持久 GPU 池配置，不需要每次重复传节点列表。

添加远程 c：在主机 cluster.nodes 增加 `c="worker_c"` 和相应 remotes 表，在 c 上配置
backend=ssh、coordinator、node_id 和贡献卡，然后启动 c 的 agent。新的提交会复制到新节点；
已有任务的 eligible_nodes/输入副本不会自动扩展。节点配置启动时读取，改贡献卡后重启该节点 agent；
修改提交端 nodes 后新的 submit 立即读取。不要给空 journal 复用旧 node_id，不要混用同一个工作目录的池。

## 一次性启动

主机 a 初始化全局策略（只运行一次，不覆盖已有策略）：

```bash
gpuq cluster init --max-running-jobs 2 --max-failover-retries 0
gpuq daemon --background
```

远程 b 启动执行 agent：

```bash
gpuq daemon --background
```

`default_queue=cluster` 让两端 daemon 自动进入池；也可显式 `gpuq --cluster daemon`。
若同一 GPU 上已有独立 gpuqd 服务，应先安排切换，避免两个队列重复调度。已有服务用原配置运行，
改配置不自动重启；服务的 GPUQ_CONFIG 也必须指向此池配置。默认后台就绪只表示已持有进程锁，
用 `gpuq cluster status` 检查主节点、worker 与 GPU 信息。这里关闭失联重跑以保持简单；
之前需要的有界节点失联重跑仍可在 init 配置，详见[重排语义](cluster_usage.md#失联任务自动重排可能重复执行)。
它只处理 worker 失联，不会把主机数据库迁移到 worker。

## 日常只提交任务

```bash
gpuq submit --name experiment-1 -- python train.py --seed 1
gpuq submit --name experiment-2 -- python train.py --seed 2
gpuq list --json
gpuq logs 1 --tail 100
gpuq cancel 1
```

例子中同项目默认允许 2 个任务、全局允许 2 个。调度器先满足显存、稳定采样、独占和 GPU 数量，
再优先选择**池内活跃任务数较少的卡**，然后考虑节点任务数/利用率等；分配后尚未启动的任务也计数。
因此两张合格且在线的卡通常各一个任务；不强制轮流投递到不合格/离线设备，不移动已运行任务。
多卡任务仍在单一节点执行，卡数不够不会拼接不同主机；GPU 允许共享，均衡不是“一卡最多一个任务”。
外部进程以实际显存、利用率和独占条件影响选卡，不计入池任务数量。

项目公平与优先级仍生效；如果同项目上限还是 1，第二项就等待。已有项目可以通过一次
`submit --project-limit 2` 持久调整。`--gpu` 可限制所有候选节点上允许的物理编号，通常省略让池自动选择。
`--gpus 2` 才申请两张卡。任务使用可见设备 0..N-1。

输入快照、HF/Torch 模型缓存、依赖环境与 checkpoint 规则复用[远程手册](remote_usage.md)。
提交前向配置的全部节点复制快照，任一失败则本次不登记全局任务；默认需要两份确认副本，
单节点简单池可 init --snapshot-copies 1。模型缓存各节点复用，输出不自动回传。

## 默认队列与恢复

普通 submit/list/show/logs/cancel/retry/priority/projects/gpus/doctor/daemon 使用 default_queue。
`--local` 显式访问原独立本地队列，`--remote HOST` 显式访问某台主机的独立队列，`--cluster` 显式访问池。
这三个选择互斥；`--state-dir` 不隐式选择本地队列。`gpuq init` 仍是独立本地初始化，
`gpuq cluster init/status` 始终面向池。旧任务不自动迁入，管理时注意队列 ID 的作用域。

没有设置 default_queue 的现有配置保持本地模式；有 endpoints 的旧 cluster 配置仍使用 etcd。
SSH 模式绝不在主机不可达时建立另一份本地队列。已授权的执行可继续，结果留在节点 journal；
主机重新可用时汇报结果、处理取消并继续分配。不要重复提交响应不明的任务，先查 list/show。
备份主机 control.sqlite3（采用 SQLite 备份机制）以及各节点 work_dir；不自动恢复丢失的输出/模型。

测试使用临时 SQLite、真实本地子进程模拟 SSH 和虚拟 GPU，覆盖两节点/同机双卡分散、固定主、
事务和断线边界；并非已在你的实际远程机器上部署或验收。

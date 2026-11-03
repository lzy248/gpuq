# 统一 GPU 池与主备切换

本页介绍可选的 **etcd 高可用模式**。只需要本地/远程统一调度且不要见证时，
使用 [SQLite + SSH 简单 GPU 池](pool_usage.md)。原有 endpoints 配置保持 etcd 模式。


[English](cluster_usage.en.md) · [工程总览](README.md) · [CLI 参数](cli_reference.md)

`--cluster` 是显式开启的全局队列：同一用户的本地和远程节点共同提供 GPU，
由一个选出的主节点分配任务。默认初始化主机为首选主；失联后备节点接管，
原主恢复并稳定同步后自动交回。已有本地队列和 `--remote` 独立队列不自动迁入。
每个多卡任务仍在一台主机执行，不支持跨主机 DDP。

## 三节点部署

需要三个独立故障域中的 **etcd v3 投票成员**（支持 v3 JSON gateway；集成测试使用 3.6.5）。
例如 A 为当前主机，B 为 GPU 服务器，W 为见证机。A/B 都运行 gpuq agent 和 etcd，W 只运行 etcd。
W 不需要 GPU、Python 或模型缓存，但它是持久保存全量控制面元数据的完整 Raft 成员，
不能用一个无状态 ping 服务替代。三成员允许一个成员失效；仅剩一个成员时不再授权新执行。
所有提交客户端和 agent 都需要访问 etcd 客户端端口；成员之间需要互通 peer 端口。

在三个主机上安装相同兼容版本的 etcd。以下是**隔离可信网络中的 HTTP 示例**，
将 `a.internal/b.internal/w.internal` 替换为实际可解析地址；每台分别设置自己的名字和 URL：

```bash
# A 用 a/a.internal；B 用 b/b.internal；W 用 w/w.internal。
ETCD_NAME=a
ETCD_URL=http://a.internal
etcd --name "$ETCD_NAME" --data-dir "$HOME/.local/share/gpuq-etcd" \
  --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls "$ETCD_URL:2379" \
  --listen-peer-urls http://0.0.0.0:2380 --initial-advertise-peer-urls "$ETCD_URL:2380" \
  --initial-cluster 'a=http://a.internal:2380,b=http://b.internal:2380,w=http://w.internal:2380' \
  --initial-cluster-token gpuq-pool-1 --initial-cluster-state new
```

实际长期部署用服务管理器保留数据目录并自动重启。不要开放无认证 etcd 到不可信网络：
队列包含命令和环境变量，写权限等同允许以该用户执行命令。使用 HTTPS、etcd 用户/角色授权，
为 client 和 peer 分别配置 `--cert-file/--key-file/--trusted-ca-file/--client-cert-auth` 与
对应 `--peer-*` 参数；证书 SAN 应匹配各 URL。gpuq 支持 CA、客户端证书/私钥和已有 token，
不自动签发证书或刷新 token。不要复制运行中的 etcd 数据目录来扩容；成员维护、备份恢复由 etcd 工具完成。

## gpuq 节点配置

A/B 安装同一版本 gpuq。A 的 `~/.config/gpuq/config.toml` 示例：

```toml
[cluster]
node_id = "a"
namespace = "training"
endpoints = ["http://a.internal:2379", "http://b.internal:2379", "http://w.internal:2379"]
work_dir = "~/.local/share/gpuq/clusters/training"
lease_seconds = 15
python = "/opt/conda/envs/train/bin/python"
# requirements = "requirements.lock"
# ca = "~/.config/gpuq/ca.pem"
# cert = "~/.config/gpuq/client.pem"
# key = "~/.config/gpuq/client-key.pem"
# token = "etcd-auth-token"

[cluster.nodes]
a = "local"
b = "gpu_b"

[remotes.gpu_b]
host = "user@b.internal"
command = ["~/.local/bin/gpuq"]
config = "~/.config/gpuq/config.toml"
```

B 使用相同 namespace/endpoints，node_id 改为 b。若也从 B 提交，配置 `b="local"`、
`a="gpu_a"` 及指向 A 的 SSH profile。运行 agent 本身不要求 SSH 到其他节点；复制输入和读取远程日志才需要。
每节点的 python/requirements 使用**该节点自己的 cluster 配置**，不使用 profile 的 python/requirements。
本机与远程 Python/CUDA/驱动兼容性仍需验证；锁文件缓存保证依赖输入一致，不承诺硬件环境完全一致。

| 配置 | 默认值 / 行为 |
|---|---|
| backend | 有 endpoints 默认 etcd，否则 sqlite；简单池 worker 使用 ssh |
| coordinator | ssh 模式必填，访问主机的 SSH profile |
| gpu_allowlist | 未设置贡献全部本机卡；数组限定物理编号，空数组不执行训练 |
| node_id | 主机名；固定唯一，限定字母、数字、点、下划线和连字符 |
| namespace | default；所有节点一致，不同值隔离队列 |
| endpoints | 必填非空 URL 数组；必须属于同一 etcd 集群 |
| work_dir | ~/.local/share/gpuq/clusters/namespace；持久 journal、输入、日志和缓存 |
| nodes | 仅当前 node_id → local；提交时必须配置至少两个实际不同节点 |
| lease_seconds | 15 秒，至少 10；失效检测不是精确时间保证 |
| python | python3；节点训练解释器 |
| requirements | 未设置；可选快照内自包含哈希依赖锁，缓存 venv |
| ca / cert / key / token | 未设置；HTTPS 信任/客户端证书/私钥、已有 etcd token |

配置在 agent 启动时加载。work_dir 独立于 `--state-dir` 和普通队列；节点身份写入 identity.json
并登记在 etcd，不得删掉执行 journal 后复用同一 node_id。一个工作目录只允许一个 agent。
不要用同一 node_id 启动多个不同目录，也不要让独立 daemon 同时调度这个池的同一组 GPU。

## 初始化、运行和提交

只需初始化一次；不覆盖已有全局策略：

```bash
# 在 A 上：默认首选主为当前 cluster.node_id。
gpuq cluster init --max-running-jobs 2 --snapshot-copies 2 --failback-seconds 30
# A 和 B 各启动一个 agent；前台便于服务管理器监控。
gpuq --cluster daemon
# 或 gpuq --cluster daemon --background
```

```bash
gpuq cluster status
gpuq --cluster submit --project experiment --project-limit 2 --gpus 2 -- python train.py
gpuq --cluster list --json
gpuq --cluster show 1 --json
gpuq --cluster logs 1 --follow
gpuq --cluster cancel 1
# 续训 checkpoint 按项目根目录的相对路径显式加入：
gpuq --cluster submit --sync-include checkpoints/resume.ckpt -- python train.py --resume checkpoints/resume.ckpt
```

提交先验证参数，再向配置的所有节点复制输入（默认排除权重/checkpoint；按哈希只传缺失内容），
收到至少 snapshot_copies 份确认后才登记全局任务。任一配置节点传输失败会中止本次提交；
已传缓存保留，下次复用。`--sync-preview` 只展示清单。`--no-sync/--remote-cwd/--log` 不适用于集群。
模型可在每个执行节点通过 HF 下载并缓存；模型缓存不在节点之间自动复制。续训文件须显式包含。
执行时创建独立快照；任务修改不会污染缓存。节点只为持有该输入副本的任务提供资源。

全局策略由 `cluster init` 写入 etcd，优先于各机 `[gpuq]` 的调度模式/总上限；默认 fair、
不设总上限、新项目普通任务上限 1。submit 的 `--project-limit N` 持久更新全局项目上限。
总上限 2 且 A/B 项目竞争时普通任务按 1+1 准入；只有一个项目时可用其配置的 2 个名额。
正优先级保留优先权，运行任务不抢占。`--gpu` 是执行节点上的物理卡编号；显存/余量均按卡。
全局依赖只等待成功，**不搬运上游输出**；共享数据、输出保存和跨节点续训需自行安排。

## 选主、回切与失败语义

etcd 负责多数派投票和复制；gpuq 候选节点通过租约绑定的事务争取调度主身份，两层主节点不必在同一台机器。
所有新分配都比较主身份、任务、项目和节点版本。新主不会复用旧任期发送新分配。
节点接受分配后用持久执行编号去重；提交响应丢失时先 `list/show` 检查，**不要盲目重复提交**。
同一个 execution 去重不等于多次 submit 自动合并。

首选主恢复后先读取权威队列、核对本地执行并发布 ready；连续被当前主观察到稳定达到
failback_seconds 后，通过事务转移领导租约。默认 30 秒；已有训练不重启。恢复节点、etcd 仲裁主、
可执行节点是不同概念。切换等待租约、网络请求及轮询，不承诺固定 RTO。

失去多数派时不能授权新执行；此前已经接受的执行和独立 runner 可以继续，结果保存在本机，
重连后上报。失联 assigned/starting/running 先保留全局名额；达到下述重排条件时，旧执行被
标记 superseded 并释放逻辑名额，新执行重新参与全局准入。取消请求中的任务不会自动重排。
这不是进程迁移：旧训练可能仍在执行，资源与外部副作用可能重复。

OOM 重试重新走全局准入，保留显存学习与累计次数；固定模式预算跨节点保留。
集群关闭本机自动 orphan 重跑（不采用 max_orphan_retries）；节点确认进程消失后报告 failed/orphaned，
可人工 `gpuq --cluster retry ID`。未知活跃任务不能手动 retry，但可由下述有界失联策略替换。
保留原 journal 恢复节点以取消旧执行；整盘丢失时不能复用原 node_id，本版不自动恢复丢失的数据。

全局待调度任务在没有节点提供 offer 时保持等待，包括尚无足够显存的节点；不会仅根据某一台
机器的容量把整个池的任务判失败。节点已接受的任务仍执行本地不可能显存检查。

## 失联任务自动重排（可能重复执行）

默认策略是**至少执行一次**：其他可运行任务清空，**或**节点失联超过 3600 秒，自动重排，
每个全局任务最多 1 次。初始化参数均持久保存在 etcd，所有主备共用；旧配置没有这些字段时保持关闭，
不会因升级隐式启用。初始化不会覆盖已有策略。

| cluster init 参数 | 默认值 | 行为 |
|---|---|---|
| --failover-after-seconds S | 3600 | 从主节点首次观察到租约心跳消失计时；0 禁用超时触发 |
| --failover-when-idle / --no-failover-when-idle | 开启 | 是否在其他可运行工作清空后触发 |
| --max-failover-retries N | 1 | 每任务累计自动失联重排预算；0 完全关闭 |

```bash
# 仅等待 2 小时后重排，最多 2 次（新建集群时）：
gpuq cluster init --failover-after-seconds 7200 --no-failover-when-idle --max-failover-retries 2
# 若不能接受重复训练：
gpuq cluster init --max-failover-retries 0
```

“其他可运行工作”指在线节点上的活跃任务，以及依赖已成功、至少一个输入副本节点在线的待运行任务；
它包含暂时等待显存的任务，不包含被未完成依赖阻塞或输入副本全离线的任务。是否空闲按当前队列快照
判断；并发新提交会使重排事务失效。超时触发不要求其他工作结束，但替代执行仍受总/项目/GPU 上限。
没有在线的其他副本节点时继续等待。一次只重新排一项，避免同时集中重跑。

lost_since 持久化，主备切换不重置计时；原节点恢复心跳会清除本次失联时间，下一次失联重新计时。
时长使用主节点 UTC 时钟，部署应同步节点时钟，时钟偏移会影响触发时刻。重排预算跨主切换、OOM 和
人工 retry 持久保留，--reset-attempts 不清失联预算；OOM 尝试数也不因换节点清零。

每次重排生成新 execution 和独立目录，并保存旧执行的 superseded 原因/时间。旧节点恢复后先取消本机
被替代执行，再上报 cancellation_ack；旧完成结果不能覆盖新执行。若旧任务其实已经完成但结果未送达，
仍可能重跑。网络分区期间无法保证立即停止旧进程，不承诺 exactly-once。

训练环境提供 GPUQ_CLUSTER_JOB_ID、GPUQ_EXECUTION_ID、GPUQ_FAILOVER_COUNT，便于输出分区或幂等写入。
独立工作目录不能隔离用户显式指定的共享绝对路径、数据库或远程上传，需按 execution 区分此类副作用。
新的快照使用原提交输入，不自动从失联机器提取最新 checkpoint；如原命令未配置续训，会从头开始。
`show --json` 展示 failover_count、lost_since（本地时间带 offset）和旧执行取消确认，不能把逻辑完成当成
旧机器上进程已经停止。

## 运维和验证边界

服务需设置 `KillMode=process`，ExecStart 使用 `gpuq --config /path/config.toml --cluster daemon`。
普通 gpuqd.service 不会自动变为集群 agent。后台启动确认的是进程锁，不保证 quorum 或 GPU 已就绪；
用 cluster status/--cluster doctor 与 agent.log 检查。运行期间不自动改成员、迁移旧任务或清理缓存。

etcd 记录每条限 1 MiB、单次读取限 16 MiB；目前队列全量扫描，适用于个人小型池，非大规模调度器。
备份 etcd 与各执行节点 work_dir；只备份队列元数据不能恢复 checkpoint 或本地执行日志。

测试使用虚拟 GPU 和临时目录：

```bash
pytest tests/test_cluster.py
GPUQ_TEST_ETCD=/path/to/etcd pytest tests/test_cluster_etcd.py
```

后一命令启动真实三成员 loopback etcd，检查成员故障、接管、回切、旧版本写入拒绝、租约与多数派丢失。
传输测试使用本地假 SSH 驱动真实子进程。两者都不是跨物理主机网络故障或真实 CUDA 训练验收；
实际部署后需另做断网/重启与代表性训练验收。

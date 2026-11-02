# SSH 远程调用

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/remote.py](../../src/gpuq/remote.py)。

## 职责与边界

把本地 CLI 参数通过 OpenSSH 交给远程同一 CLI。任务、状态、GPU、daemon、日志与依赖均归
远程队列，不做跨主机联合调度。本地选中 --remote 后不初始化本地队列。
同步实现见 [sync](sync.md)，环境见 [environment](environment.md)，用户说明见
[中文](../remote_usage.md) / [English](../remote_usage.en.md)。

## 接口与数据

普通 SSH 服务显式拒绝嵌套 --cluster/cluster 命令；集群的输入/日志走独立
[cluster-transfer](cluster-transfer.md) 协议，不能用普通远程转发冒充全局调度。


remote_profile 读取本地显式 --config、GPUQ_CONFIG 或默认路径的 [remotes.NAME]；
未知名称直接作为 SSH 主机别名/IP/user@host。profile 字段：host、port、identity、command
（字符串或 argv 数组）、config（远程配置）、state_dir、work_dir、cwd（no-sync）、python、requirements。
命令行 --ssh-port/--ssh-identity/--remote-command/--remote-config/--remote-workdir 覆盖 profile；
全局 --state-dir 指远程状态目录。python 是远程解释器，不能假定本地环境路径远程存在。

协议 v2：stdin 首行 JSON（上限 16 MiB）包含 protocol、argv、sync 与可选 manifest、local_root、
relative_cwd、project、work_dir、python、requirements。同步时远程 stdout 首行回应 protocol/missing
内容哈希，再由客户端流式发送 tar blob；之后 stdout 原样流式传递 CLI 输出。非同步直接执行。
服务端仅支持 submit 附带快照，拒绝嵌套 remote。两端必须安装兼容版本。

### 代码调用契约

| 入口 | 输入 → 输出 | 副作用 / 调用方 |
|---|---|---|
| remote_profile(args) | CLI namespace → 已验证 profile dict | 只读本地 TOML；dispatch 调用 |
| request_for(args, argv, profile) | 已通过 parser 的原始 argv → (request, 本地 root 或 None) | 保留命令 remainder；不上传、不入队 |
| dispatch(args, argv) | 本地解析参数 → 远程退出码，Ctrl-C 为 130 | 预览除外会启动 SSH；stdout 流式转发，进度写 stderr |
| serve() | stdin 请求/可选 tar → handler 退出码 | 生成快照和环境后调用远程 command_submit；其他命令直接调用各 handler |

协议的 python_command 布尔标记表示客户端已经替换了首个 Python 命令；服务端跳过该
远程解释器的本地路径重映射。managed 环境即使使用自定义解释器名称，也据此切换到 venv Python。
请求不是任务记录：只有最终 command_submit → QueueDB.submit 成功才产生 job ID。
快照目录名与 job ID 无对应关系，实际路径以 Job.cwd 为准。

| 失败阶段 | 持久结果 |
|---|---|
| 本地参数/清单检查 | 不创建远程任务；preview 也不连接远程 |
| blob 接收/物化/环境准备 | 清理本次快照；已验证 blob 和完整旧环境保留 |
| CLI submit handler 校验 | 可能留下未关联快照；不存在自动回滚整个工作区 |
| 入队成功后连接中断 | 任务可能已存在；禁止自动重提，应查询同一远程队列 |

## 关键流程与不变量

SSH 使用 -T、BatchMode、连接超时 10 秒、存活检测 15 秒/3 次，保留 OpenSSH 主机验证。
只有远程可执行 argv 进入经过 shlex.quote 的 shell；用户任务 argv/env 作为 JSON 走 stdin。
不搜索/删除命令 remainder 中的参数；仅剥离本地传输选项。不要把 --remote 当作任务命令的一部分解析。

默认 submit 从本地 cwd 检测根目录并同步；项目身份 hostname:绝对根目录，不随快照路径变化，
--project 可跨客户端指定共同身份。远程 work_dir 默认为 ~/.local/share/gpuq/workspaces。
每次创建 job-* 快照；相对 cwd 即使为空目录也会重建。项目内绝对 argv、KEY=路径、PYTHONPATH、
日志路径转换为快照路径。shell 命令字符串不作隐式路径替换。
--no-sync 需要显式 --remote-cwd 或 profile.cwd，使用既有远程 checkout；requirements 管理模式不适用。
profile.python 替换首个 python/python3.x 命令并在绝对路径时前置其 bin 到 PATH（除非显式 --env PATH）。
profile.command 未指定时先找远程 PATH 的 gpuq，再退到 ~/.local/bin/gpuq；完整字段默认值见用户手册。
no-sync 不调用 materialize/prepare_environment，也不注入 workspace 模型缓存变量；配置 requirements 时拒绝。
相对 --log 由远程 CLI 按其进程 cwd 解析，不自动以快照 cwd 为基准；默认日志仍在远程状态目录。
项目内绝对路径重映射仅针对单独 argv、KEY=VALUE/PYTHONPATH，不改写 shell 脚本正文。

## 失败与限制

不自动安装 gpuq、不复制 Conda/venv、不配置 SSH 密钥。网络故障不回退本地、不自动重提；
提交响应丢失可能已经入库，重试前先查询。远程非零退出码保留；握手/参数错误退出 2。
--sync-preview 仅本地文件清单，不连接远程，也不是预估缺失 blob 的网络操作。
提取/环境准备失败删除当次快照；已缓存 blob 保留。提交 handler 失败可能留下未关联快照，
保留快照/缓存无自动 GC，清理需避开运行、重试及依赖任务。传输不是安全沙盒或跨用户存储隔离。
断开日志跟踪只关闭客户端连接，不取消已提交训练。后台 daemon 需显式启动，见 [scheduler](scheduler.md)。

## 修改与验证

协议变更同步客户端/服务端和版本号；新增本地选项必须检查剥离边界及 -- 后字面参数。
运行 `pytest tests/test_remote.py tests/test_cli.py tests/test_environment.py`。
测试通过假 SSH 执行真实 CLI 双端与数据库，不证明真实网络/驱动环境可用。

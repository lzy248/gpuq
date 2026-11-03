# 入口与包导出

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/__init__.py](../../src/gpuq/__init__.py)、[src/gpuq/__main__.py](../../src/gpuq/__main__.py)、[packaging/gpuq_entry.py](../../packaging/gpuq_entry.py)。

## 职责与边界

将 Python 安装、`python -m` 和 PyInstaller 二进制统一接到 CLI 或内部 runner。
只路由，不负责调度。调用链见 [CLI](cli.md)、[runner](runner.md)、[构建](operations.md)。

## 接口与数据

新增内部 --gpuq-pool-store 持久 SSH 服务，调用单主 SQLite CAS；无需常驻网络监听。


CLI 还识别内部 --gpuq-cluster-call（SSH 输入复制/日志协议），显式 --cluster 与 cluster
子命令进入 [cluster](cluster.md)。训练 runner 入口不变，仍面向节点本地 journal；
仅存在 cluster 表不会切换模式；设置 gpuq.default_queue=cluster 后，服务下次启动会运行池 agent。
原独立本地后台启动显式传 --local，避免被默认路由切换。


| 调用 | 实际入口 |
|---|---|
| `gpuq ...` | pyproject 的 `gpuq.cli:main` |
| `gpuqd ...` | `gpuq.cli:daemon_entrypoint`，把 `daemon` 插到参数最前面 |
| SSH 内部服务 | `gpuq --gpuq-remote-call`，由 CLI 路由到 [remote](remote.md)；双方必须支持协议 v2 |
| `python -m gpuq ...` | `__main__.py` 调用 CLI main |
| Python 安装的内部执行器 | 当前 `sys.executable -m gpuq.runner --state-dir ... --job-id ...` |
| 冻结二进制的内部执行器 | 当前可执行文件 `--gpuq-internal-runner --state-dir ... --job-id ...` |

包级导出 `GPUState`、`Job`、`JobStatus`；`__version__` 当前为 `0.3.0`。
版本同时存在于 pyproject；构建脚本从 Python 包读取版本，发布工作流从 tag 定位产物。

## 关键流程与不变量

二进制入口显式导入 CLI 和 runner，让 PyInstaller 发现二者。只有第一个参数为
`--gpuq-internal-runner` 时移除该参数后进入 runner，否则走普通 CLI。
不能让冻结程序去调用 `python -m gpuq.runner`，目标机器可能没有 Python。
SSH 内部请求只有 argv 恰好为 [--gpuq-remote-call] 时由 CLI 分流；其数据来自 stdin，
不是 runner 入口。后台启动在 Python 安装中使用 sys.executable -m gpuq，在冻结程序中
直接使用当前二进制，均传入 state/config 与 daemon 覆盖参数。管理依赖环境另需远程 Python/venv。

## 失败与限制

内部 runner 需要已领取的数据库记录，不能用它绕过提交/调度。
`gpuqd` 把 daemon 放在最前；全局 `--state-dir`/`--config` 推荐通过
`gpuq --state-dir ... --config ... daemon` 或环境变量传递，别假设放在子命令之后仍可解析。
当前源码包含未必进入既有发布二进制的功能，`--version` 不能替代安装来源核实。

## 修改与验证

入口变更联动 scheduler 的 `runner_command`、pyproject scripts、打包 smoke test。
运行 `pytest tests/test_scheduler.py -k entrypoint` 与 `pytest tests/test_cli.py -k version`。
有构建依赖时按 [运维](operations.md) 构建二进制，再测普通和内部 runner 的 `--help`。

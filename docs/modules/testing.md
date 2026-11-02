# 测试地图与验证边界

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[tests/conftest.py](../../tests/conftest.py)、[tests/test_cli.py](../../tests/test_cli.py)、[tests/test_db.py](../../tests/test_db.py)、[tests/test_exclusive_external_sharing.py](../../tests/test_exclusive_external_sharing.py)、[tests/test_gpu.py](../../tests/test_gpu.py)、[tests/test_multigpu.py](../../tests/test_multigpu.py)、[tests/test_orphan_recovery.py](../../tests/test_orphan_recovery.py)、[tests/test_runner.py](../../tests/test_runner.py)、[tests/test_scheduler.py](../../tests/test_scheduler.py)、[tests/test_docs.py](../../tests/test_docs.py)、[tests/test_projects.py](../../tests/test_projects.py)、[tests/test_remote.py](../../tests/test_remote.py)、[tests/test_environment.py](../../tests/test_environment.py)。

## 职责与边界

pytest 验证队列契约，全部自动测试应能在没有物理 GPU 的 Linux 上运行。
[工程总览](../README.md) 提供完整安装/验证命令；[文档设施](documentation.md) 说明结构检查。
测试通过不证明 CUDA/NCCL、真实 GPU 驱动兼容或断电后训练 checkpoint 正确。

## 接口与数据

| 文件 | 覆盖内容 / 修改入口 |
|---|---|
| conftest.py | 临时 Settings/QueueDB；submit_job 工厂，默认 fixed=4000MB+1000MB reserve、stable=2、notify=False |
| test_cli.py | UTC 存储与 show/list 文本与 JSON 本地时区显示及原记录不变、参数别名、默认 auto、提交/JSON、init、TTY 分页、日志 tail/follow 和 Ctrl-C |
| test_db.py | 序列化、优先级、依赖、迁移、终态 retry、auto 校准、带条件失联更新 |
| test_gpu.py | nvidia-smi CSV 样本和空进程输出 |
| test_scheduler.py | FakeBackend/FakeClock/gpu/mark_running 辅助；稳定、资源门槛、选卡、预留、auto 释放、独占、OOM 容量、入口和 systemd |
| test_exclusive_external_sharing.py | 显式卡外部共享；不能突破本队列独占和资源余量 |
| test_orphan_recovery.py | 有界持久计数、旧 boot/PID 复用、存活 worker、取消/终态不重排、配置 |
| test_runner.py | 真实 CPU worker 的环境/日志/退出、OOM 解析/学习/有界重试、普通失败、取消 |
| test_multigpu.py | 整组领取、逐卡复查/预留、旧单卡迁移、JSON、恢复、独立 session rank 的环境/显存/取消 |
| test_projects.py | 项目限额/轮次、单项目2与多项目1+1、旧库迁移、OOM身份保留、配置校验 |
| test_remote.py | 假 SSH 的真实 CLI 双端协议、增量/隔离/checkpoint、路径/命令、后台 daemon、环境复用 |
| test_environment.py | 环境身份/失败清理、锁复用与真实离线空锁 venv/pip |
| test_docs.py | 文档检查器正向路径与缺归属、坏链接/锚点、遗漏章节、重复归属、孤立文档的拒绝路径 |

## 关键流程与不变量

使用 tmp_path 为每个用例创建独立状态目录，不连真实 ~/.local/state/gpuq。
FakeBackend.sample 返回可修改的 GPUState 列表，便于模拟二次采样资源消失；
process_memory_mb 可返回 None。FakeClock.advance 推进单调时间，不为稳定窗口真实等五分钟。
需伪造 active 时写 claim/mark_running/worker_pid，并注入 process_checker，避免测试自己的假 PID
被当作失联。多卡 launcher 回调从 Job.assigned_gpus 读取完整分配。

真实进程测试会用 fork、Popen、setsid、/proc 和信号；必须在 finally 回收自己创建的子进程。
独立 session 的测试启动器显式转发 SIGTERM，用无关旁观进程验证隔离。
测试不应发送真实通知或依赖训练库。

## 失败与限制

现有 tests 工厂的 fixed 默认与 CLI auto 默认不同，写测试时必须显式选择想验证的路径。
没有独立 config/notify 全面覆盖；配置部分由 CLI/共享策略/恢复测试间接覆盖。
GPU CSV、通知交付、取消竞态及日志跨 attempt 的覆盖不能仅从测试绿灯推断完整。
受限容器若禁止进程/信号或 /proc 访问，应报告环境限制，不能宣称硬件行为已验证。

新增测试文件：[tests/test_projects.py](../../tests/test_projects.py) 验证项目限额、总并发、轮次重启、
迁移与配置保留；[tests/test_remote.py](../../tests/test_remote.py) 用真实 CLI 子进程和本地假 SSH
验证协议、增量去重、隔离、checkpoint、命令转义和远程管理；
[tests/test_environment.py](../../tests/test_environment.py) 验证环境身份、复用、失败清理、后台锁，
并用空锁文件执行离线真实 venv/pip 测试。没有真实远程 SSH、GPU/HF 下载验证。
资源历史测试 fixture 显式 fifo，公平测试另启 fair，避免隐藏原有资源策略断言。

## 修改与验证

| 修改区域 | 最小针对性命令 |
|---|---|
| 项目公平/限额/迁移 | `pytest tests/test_projects.py` |
| SSH/同步/后台/环境 | `pytest tests/test_remote.py tests/test_environment.py` |
| CLI/配置/输出 | `pytest tests/test_cli.py tests/test_exclusive_external_sharing.py tests/test_orphan_recovery.py` |
| 数据/状态 | `pytest tests/test_db.py tests/test_orphan_recovery.py tests/test_multigpu.py` |
| 调度/显存 | `pytest tests/test_scheduler.py tests/test_exclusive_external_sharing.py tests/test_multigpu.py` |
| 进程/OOM | `pytest tests/test_runner.py tests/test_multigpu.py` |
| 文档基础设施 | `python scripts/check_docs.py` 和 `pytest tests/test_docs.py` |

提交运行时变更前完整 `ruff check src tests scripts/check_docs.py`、`pytest`、`python -m build`。
新增测试文件也要更新本文和归属清单；测试结果报告具体命令与限制，不把固定通过数量写成契约。

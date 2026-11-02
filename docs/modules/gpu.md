# GPU 发现与 backend 协议

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/gpu.py](../../src/gpuq/gpu.py)。

## 职责与边界

只读取硬件快照并解析，不选择设备、不管理进程、不保留显存。
被 [scheduler](scheduler.md)、CLI gpus/doctor 调用，返回 [模型](models.md) 对象。

## 接口与数据

`GPUBackend` 协议：`sample() -> list[GPUState]`；
`process_memory_mb(pid) -> int | None`。测试可注入 FakeBackend，无须 NVIDIA。

`NvidiaSMIBackend(executable='nvidia-smi', timeout_seconds=10)` 每次 sample 执行两条查询：
设备 CSV 字段为 index/uuid/name/memory.total/memory.used/memory.free/utilization.gpu/temperature.gpu；
进程 CSV 字段为 gpu_uuid/pid/used_gpu_memory。参数为 `--format=csv,noheader,nounits`，
通过 UUID 合并，所有显存值按 MB 使用。

## 关键流程与不变量

`parse_gpu_csv` 跳过不足 8 列的行；`parse_process_csv` 跳过不足 3 列、无进程消息和
无法解析的进程行。`_number` 将空、N/A、Not Supported 当作 0，其他值 int(float(...))。
子进程使用 argv，不使用 shell；check=True，捕获 stdout/stderr，超时异常上抛。
`process_memory_mb` 会重新采样并返回第一个匹配 PID 的用量，不是所有 GPU 上用量的和。
多卡计量应使用 scheduler 的逐卡快照/进程树逻辑。

## 失败与限制

两份 CSV 不是驱动提供的原子快照。未知显存被转成 0，不能将未观测解释为任务没有用显存。
退出失败、超时、坏设备数字等可抛异常；daemon 对 OS/subprocess 错误记录 monitor error，
doctor 将采样异常显示为失败。当前不是 ROCm 发现器，即使 runner 能识别 HIP OOM 文本。

## 修改与验证

改变协议联动所有 FakeBackend、scheduler 峰值/预留、CLI JSON。
运行 `pytest tests/test_gpu.py tests/test_scheduler.py tests/test_multigpu.py`。
新增解析能力用脱敏 CSV 样本测试；不要依赖本机型号/PID。现有解析测试不保证所有驱动版本格式兼容。

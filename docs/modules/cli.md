# 命令行适配层

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/cli.py](../../src/gpuq/cli.py)。

## 职责与边界

`build_parser` 定义用户接口，`command_*` 负责输入转换和输出，业务委托给
[数据库](db.md)、[调度器](scheduler.md)、[配置](config.md) 和 [GPU backend](gpu.md)。
完整用户选项见 [中文 CLI](../cli_reference.md) / [English CLI](../cli_reference.en.md)。

## 接口与数据

| 命令 / 函数 | 输入到输出或副作用 |
|---|---|
| `main(argv=None)` | 调用 handler，最终 `SystemExit(code)`；不是返回整数的库 API |
| `init` / `command_init` | 创建状态目录/DB；目标配置不存在时写示例，已有文件不覆盖 |
| `submit` | `--` 后保留 argv；校验 cwd、环境键、GPU 编号；持久化 Job 并打印 ID/log |
| `list` / `ls` | 默认取 200 条；TTY 且超过默认 20 条/页时翻页；JSON/管道不进入交互屏幕 |
| `show` | 普通键值或 `Job.as_dict()` JSON，包含完整多卡分配 |
| `cancel` | 调用 cancel_job，默认 10 秒后升级 SIGKILL |
| `retry` | 只接受终态；可覆盖 memory/mode，`--reset-attempts` 重置 attempt |
| `priority` | 更新指定任务优先级 |
| `logs` / `log` | 尾部行数 `-n`/`--lines`/`--tail` 默认 100，允许 0，`-f` 每秒轮询 |
| `gpus` | 从真实 backend 采样，输出文本或 JSON |
| `doctor` | 初始化/打开 DB，检查采样；GPU 检查失败返回 1 |
| `daemon` | 传入 Settings、once、dry_run 到 run_daemon |

提交默认 auto：入库 `memory_mode=auto, memory_mb=1, retry_on_oom=True`。
`--gpus`/`--gpu-count` 默认 1；`--gpu` 可重复/逗号分隔并去重。
`--env KEY=VALUE` 只拆第一个等号、校验变量名；命令不通过隐式 shell 执行。
日志默认位于状态目录 logs，文件名包含时间、清洗过的名称和随机后缀。
多卡列表显示全部物理编号和 `Nx` 每卡资源；文本是展示，JSON 才适合程序读取。

## 关键流程与不变量

所有全局选项放在子命令之前。`--` 后的参数属于训练命令。
`_job_page`/交互终端必须在 finally 中还原 termios、光标与 alternate screen。
`_tail_lines` 用有界 deque，读整个文件但内存随行数上限增长；0 行不读取旧内容。
follow 到达终态后结束，Ctrl-C 返回 130。普通参数错误及 handler 的
ValueError/KeyError/RuntimeError 转为 argparse 错误（退出 2）。

## 失败与限制

`command_init` 写配置的路径使用显式 `--config` 或默认路径，尚未完全遵循
加载阶段的 `GPUQ_CONFIG` 选择；使用自定义路径时显式传 `--config`。
`--poll-seconds` 会重建 Settings，目前未复制 `exclusive_shared_gpus` 和
`max_orphan_retries`；需要它们时通过 TOML 调整采样间隔。
日志 follow 不处理轮转/截断；初始 tail 与定位 EOF 之间没有原子快照。
`doctor` 会触碰状态目录/DB；dry-run 也不是只读，详见 [调度器](scheduler.md)。

## 修改与验证

新增 Job 参数联动 parser → command_submit → QueueDB.submit → Job/JSON → 用户双语手册。
新增 Settings 字段检查 command_daemon 的覆盖分支和 init 模板。
运行 `pytest tests/test_cli.py tests/test_multigpu.py`；关注参数剩余部分、默认 auto、
JSON、分页、零行 follow、无效 GPU 数。不要用真实队列验证展示。

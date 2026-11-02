# 尽力而为的通知

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/notify.py](../../src/gpuq/notify.py)。

## 职责与边界

`notify(*, title, message, job_id, command) -> None` 启动本地通知进程。
[runner](runner.md) 在结果已记录后调用；通知不改变任务状态，也不是邮件/远程消息队列。

## 接口与数据

command 是 argv 序列或 None；配置字符串到 argv 的转换在 [config](config.md)。
逐参数替换 `{title}`、`{message}`、`{job_id}`，不使用 shell、没有 shell 转义语义。
无显式 command 时用 PATH 中的 notify-send；不可用时静默返回。

## 关键流程与不变量

显式 command 优先，启动失败不会再自动回退 notify-send。
Popen stdout/stderr 都指向 DEVNULL；OSError 被吞掉。函数不 wait，不确认送达，
通知慢或失败不应阻塞任务完成写回。Job.notify=False 由 runner 提前拦截。

## 失败与限制

外部命令可能有任意用户权限副作用，配置文件必须可信。无桌面会话时 notify-send 可能失败；
成功 Popen 不代表最终命令成功。当前没有专门通知测试文件，不能声称已有完整交付测试。

## 修改与验证

改变模板或阻塞/失败策略时联动配置说明与 runner._send。
运行 `pytest tests/test_runner.py` 检查调用方；新增通知逻辑应 mock shutil.which/subprocess.Popen，
覆盖占位符、无程序、OSError 和优先级，禁止测试发送真实通知。

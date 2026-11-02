# Contributing / 参与贡献

## English

Thanks for improving gpuq. Small, focused pull requests are easiest to review.

Start with the [engineering guide](docs/README.md) and follow [AGENTS.md](AGENTS.md).
Every maintained code/config/test file must have a module owner in
[module-map.json](docs/module-map.json). Update module contracts with behavior changes;
new modules use the [documentation template](docs/templates/module.md).

1. Open an issue for behavior changes or scheduling-policy changes.
2. Fork the repository and create a branch from `main`.
3. Install development tools:

   ```bash
   python -m pip install -e '.[dev]'
   ```

   Use Ruff 0.16.6 from this extra. `pyproject.toml` pins both the required version
   and the project's original rule set (`E4`, `E7`, `E9`, `F`) so local checks and
   CI use the same policy; rule-set changes should be reviewed explicitly.

4. Add or update tests. Tests must not require a physical GPU; use the fake backend.
5. Run:

   ```bash
   python scripts/check_docs.py
   ruff check src tests scripts/check_docs.py
   pytest
   python -m build
   ```

   To reproduce the standalone binary, install `.[binary]` and run
   `scripts/build_binary.sh` on Linux x86_64.

6. Update both English and Chinese documentation for user-visible behavior.

Please preserve the core safety contract: gpuq may control only its own task process
groups. It must never kill, pause, or allocate placeholder memory against unrelated
GPU users. New retry paths must be bounded by a precise failure class; generic code
errors must not become automatic retry loops.

Do not include private training logs, tokens, hostnames, user paths, or proprietary
commands in issues and tests.

## 中文

感谢帮助改进 gpuq。范围清晰、改动集中的 PR 最容易审查。

先读[工程总览](docs/README.md) 和 [AGENTS.md](AGENTS.md)。所有维护的代码、配置、测试
文件都必须在[归属清单](docs/module-map.json) 中关联模块说明；行为变更同步说明，新增模块
使用[文档模板](docs/templates/module.md)。结构检查通过后仍需人工核对文档语义与实际实现。

1. 调度策略或外部行为变更请先提交 issue。
2. Fork 仓库并从 `main` 创建分支。
3. 安装开发依赖：

   ```bash
   python -m pip install -e '.[dev]'
   ```

   请使用此依赖组中的 Ruff 0.16.6。`pyproject.toml` 同时锁定工具版本及项目原有的
   `E4`、`E7`、`E9`、`F` 规则，确保本地与 CI 使用同一口径；扩展规则须明确评审。

4. 新增或更新测试。测试不得依赖真实 GPU，应使用虚拟 backend。
5. 运行：

   ```bash
   python scripts/check_docs.py
   ruff check src tests scripts/check_docs.py
   pytest
   python -m build
   ```

   如需复现独立二进制，请在 Linux x86_64 安装 `.[binary]` 后运行
   `scripts/build_binary.sh`。

6. 用户可见行为必须同时更新中英文文档。

请保留核心安全契约：gpuq 只能控制自己创建的任务进程组，绝不能终止、暂停其他
用户任务，也不能用占位显存影响他人。新增重试路径必须对应精确定义的失败类别，
不能让普通代码错误进入自动循环。

issue 和测试中不得包含私有训练日志、token、主机名、用户路径或专有命令。

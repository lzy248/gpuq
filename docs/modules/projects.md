# 项目根目录与身份

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[src/gpuq/projects.py](../../src/gpuq/projects.py)。

## 职责与边界

project_root(cwd) 给本地提交、旧记录迁移和远程同步提供稳定根目录。
本模块只识别目录，不保存策略、不调度任务；策略见 [数据库](db.md) 和 [调度器](scheduler.md)。

## 接口与数据

输入 str/Path，经 expanduser/resolve 返回绝对 Path。向上首先找最近 .git 文件或目录
（含 Git worktree 标记），没有时找最近 pyproject.toml/package.json，再没有使用 cwd。
本地默认 project_id 为根目录字符串。--project 可覆盖；远程同步默认 hostname:本地根目录，
不能用每次随机 job-* 快照目录作为项目身份。

## 关键流程与不变量

同一 Git 项目的不同子目录归同项目；显式身份相同即共享并发限制和轮次。
旧 DB 根据 cwd 回填身份；此后重试/OOM/失联恢复保留，不因运行路径改变而重算。
远程多个客户端若需同项目轮转归组，应使用相同 --project。

## 失败与限制

没有项目标记时不同 cwd 会得到不同身份；移动目录默认会创建新身份。
标记存在不等于 Git 仓库完整；同步时 Git ls-files 失败会中止，不退回扫描全部目录。
不解析 Git remote URL，不提供跨主机全局配额。

## 修改与验证

运行 `pytest tests/test_projects.py tests/test_remote.py`。修改识别规则需考虑旧任务迁移、
子目录快照、显式身份、项目策略保留；同步更新用户说明，避免意外扩大上传根目录。

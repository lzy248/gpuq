# 文档设施与维护流程

[返回工程总览](../README.md) · [工程规范](../../AGENTS.md)

覆盖文件：[docs/module-map.json](../../docs/module-map.json)、[scripts/check_docs.py](../../scripts/check_docs.py)。

## 职责与边界

让零上下文 agent 通过 [总览](../README.md) 和模块说明获得工程背景。
[AGENTS.md](../../AGENTS.md) 是自动发现的规范入口；双语 README 是用户入口。
清单约束文件归属，检查器验证结构，人工复核保证语义准确。不是源码自动生成 API 站点。

## 接口与数据

`docs/module-map.json` 格式：schema_version=1，modules 数组，每项包含 doc（仓库相对
模块 Markdown 路径）和 files（该模块负责的准确文件路径列表）。每个维护的非 Markdown 文件
必须且只能出现一次；Markdown 的组织由导航/链接检查覆盖。某些操作说明可显式归属已有 Markdown。
共享一个模块说明必须有共同职责，不能把所有源码塞进一个无细节的清单。

`python scripts/check_docs.py` 从脚本位置推断仓库根目录；`--root PATH` 用于隔离测试。
依赖 Python 标准库和 Git，不导入/执行 gpuq 源码、不联网、不启动任务、不修改被检查文件。
成功退出 0 并打印检查统计；失败退出 1 并列出可定位的错误。

## 关键流程与不变量

检查器使用 `git ls-files --cached --others --exclude-standard` 包含已跟踪与尚未提交文件，
因此新增代码未登记也会失败。生成物必须由 .gitignore 排除；不会因为尚未 git add 就漏掉文件。

检查项：清单版本/条目、所有非 Markdown 文件的唯一归属、文件和模块文档存在；每个模块
说明包含五个必需章节且给出其覆盖文件的可点击链接；所有维护 Markdown 的本地内联链接、
HTML href/src 和 Markdown 标题锚点有效；总览直接链接每份模块说明；全部维护 Markdown
可从 AGENTS/双语 README/总览入口到达。

代码围栏内示例不当作链接；http/https/mailto 等外部链接不联网验证。
锚点按普通 GitHub 风格标题生成（含重复标题后缀）；新文档使用普通内联 Markdown 链接，
避免复杂嵌套目标或引用式链接。检查器不证明外部链接可用、代码示例可执行或语义已经同步。

功能文档验收遵循 [AGENTS.md](../../AGENTS.md) 的逐项核对规则：每完成一组接口/行为改动立即同步代码文档，再进入下一组；先列行为变化与文档位置，
原位更新模块契约、关键 docstring、参数表、配置示例和能力描述，反查 parser/配置加载器并搜索旧断言。
新增专题或文末概述不能替代这些入口；同时检查任务路由表列数和架构图覆盖，
检查器不验证 Mermaid 渲染或 Markdown 表格对齐。结构检查与人工语义核验分别报告。

维护步骤：复制 [模板](../templates/module.md) → 填真实契约和验证入口 → 登记精确文件 →
更新总览导航/任务路由 → 更新受影响的用户双语说明 → 跑检查 → 人工对照实现与测试。
移动/删除文件同时更新所有入口。文档变更与相关代码同一交付，不保留空壳或待填占位说明。

## 失败与限制

未安装 Git、目录不是 Git 工作区、路径越出仓库、归属重复、失效锚点或孤立文档都不能通过。
不检查实现是否符合叙述；例如默认值变更而描述仍旧需要评审发现。
不会机械要求每个私有 helper 独立文档；AGENTS 要求的输入/输出/副作用/异常/联动必须人工确认。

## 修改与验证

运行 `python scripts/check_docs.py`、`pytest tests/test_docs.py`、
`ruff check scripts/check_docs.py tests/test_docs.py`。CI 同样执行文档检查。
检查器测试在临时 Git 仓库构造缺归属、失效链接/锚点、缺章节、重复归属、不可达文档，
避免一个始终返回成功的脚本成为虚假门禁。

人工零上下文审计：从总览找一个任务（新参数、schema、多卡、取消、发布），
只读对应 Markdown 应能说明修改路径、关键安全约束、测试命令和覆盖缺口；不满足就继续补说明。

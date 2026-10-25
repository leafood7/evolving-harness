# 通用任务评测示例

这些小型项目是专门构造的教学夹具，不是生产缺陷或真实收益证据。两个任务分别覆盖普通业务修改和模块导入修改。真实项目可以使用相同 JSON 配置协议，不依赖 log-inspector 的目录或接口。

## 零模型离线演示

从 Harness 仓库根目录运行：

```powershell
python examples/evaluation/offline_demo.py --output .harness/eval-demo-001
```

输出目录必须不存在。演示用脚本替代模型，但实际运行 Python 测试与接口探针，生成 HTML / JSON 报告。两组都成功，预期为两个平局。报告中的 fixture/scripted 模型标识不是真实提供商。它验证流程，不验证 Skill 收益。

## 在 Pi 内运行真实对照

1. 在当前项目范围创建待测 Skill：`/harness package edit skill:verify`。记录返回的组件 ID；不必先启用候选。
2. 将 suite.json 的 `candidate` 替换为该组件 ID。`baseline` 可填旧版组件 ID，或 null 表示不注入 Skill。也支持 `skill:名称`，此时准备阶段解析并固定当前活动版本。
3. 用 `native --allow-write` 打开包含此配置和源码的工作区。在 Harness 根目录运行示例时，依次输入：

```text
/harness eval prepare examples/evaluation/suite.json
/harness eval run
/harness eval status
/harness eval report
```

prepare 仅创建独立源码副本、冻结任务与验收代码并显示完整载荷，不调用模型。run 显示源码、记忆、两版 Skill 和可信 Python 验收参数，经一次确认顺序执行整批尚未尝试的任务，每任务两组独立会话，随机组内顺序。每任务最多约5分钟，最多5个任务；每完成一个任务保存结果。关闭或超时中断的 pair 保持原状态，不自动重放；可继续未尝试的其他 case，需要重做时重新 prepare，并保留旧报告。

报告保存在数据库同级 `task-evals/eval_.../report.html` 和 `report.json`。HTML 不加载外部资源，所有证据文本经过 HTML 转义。报告包含每任务独立检查、最终回答、工具数量、耗时、可用用量与费用，附任务 ID 可关联原始事件。缺失费用不当成零。建议只分待完成、保留当前、拒绝晋升、证据不足、人工复核，不自动启用候选。

参与此入口的候选增加一道启用门槛：评测未完成、出现退步、失败或无结论、跨任务混用模型时，拒绝启用。为相同候选重建一份通过的评测不能抹掉旧失败；应修改为新版本再验证。平局允许继续原有契约评估和人工确认，但不自动算收益。没有参加通用任务评测的组件保留原有门槛，不伪称所有 Skill 已完成真实任务评测。

## 配置规则

顶层固定字段：version=1、name、baseline、candidate、cases。每份1—5个 case；每个 case 包含：

- id：不重复的简单英文名称。
- source：相对 JSON 所在目录的源码文件夹，必须仍在当前工作区内。
- prompt：任务要求。
- protected：不可改动的既有文件相对路径；AGENTS.md、tests/ 下既有文件和验收直接引用的既有 .py 脚本自动保护。可新增测试。
- checks：1—3项，每项有 name、args、require_tests。args 是当前 Python 解释器的参数数组，不经过 Shell。例如 `['-I','-X','utf8','checks/run_tests.py']`，JSON 中使用双引号。至少一项 require_tests=true；须退出0且输出 unittest 的非零测试数量。

检查参数和内联 `-c` 代码在 prepare 时固定，不向 Agent 提供评分提示。验收本身运行在用户权限下；只能使用自己信任的配置，不应导入来历不明的评测代码。被测代码也会在测试时执行。源码副本及无 Shell 参数执行不提供 OS 沙盒。

配置文件位于 source 内时会从复制文件集中排除，避免把本次验收代码一并交给模型。这是正常工作流中的隔离，不阻止具有当前用户权限的恶意程序读取副本外文件。

快照最多80个文本文件、单文件128KB、总计1MB，只覆盖 .py/.md/.toml/.json/.jsonl/.log/.txt；跳过隐藏文件、.git/.harness、task-evals/skill-comparisons 运行产物、依赖与缓存。该有界文件集合不等于全盘完整性检查。拒绝符号链接和目录联接。只支持 Python unittest 计数，不是多语言通用测试解析器。

若两组都通过，结果为平局；候选单独通过才记为本任务改善，反之为退步。执行异常、保护文件被改或原项目快照变化不能算有效改善。新增测试数量、调用更少、耗时更短或检查更多均不自动证明更好。可信脚本的退出状态与输出也不是抗恶意 Agent 的形式化保证。

当前没有 LLM 裁判、Langfuse、Playwright 或语义质量评分；先用独立可执行规则和证据建立可复现比较，再扩展适配器。

已有测试文件是只读的：即使仅追加测试、没有删除断言，也不能修改；新增回归应写到新的 tests/test_*.py。准备后的两组任务会收到相同明确约束。当前是在提示中告知、验收前后核对并拒绝违规，不是操作系统或工具写入层面的强制拦截。

新的失败证据提供 violations 数组，列出相对路径、changed/missing/unreadable_or_unsafe 等原因；内容变化记录原/现文本哈希，不把整份源码放入诊断。源码集合超限等问题单列 workspace_invalid，避免误说为受保护测试被修改。完整性或检查器副作用导致的结果不算有效收益。旧记录不补写或改分，事后定位应另存审计文件。

# Log Inspector

本地命令行日志分析工具，Python 3.11+，无第三方依赖。第一阶段支持级别和时间筛选，保留输入顺序，不读取或发送任何网络数据。

从项目根目录运行：

```text
python -m log_inspector examples/app.log
python -m log_inspector examples/app.log --level ERROR --level WARNING
python -m log_inspector examples/app.log --since 2026-10-24T09:02:00+08:00 --until 2026-10-24T01:04:00Z --json
python checks/run_tests.py
```

输入格式：`2026-10-24T09:03:00+08:00 ERROR 数据库连接超时`。时间必须包含时区；时间窗口包含两端，按绝对时间比较。WARN/FATAL分别归一化为WARNING/CRITICAL。

结果写入stdout，诊断及统计写入stderr。`--json`输出JSON Lines；可用 `> result.jsonl` 保存结果。默认输入编码UTF-8（允许BOM），其他编码可显式传 `--encoding gb18030`。

## 异常聚合报告

`--aggregate`将匹配的日志按“日志级别＋完整消息”分组，每组输出出现次数、最早与最晚时间（按绝对时间比较）。聚合在级别筛选（`--level`）和时间筛选（`--since`/`--until`）之后执行，支持 `text` 与 `jsonl` 两种输入。

普通文本输出每行格式为：

```text
次数 级别 最早时间 最晚时间 消息
```

例如：`2 INFO 2026-10-24T01:00:00+00:00 2026-10-24T02:00:00+00:00 ok`。

叠加 `--json` 后，每组输出一行 JSON，字段为 `level`、`message`、`count`、`earliest`、`latest`。

`--top N`仅在 `--aggregate` 模式下使用，输出次数最多的前 N 组；按次数降序排列，同次数时按级别名称与消息排序（保证结果稳定）。

```text
python -m log_inspector examples/app.log --aggregate --top 3
python -m log_inspector examples/app.jsonl --input-format jsonl --aggregate --json
```

默认跳过并报告格式错误，退出码0表示文件处理完毕，不表示每行有效。`--strict`遇到首条格式错误返回2；此前已输出的结果不会撤销。参数、读取或编码错误返回2。空白行不算格式错误，诊断最多逐条显示10项，统计保留全部数量。

测试：从项目根目录运行 `python checks/run_tests.py` 执行全部测试；测试失败或未发现测试时退出码非0。也可用底层命令 `python -m unittest discover -s tests -v`。

后续真实需求：聚合重复异常、输出统计报告，以及大文件/多行异常处理。当前不支持自动识别格式、堆栈多行合并或排序。

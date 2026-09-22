"""内置样板 Skill（v10）：commit / review / test。

随程序分发（三级中的最低优先级），同名项目级 / 用户级文件可覆盖。
兼作编写模板：SOP 用编号步骤写清执行流程。
"""

from __future__ import annotations

COMMIT_SKILL = """---
name: commit
description: 按项目规范生成 Git 提交
mode: shared
tools: [ReadFile, Glob, Grep, Shell]
---

# commit Skill

按以下步骤生成提交：

1. 用只读工具查看本次改动涉及的文件（优先看最近修改的文件），归纳改动主题。
2. 检查是否存在临时文件、密钥或调试残留不应入库的内容；有则先指出并停止。
3. 起草提交信息：第一行不超过 50 字的中文概述，空一行后补充改动要点列表。
4. 把起草的提交信息完整展示给用户确认后再执行 git 提交命令。
5. 提交命令执行后，回报实际的提交结果；失败时给出原因与修正建议。

约束：绝不使用 --no-verify 跳过钩子；绝不代用户 push。
"""

REVIEW_SKILL = """---
name: review
description: 按严重度审查代码改动
mode: shared
tools: [ReadFile, Glob, Grep]
---

# review Skill

按以下步骤审查代码：

1. 列出本会话涉及的全部文件与改动点，先给一句总体评价。
2. 逐文件检查：正确性（边界条件、错误处理）、安全性（注入、越权、敏感信息）、可维护性（命名、重复、复杂度）。
3. 发现的问题按「严重 / 一般 / 建议」三档分组输出，每条给出文件位置、问题说明与修复建议。
4. 没有问题的方面也要明确列出「已检查且通过」，避免沉默通过。
"""

TEST_SKILL = """---
name: test
description: 在独立会话中运行测试并摘要回流
mode: isolated
tools: [ReadFile, Glob, Grep, Shell]
context: recent
---

# test Skill

在隔离会话中执行测试并汇报：

1. 根据任务判断测试范围（单个文件 / 模块 / 全量），选择对应的测试命令。
2. 执行测试命令，收集输出。
3. 如有失败：读取失败用例的源码与被测代码，判断失败原因（代码缺陷 / 测试过期 / 环境问题），给出修复建议。
4. 输出结构化结果：通过数 / 失败数、失败用例清单（含原因判断）、建议的下一步。
"""

BUILTIN_SKILLS: tuple[tuple[str, str], ...] = (
    ("commit", COMMIT_SKILL),
    ("review", REVIEW_SKILL),
    ("test", TEST_SKILL),
)

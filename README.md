# EikoCode

**Windows 终端里的轻量 AI 编程助手。** 纯 Python、纯本机运行：在终端里跟模型多轮对话，让它读你的代码、搜你的项目、改你的文件、跑你的命令——但每一步都按你确认的边界走。

设计原则只有一条：**可预测优先于智能**。上下文只由你显式改变，工具不做任何静默的摘要 / 截断 / 丢弃；模型不会在你看不见的地方偷偷跑一堆写操作，中断不留半成品。

启动后会长出一只白色小猫：

```
   /\_/\     EikoCode v0.1.0
  ( ^.^ )    模型：deepseek-chat
   > ^ <
  /|   |\
 (_|   |_)
```

## 特性一览

| 能力 | 一句话说明 |
| --- | --- |
| 多轮对话 + 工具系统 | 读文件 / Glob / Grep / 写改文件 / 跑 PowerShell；只读并发、写串行 |
| 安全纵深防御 | 危险黑名单红色拦截、路径沙箱、显式规则、三档权限、人在回路 |
| 提示词装配 | 稳定 / 变化分离命中供应商缓存，环境变化原样追加、不回写历史 |
| MCP 客户端 | 配置即接入本地子进程与远程服务，远端工具与内置无差别调用 |
| 上下文压缩 | 超大工具结果落盘留预览；历史过长生成结构化摘要，用户原话逐字保留 |
| 项目指令与记忆 | `EIKOCODE.md` 指令注入、会话存档恢复、长期笔记自动维护 |
| 命令框架 | 集中式注册、Tab 补全、提示符状态段 |
| Skill / Hook / 子工作者 / 工作树 / 小组协作 | 多种扩展与多任务机制，见下文 |

## 安装

需要 Python 3.11+，以及一个支持工具调用（tool_use / function calling）的模型。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 配置（只需做一次）

```powershell
.\scripts\setup.ps1
```

交互式询问 API Key / Base URL / 模型名（Key 输入时不显示在屏幕上），写进 Windows 用户级环境变量，之后任何新开的终端都自动生效。

```powershell
.\scripts\setup.ps1 -Show     # 查看已配内容（Key 只露前 6 位）
.\scripts\setup.ps1 -Clear    # 清除配置
```

也可以只在当前窗口临时设置（关掉窗口就失效）：

```powershell
$env:EIKOCODE_OPENAI_API_KEY  = "sk-..."
$env:EIKOCODE_OPENAI_BASE_URL = "https://api.deepseek.com/v1"
$env:EIKOCODE_MODEL           = "deepseek-chat"
```

- 模型名以 `claude` 开头走 Anthropic 原生协议，其余走 OpenAI 兼容协议（DeepSeek / 通义 / 本地 Ollama / vLLM）
- 至少设置一个凭据，否则启动时直接提示缺凭据并退出
- 凭据只存在系统环境变量里：不在项目目录、不会被 git 提交，EikoCode 本身没有任何把密钥写盘的代码路径

## 运行

```powershell
.\scripts\start.ps1      # 推荐：自动切目录、校验环境、从注册表兜底读配置
python -m eikocode       # 也行，但要保证当前窗口读得到环境变量
```

想省掉敲命令，直接双击项目根目录的 `启动 EikoCode.bat`。

## 内置命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 列出全部命令 |
| `/model` | 查看当前模型；`/model <name>` 切换模型 |
| `/usage` | 显示上下文用量与轮次 |
| `/plan` | 只规划模式：写 / 执行类工具被拦截，只跑只读工具产出计划 |
| `/auto` | 切换工具自动批准；开启后危险命令仍拦截 |
| `/mode` | 权限档位：`strict`（只读也确认）/ `default`（写与执行确认）/ `permissive`（放行） |
| `/mcp` | 查看外部工具服务状态；`/mcp reload` 重建连接 |
| `/compact` | 手动上下文压缩；`/compact status` 查看用量 |
| `/sessions` `/resume` `/notes` | 会话列表 / 恢复历史会话 / 查看与清空自动笔记 |
| `/skills` | 查看已加载 Skill、详情与重扫 |
| `/worktree` | Git 工作树隔离：create / enter / exit / delete / list |
| `/team` | 小组协作：create / status / dispatch / merge / close |
| `/tasks` | 后台子任务列表、详情、终止 |
| `/review` `/explain` | 预设提示词命令 |
| `/clear` `/exit` | 清空会话 / 退出（`/reset` `/quit` 等价） |

## 工具与安全

模型可以调用 6 个内置工具：

| 工具 | 权限 | 作用 |
| --- | --- | --- |
| `ReadFile` | 只读 | 读取一个文本文件（≤ 1 MB） |
| `Glob` | 只读 | 按文件名模式返回匹配路径列表 |
| `Grep` | 只读 | 按内容正则搜索，返回 `文件:行号:内容` |
| `WriteFile` | 写 | 创建 / 覆盖文件，沿用原文件换行符 |
| `EditFile` | 写 | 一次多段替换，全部唯一匹配才落盘 |
| `Shell` | 执行 | 跑 PowerShell 命令，工作目录跨调用延续 |

权限规则：

- **只读工具自动通过**；写文件、跑命令需要你确认
- **危险命令红色拦截**：`rm -rf`、`format`、`shutdown`、`reg delete`、写物理盘等必须输入完整的 `yes` 才放行，不受任何开关影响
- 嫌确认烦就 `/auto on`：只读、写文件、非危险命令直接执行，事后可查，不是静默执行
- 每次工具调用经过**五层裁决**：危险黑名单 → 路径沙箱 → 显式规则 → 权限档位 → 人在回路，每层裁决都说明依据

可预测的工具行为：

- `EditFile` 原子落盘：任一段不匹配整次不写入；匹配不唯一报错；文件被外部改动后拒绝覆盖
- 工具超时 120 秒，杀掉整个进程树不留孤儿
- 连续失败超过 3 次停止本轮，把问题交还给你
- 工具结果计入上下文用量，触及 95% 上限时拒绝发送，绝不自动截断你的历史

`.eikocode.toml`（可从 `.eikocode.toml.example` 复制）支持声明安全规则和调整默认值：

```toml
auto_approve = false
max_tokens = 8000

[[rules]]
tool = "Shell"
match = "*pytest*"
action = "allow"          # allow / deny / ask

[[mcp_servers]]
name = "local-tools"
command = "npx"
args = ["-y", "some-mcp-server"]
```

## 上下文压缩

长会话的 token 大头是工具结果。压缩只动会话历史，你的原话一个字不丢：

- **预防（无 LLM）**：单个工具结果超过 20,000 字符时，完整原文写入 `.eikocode/snapshots/`，消息里只留预览和路径
- **兜底（调 LLM）**：累计用量到 90% 时自动生成结构化摘要，最近 3 轮保持原文，用户原话逐字保留；摘要连续失败 3 次即熔断
- 用量 80% 提醒、95% 拒绝发送；`/compact` 随时手动压缩

## 项目指令与记忆

- 在项目根放一份手写的 `EIKOCODE.md`（技术栈、编码规范、注意事项），启动时自动注入；支持 `@路径` 引用其他文件
- 对话自动存档，`/resume <会话id>` 恢复后接着聊
- 长期笔记自动维护四类内容：用户偏好、纠正反馈（用户级）与项目知识、参考资料（项目级）；`/notes` 查看

## 扩展能力

- **Skill**：`.eikocode/skills/` 放「YAML 元信息 + Markdown 指令」的能力包，两阶段加载、工具白名单最小权限；已激活的 Skill 自动注册成 `/命令`
- **Hook**：`.eikocode/hooks.yaml` 写「事件 + 条件 + 动作」规则，工具执行前可拦截并回写原因，让模型自己调整重试
- **子工作者**：模型通过 `Agent` 工具启动子任务（预定义角色或 Fork 继承上下文），后台运行、完成自动通知，三层防嵌套
- **工作树隔离**：`/worktree create` 以 git worktree 为任务提供文件系统级隔离，带变更保护与过期清理
- **小组协作**：`/team` 让 Lead 拆任务派发成员并行执行，共享任务清单与消息邮箱，完成后自动合并

## 设计取舍

- **上下文只由你显式改变。** 不自动摘要、不自动截断、不静默丢弃历史轮次
- **中断即回滚。** 半截回复不进上下文；工具循环中取消，本轮整轮不回写
- **失败可见。** 网络、鉴权、限流、超时各有固定文案，不抛堆栈
- **不静默降级。** 功能不可用就明确报错并说明原因，不悄悄换条路
- 语义检索、图形界面、插件市场不在当前范围内；批处理只做「只读并发、写 / 执行串行」

## 故障排查

**提示「未找到 API Key」**
环境变量没生效。确认是同一个终端会话里设置的；或直接用 `start.ps1` 启动（会从注册表补读）。

**中文显示为问号或乱码**
启动时已强制 UTF-8 编码。如果仍乱码，检查终端字体，或换用 Windows Terminal。

**长回答被截断**
带思考的模型会把思考算进 `max_tokens` 额度，把 `.eikocode.toml` 里的 `max_tokens` 调到 16000 左右。

## 开发

```powershell
pytest    # 离线跑，全部用假响应，不联网
```

想参与改进看 [CONTRIBUTING.md](CONTRIBUTING.md)；遇到问题欢迎提 [Issue](https://github.com/Eiko3/EikoCode/issues)。

## 开源协议

[MIT](LICENSE) © 2026 Eiko

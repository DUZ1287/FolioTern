# AgentRadar

面向个人学习与工程实践的 AI 技术研究 Agent。核心是可独立运行的 Python 程序；网页只是可选阅读界面，关闭浏览器不影响命令行研究和定时采集。

## 两种工作方式

**每日雷达 `run`**：从 GitHub Releases、仓库搜索和 arXiv RSS 采集公开资料，去重保存，按兴趣选候选。配置模型后生成中文分析，并针对重点条目有限补查；未配置模型时明确输出原文基础报告。

**自主研究 `research`**：给一句研究目标，模型通过原生 Chat Completions function calling 自主选择工具；读取观察结果，再决定继续查证或提交结论。任务状态、证据和执行记录保存在本机，可中断恢复。

```text
研究目标 → 模型决策 → 参数校验 → 只读工具 → 证据与观察
              ↑                              │
              └──────────────────────────────┘
                        ↓
              引用检查 → 中文结论 / 信息不足
```

## 开始使用（当前电脑）

项目位于 `D:\个人项目\AgentRadar`，独立 Python 环境已创建。

1. 双击 `配置模型.cmd`，在本机输入兼容 API 地址、模型名称和 API Key；密钥输入不显示。接口地址通常包含 `/v1`，不要附加 `/chat/completions`。
2. 双击 `开始研究.cmd`，输入研究目标。也可以在 PowerShell 执行：

```powershell
Set-Location 'D:\个人项目\AgentRadar'
.\.venv\Scripts\python.exe radar.py research '最近有哪些适合 Windows Python 项目实践的 Agent 记忆方案？比较原始资料与限制，给一个两小时的小实验。'
```

程序会输出当前调用的工具、返回状态、任务 ID 和报告路径。结果保存在 `reports/research/<任务ID>.md` 与 `.json`，不需要启动网页。

**当前尚未配置真实模型接口。** 缺少配置时 `research` 会明确退出，不伪装成模型完成的研究。已完成离线模拟模型的工具循环测试；真实服务的兼容性、输出质量和费用仍需填写配置后验证。

API 服务需要支持 Chat Completions 原生 `tools`/`tool_calls`。仅提供纯文本聊天的接口无法驱动这套研究循环。`config.json` 中的 `llm.token_parameter` 可设为 `max_tokens`（默认）或 `max_completion_tokens`，以匹配服务商要求。

## 常用命令

以下命令均在项目目录执行，`python` 指 `.venv\Scripts\python.exe`。

| 命令 | 作用 |
|---|---|
| `python radar.py configure` | 在终端安全填写本机模型配置 |
| `python radar.py doctor` | 检查 Python、模型配置完整性和本地记录数；不显示密钥 |
| `python radar.py run` | 手动生成今日日报，使用当前模型配置 |
| `python radar.py research "研究目标"` | 开始自主研究 |
| `python radar.py tasks` | 查看最近研究任务及状态 |
| `python radar.py research --resume 任务ID` | 使用已保存的证据和观察继续任务 |
| `python radar.py feedback 条目ID useful` | 记录有用反馈；也支持 saved、hard、ignore、completed |
| `python radar.py practice` | 从明确标记有用的条目中生成本周任务 |
| `python radar.py export` | 从数据库重新导出最近日报，无需重新采集 |
| `python radar.py serve` | 可选：打开仅绑定本机的阅读服务 |

日报保存在 `reports/latest.html`、`reports/YYYY-MM-DD.html` 和 `.md`。条目 ID 可在 `data/radar.db` 的条目记录、日报记录中查看，或使用可选网页操作反馈。

研究状态 `completed` 表示已形成结构化结论，其中 `outcome=insufficient` 表示证据不足，并不代表技术问题已经查清。`paused` 表示已保存进度；预算耗尽时等待次日或自行调整预算再恢复。恢复不会重复已完成的资料请求；已执行过且失败的相同工具参数会被阻止，可由模型选择其他证据路径。

## Agent 可以调用的工具

| 工具 | 能力与边界 |
|---|---|
| `search_projects` | 搜索公开 GitHub 仓库，最多 5 个候选；关注度不当作质量证据 |
| `read_project` | 读取指定 `owner/name` 的 README；区分项目自述和实测 |
| `read_releases` | 读取最多 3 条版本记录 |
| `search_papers` | arXiv 论文搜索，只读标题和摘要，明确未阅读全文 |
| `search_history` | 查询本地历史条目，不把旧记录当成独立核验 |
| `finish_research` | 提交中文发现、引用、建议和限制；引用 ID 必须确实存在 |

所有外部工具只能访问代码限定的 GitHub/arXiv 主机，不接受任意 URL。没有命令执行、自动安装、项目修改或消息发送工具。README、论文和历史中的指令不能扩大工具权限。

引用校验验证的是“资料确实取得，ID 有效”，不是语义正确性的证明；模型仍可能误读原文，需要你对重要结论核查。离线安全测试验证权限和程序约束，不声称彻底解决提示词注入。

## 自动运行

已安装当前用户的 Windows 任务 `AgentRadar-Daily-0830`，每天北京时间 08:30 执行 `scripts/run_daily.py`，普通权限、隐藏窗口，错过触发后尽可能补跑。无需保持网页打开。

本机需要开机、用户已登录且网络可用。任务不会唤醒关机电脑。采集日志存放 `logs/YYYY-MM-DD.log`；失败或部分采集会写入来源状态，已完成的其他来源继续使用。

```powershell
# 查看任务状态
Get-ScheduledTask -TaskName 'AgentRadar-Daily-0830'
Get-ScheduledTaskInfo -TaskName 'AgentRadar-Daily-0830'

# 重新注册此项目的任务
.\scripts\Install-Schedule.ps1

# 移除定时任务，保留代码、数据和报告
.\scripts\Remove-Schedule.ps1
```

脚本不会覆盖同名但指向其他项目的任务。若更换系统时区，请重新注册以重新换算北京时间。任务的 `run` 和手动 `research` 共用进程锁，避免同时执行。

## 配置和费用控制

- `config.json`：主题、来源、候选数量、每日调用预算、每次研究的步骤/工具上限。
- `.env`：本机模型地址、模型名称、API Key；可选 `GITHUB_TOKEN` 仅发送到 GitHub。`.env.example` 不含真实凭据。
- 默认每天最多 24 次模型请求，Token 预留上限 120000；失败和重试也计入，不因重启程序重置。每日预算按北京时间换日。
- 默认每次研究最多 8 个普通模型步骤加一次结束尝试，最多 12 个工具结果；任务的工具上限跨恢复累计。
- 单次任务时间目标为 600 秒，HTTP 请求受剩余时间和单请求超时约束。网络库的分阶段超时可能导致小幅超时；Windows 定时任务另有 12 分钟终止上限。
- Token 按可见输入的 UTF-8 字节数和输出上限保守预留，不是服务商账单的严格计费上限。不同模型可能有额外计费项，应同时设置供应商侧限额。
- 不配置价格时只记录用量，不编造费用。建议先用一个短问题验证模型配置，再运行全天候任务。

配置在本机不等于推理在本机。云端模型会接收研究问题、公开资料和必要偏好；密钥只用于目标模型服务的认证，不转发给资料来源，不写入报告和 Git。

## 数据与恢复

`data/radar.db` 保存条目版本、采集历史、日报、反馈、分析缓存、每日预算和研究任务。工具观察、证据与待执行队列一起提交；中断后可以继续，不能把暂停任务当成已完成。

精确去重按来源、外部 ID 和内容版本执行。arXiv 跨分类条目合并；论文和仓库之间尚未建立完整的跨源事件图，不能保证语义重复全部识别。RSS 是近期快照，搜索和发布记录也有限额，覆盖不足会明确标记 `partial`，不声称抓取了全部前沿进展。

版本发布接口触发限流时可以等待恢复，或在 `.env` 配置自己的 GitHub 令牌；不要把令牌发到聊天或写入代码。被限流的来源不会推进成功检查点。

## 验证与实际限制

测试使用临时数据库、模拟 HTTP 和模拟模型，不消费真实模型额度：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖采集格式、版本去重、限流/失败隔离、凭据隔离、参数校验、原生工具多步调用、证据 ID 校验、预算暂停与恢复、命令注入权限边界、页面写请求校验等。

2026-11-17 完整测试结果：**74 passed**。另有 1 条测试客户端的依赖弃用提示；Python 语法检查通过。

2026-11-17 已真实执行 `run` 并取得 116 条资料。该次 GitHub 发布接口限流，arXiv 与仓库搜索返回有数量上限的结果，生成的是基础报告。没有真实模型输出质量或连续一周稳定性的验证结果；这些不能写成已经达到的项目成绩。

当前环境：Python 3.14.9；安装版本在 `requirements.lock.txt`。后续在新环境可以安装锁定依赖，但本次没有另建第二个环境验证重装可复现性。

## 代码位置

- `agentradar/agent.py`：独立研究 Agent、持久任务状态、证据与恢复。
- `agentradar/model.py`：原生 Chat Completions 工具调用传输。
- `agentradar/research_tools.py`：5 个只读研究工具。
- `agentradar/pipeline.py`、`analysis.py`：每日采集后的筛选与有限研究。
- `agentradar/collectors.py`、`storage.py`：来源连接器及版本化 SQLite。
- `agentradar/reports.py`：Markdown/HTML 导出。
- `agentradar/web.py`、`assets/`：可选阅读与反馈界面。
- `scripts/`：Windows 自动运行与便捷入口。

## 接口资料

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [GitHub Releases API](https://docs.github.com/en/rest/releases/releases)
- [GitHub Search API](https://docs.github.com/en/rest/search/search)
- [arXiv RSS](https://info.arxiv.org/help/rss.html)
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)

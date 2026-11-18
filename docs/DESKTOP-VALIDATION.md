# 桌面升级验收记录

更新日期：2026-11-18。环境：Windows，Python 3.14，pywebview 6.2.1，PyInstaller 6.22.3。

## 自动化检查

- 原有 78 项测试通过，覆盖研究循环、暂停恢复、持久化、预算、来源隔离与 Web 功能。
- 桌面与配置新增 24 项测试通过：来源 CRUD/重复/启停/重启、模型凭据不回显、不入库、切换与删除，非法参数响应不包含密钥，RSS 私网/DNS/重定向/响应大小/压缩限制，固定 IP 与 TLS 域名，RSS 去重与关键词，模型工具观察往返，中文路径迁移，外观与调度持久化，单实例与关闭状态。
- 另有 5 项后台调度回归覆盖系统拒绝更新、移除任务、并发跳过和结果持久化。完整回归为 107 项通过，另有一条测试客户端弃用提示。

## 本机真实验证

- Python 桌面入口成功显示独立 Windows 窗口和 WebView2 内容；不是浏览器标签页。信息源页面显示 SQLite 中的 9 个默认来源。
- Windows Credential Manager 使用无敏感信息的测试值完成写入、读取、删除；没有留下测试密钥。
- 有界真实网络采集：GitHub Releases 返回 1 条符合时间范围的记录，通用 RSS 返回 3 条，arXiv 搜索返回 3 条。均明确标为 partial（到达配置数量上限），没有冒充完整覆盖。
- PyInstaller onedir 构建成功，约 44 MB，包含 EXE 与依赖目录。Windows 已实测打包程序独立启动、读取 11 个页面、创建来源、保存与删除测试凭据以及切换主题。

## 关窗采集与打包

2026-11-18 关窗实测使用隔离的中文数据目录。通过 EXE 的本机接口登记 Windows 每日任务，正常关闭窗口后手动触发该系统任务。GitHub Releases 限流，arXiv 成功收录设定上限的 2 条论文，SQLite 保存了 1 份日报并记录部分完成。验证后已移除临时任务。此检查验证系统任务动作，未等待次日定点触发。

打包过程中复现了 PyInstaller 从 PATH 选错同名 libexpat.dll 导致 SAX 解析失败的问题。构建配置现优先使用构建解释器的库，Windows 构建脚本在压缩前校验哈希。

## 尚待验证与后续工作

- 未配置真实模型档案：没有验证任何服务商的真实工具能力、研究质量、费用或长任务稳定性。
- 没有在不安装 Python 的干净 Windows 虚拟机上进行测试；本机存在 Python 与 WebView2。
- 尚无签名安装包、自动更新或系统托盘常驻。可直接运行 onedir 目录中的程序，自动任务页可登记或移除 Windows 计划。
- 通用 RSS 不使用系统代理；只有代理可达的网络可能失败。只允许 HTTPS 标准端口，不支持订阅认证或凭据 URL。
- 跨源事件聚合、证据可信度分级和更复杂的实践任务生成按后续阶段推进。

## 参考阅读

设计参考 [TrendRadar](https://github.com/sansan0/TrendRadar)、[Folo](https://github.com/RSSNext/Folo)、[GPT Researcher](https://github.com/assafelovic/gpt-researcher)。桌面 API 和打包参考 [pywebview API](https://pywebview.flowrl.com/api/) 与 [Freezing](https://pywebview.flowrl.com/guide/freezing.html)。未复制这些项目代码或 UI 素材。

# Windows 桌面升级实施记录

## 审查结论

保留现有 Function Calling 循环、TaskJournal、Store、Budget 和命令行。新增桌面宿主和配置服务；模板继续复用 FastAPI。数据目录与程序资源目录分离，旧 CLI 默认目录不变。配置修改不得覆盖历史数据库，模型缓存继续区分服务地址与模型，恢复任务记录模型配置签名，切换后要求重新建任务。

## 参考项目（2026-11-17 查阅）

- [TrendRadar](https://github.com/sansan0/TrendRadar)：README 和 trendradar/__main__.py 展示采集、存储、分析及调度分层。借鉴来源状态和关键词筛选；不引入它的推送系统。
- [Folo](https://github.com/RSSNext/Folo)：README 与 apps/desktop/package.json 展示 Electron 桌面与阅读产品结构。借鉴订阅导航和内容/配置分离；本项目沿用 Python，避免引入另一套 Node 构建链。
- [GPT Researcher](https://github.com/assafelovic/gpt-researcher)：gpt_researcher/agent.py 区分研究、上下文、证据和报告职责。借鉴可观察的阶段状态；保留现有单 Agent 循环。

只参考设计模式，未复制第三方代码或图像。实现依据 pywebview 官方 API、Windows/WebView2 与 PyInstaller 文档。

## 执行顺序

1. A：pywebview 宿主，随机回环端口、单实例、关闭协作、LOCALAPPDATA 数据目录、迁移入口，保留 CLI。
2. B：浅色冰透玻璃主题，深色/低透明度、减少动画、响应式导航。阅读正文保持不透明底色。
3. C：SQLite 信息源档案，输入/重复检查，CRUD、测试、采集与状态；通用 RSS 使用解析并固定公网 IP 的 TLS 连接，拒绝重定向及私网，限制响应大小。
4. D：多模型档案与 Windows Credential Manager，预算、启用、测试基本响应及原生工具往返。无模型仍可采集。
5. E：onedir 构建、资源检查、本机 EXE 启动验证；未有干净 Windows 虚拟机时不声称完成无 Python 机器验收。

## 验收原则

原测试继续运行，新增配置重启持久化、SSRF、模型能力验证、桌面路径/迁移测试。真实网络、模拟模型、桌面启动、干净机器验收分别记录。未填写模型密钥时不得将模拟结果称作真实模型研究通过。

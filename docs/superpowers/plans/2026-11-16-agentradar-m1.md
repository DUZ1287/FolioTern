# AgentRadar M1 Implementation Plan

> 实施状态（2026-11-17）：本计划的基础闭环已落地，并扩展为模块化每日流水线及独立研究 Agent。以下代码保留为初始计划示意，当前运行方式以项目 README.md 为准；最终实现不要求照搬单文件结构。

> **For agentic workers:** Use the executing-plans skill to implement this plan task-by-task in the current session. Steps use checkbox syntax. This plan does not request subagent delegation.

**Goal:** 在 Windows 上运行一个命令，从 GitHub Releases 和 arXiv RSS 收集条目，保存 SQLite，并生成直接可打开的基础 HTML 报告。

**Architecture:** 同步采集、来源隔离、SQLite 唯一键去重、转义后的静态 HTML。此阶段不调用 LLM，不创建定时任务；输出明确为基础信息列表，不冒充已完成中文分析的智能日报。

**Tech Stack:** Python、httpx、feedparser、SQLite、pytest。开发时核验本机 Python 和依赖兼容性，将实际测试版本写入锁定文件，不在计划里猜测最新版。

---

## 边界与文件布局

根目录：`D:\个人项目\AgentRadar`。当前只有设计与计划，以下源文件均为待创建。

- `requirements.in`：运行依赖。
- `requirements-dev.in`：测试依赖。
- `radar.py`：M1 命令入口、采集和报告；控制在一个小模块，M2 按设计拆分。
- `tests/test_radar.py`：离线行为测试。
- `tests/fixtures/github.json`、`arxiv.xml`：固定响应样本。
- `.gitignore`：忽略凭据、虚拟环境、数据库、报告和缓存。
- `README.md`：运行方式、阶段限制和结果状态。
- `data/radar.db`、`reports/latest.html`：执行后产生，非源代码。

M2 再扩展陌生项目搜索、全文/内容版本、中文分析、预算、重试、检查点和定时。M1 使用有限来源响应，不能声称已完整采集过去的所有消息。

## Task 1：建立可运行环境

- 环境观察（2026-11-16）：文档检查尝试通过 `py` 编译 Python 示例，但启动器返回 `No installed Python found!`，示例尚未完成语法或运行验证。这仅说明该启动器未发现安装，不能证明机器上不存在其他 Python 环境。
- [ ] 检查本机 Python：`py --version`。若启动器不可用，执行 `Get-Command python,python3,uv -ErrorAction SilentlyContinue` 检查可用解释器或环境管理器，再核实其版本。使用受支持且依赖兼容的 Python 环境，记录具体路径和版本；下述 `py` 命令按已核实的解释器路径替换。
- [ ] 创建 `requirements.in`：

```text
httpx
feedparser
```

- [ ] 创建 `requirements-dev.in`：

```text
-r requirements.in
pytest
```

- [ ] 创建 `.gitignore`：

```gitignore
.venv/
.env
data/
reports/
__pycache__/
.pytest_cache/
```

- [ ] 在项目目录执行以下命令；每条成功后再执行下一条。安装阶段需可用网络，安装失败不声称完成。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.in
```

预期：虚拟环境生成，依赖安装成功。不要修改系统 Python 的包集合。

## Task 2：固定采集、去重、失败隔离与页面安全的验收行为

- [ ] 创建 `tests/fixtures/github.json`：

```json
[{"id":123,"name":"Fixture release","tag_name":"v0.1","html_url":"https://github.com/example/project/releases/tag/v0.1","published_at":"2026-11-16T00:00:00Z","draft":false}]
```

- [ ] 创建 `tests/fixtures/arxiv.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fixture feed</title><link>https://arxiv.org</link><description>Offline test</description><item><title>Fixture paper</title><guid>https://arxiv.org/abs/2611.00001</guid><link>https://arxiv.org/abs/2611.00001</link><pubDate>Mon, 16 Nov 2026 00:00:00 GMT</pubDate></item></channel></rss>
```

- [ ] 创建 `tests/test_radar.py`：

```python
from pathlib import Path
import sqlite3
import httpx
from radar import collect, save, render, run

FIXTURES = Path(__file__).parent / "fixtures"

def client(fail_github=False):
    def respond(request):
        if request.url.host == "api.github.com":
            if fail_github:
                return httpx.Response(503)
            return httpx.Response(200, content=(FIXTURES / "github.json").read_bytes())
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
    return httpx.Client(transport=httpx.MockTransport(respond))

def test_two_sources_and_duplicate_storage(tmp_path):
    with client() as c:
        items, statuses = collect(c)
    assert len(items) == 2
    assert all(s["status"] == "ok" for s in statuses)
    db = tmp_path / "radar.db"
    save(db, items, statuses)
    save(db, items, statuses)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2

def test_failed_source_does_not_block_other_source(tmp_path):
    with client(fail_github=True) as c:
        code = run(c, tmp_path)
    html = (tmp_path / "reports/latest.html").read_text(encoding="utf-8")
    assert code == 1
    assert "Fixture paper" in html
    assert "error: HTTPStatusError" in html

def test_all_failed_report_replaces_stale_report(tmp_path):
    with client() as c:
        assert run(c, tmp_path) == 0
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as c:
        assert run(c, tmp_path) == 2
    html = (tmp_path / "reports/latest.html").read_text(encoding="utf-8")
    assert "Fixture paper" not in html
    assert "本次没有取得条目" in html

def test_external_text_is_escaped_and_unsafe_link_disabled():
    html = render([{"title": "<script>alert(1)</script>", "url": "javascript:alert(1)",
                    "source": "fixture", "published_at": None}], [])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "发布时间未知" in html
```

- [ ] 执行 `.\.venv\Scripts\python.exe -m pytest -q`。在实现前应因 `radar` 尚不存在而失败；确认失败来自缺少实现，不是环境损坏。

## Task 3：实现完整 M1 闭环

- [ ] 创建 `radar.py`，使用以下完整初始实现：

```python
import argparse
import calendar
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx

SOURCES = (
    ("github", "https://api.github.com/repos/langchain-ai/langgraph/releases?per_page=20"),
    ("arxiv", "https://rss.arxiv.org/rss/cs.AI"),
)

def now():
    return datetime.now(timezone.utc).isoformat()

def collect(client):
    items, statuses = [], []
    for source, url in SOURCES:
        try:
            response = client.get(url)
            response.raise_for_status()
            if source == "github":
                entries = response.json()
                batch = [dict(source=source, external_id=str(e["id"]),
                              title=e.get("name") or e["tag_name"], url=e["html_url"],
                              published_at=e.get("published_at"))
                         for e in entries if not e.get("draft")]
            else:
                feed = feedparser.parse(response.content)
                if feed.bozo:
                    raise ValueError("Invalid RSS")
                batch = []
                for e in feed.entries:
                    parsed = e.get("published_parsed")
                    published = (datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc).isoformat()
                                 if parsed else None)
                    batch.append(dict(source=source, external_id=e.get("id") or e.link,
                                      title=e.title, url=e.link, published_at=published))
            fetched = now()
            for item in batch:
                item["fetched_at"] = fetched
            items.extend(batch)
            statuses.append(dict(source=source, status="ok", count=len(batch)))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            statuses.append(dict(source=source, status="error: " + type(exc).__name__, count=0))
    return items, statuses

def save(path, items, statuses):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS items (
            source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, published_at TEXT, first_seen_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL, PRIMARY KEY(source, external_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, statuses TEXT NOT NULL)""")
        for i in items:
            conn.execute("""INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                title=excluded.title, url=excluded.url, published_at=excluded.published_at,
                fetched_at=excluded.fetched_at""",
                (i["source"], i["external_id"], i["title"], i["url"],
                 i["published_at"], i["fetched_at"], i["fetched_at"]))
        conn.execute("INSERT INTO runs(created_at, statuses) VALUES (?, ?)",
                     (now(), json.dumps(statuses, ensure_ascii=False)))

def render(items, statuses):
    escape = html.escape
    out = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
           '<title>AgentRadar 基础信息报告</title><body>',
           '<h1>AgentRadar 基础信息报告</h1>',
           '<p>尚未进行 AI 中文分析。仅展示本次取得的有限条目，不代表完整历史或今日全部发布。</p>',
           '<p>生成时间（UTC）：' + escape(now()) + '</p>', '<h2>来源状态</h2><ul>']
    for s in statuses:
        out.append('<li>' + escape(s["source"] + ': ' + s["status"]) + '</li>')
    out.append('</ul><h2>本次采集</h2><ul>')
    if not items:
        out.append('<li>本次没有取得条目；请检查来源状态。</li>')
    seen = set()
    for i in items:
        key = (i.get("source"), i.get("external_id", i["url"]))
        if key in seen:
            continue
        seen.add(key)
        title = escape(i["title"])
        parsed = urlparse(i["url"])
        if parsed.scheme in ("https", "http") and parsed.netloc:
            title = '<a href="' + escape(i["url"], quote=True) + '" rel="noreferrer">' + title + '</a>'
        out.append('<li>' + title + '<p>' + escape(i["source"]) + ' · ' +
                   escape(i.get("published_at") or '发布时间未知') + '</p></li>')
    out.append('</ul></body></html>')
    return '\n'.join(out)

def run(client, root):
    items, statuses = collect(client)
    save(root / "data/radar.db", items, statuses)
    output = root / "reports/latest.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(render(items, statuses), encoding="utf-8")
    temporary.replace(output)
    failures = sum(s["status"] != "ok" for s in statuses)
    return 2 if failures == len(statuses) else 1 if failures else 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    with httpx.Client(timeout=20, follow_redirects=False,
                      headers={"User-Agent": "AgentRadar-personal/0.1"}) as client:
        raise SystemExit(run(client, args.output_dir.resolve()))

if __name__ == "__main__":
    main()
```

M1 只请求两个固定公开入口，拒绝自动跟随重定向；不具备任意 URL 抓取工具。后续研究工具引入前必须实现设计中的目标地址检查。

- [ ] 运行 `.\.venv\Scripts\python.exe -m pytest -q`。预期四项测试通过；如不通过，修复后再继续。
- [ ] 检查实现边界：此阶段更新已有条目会覆盖旧标题，不保存内容版本；页面展示本次采集列表，不具备跨日报推荐去重。M2 的数据库迁移和推荐记录必须解决这些限制，不能直接宣传已支持。

## Task 4：真实采集验收与说明

- [ ] 执行 `.\.venv\Scripts\python.exe radar.py`。解释退出状态：0 为两个来源均成功；1 为部分失败；2 为全部失败。失败不是“没有新闻”。
- [ ] 打开 `reports/latest.html`，查看中文是否正常、来源状态是否明确、链接是否指向对应原文。真实联网结果与离线测试结果分开记录。
- [ ] 再运行一次；数据行数可以因远端新数据增加，但同一个来源 ID 不得重复入库。人工验证用 SQL：

```sql
SELECT source, external_id, COUNT(*)
FROM items
GROUP BY source, external_id
HAVING COUNT(*) > 1;
```

预期返回零行。

- [ ] 创建 `README.md`，包含下列内容，并补充实际 Python 版本、实际测试日期及联网结果：

```markdown
# AgentRadar

M1 基础信息采集器：GitHub Releases + arXiv RSS + SQLite + 静态 HTML。

在项目目录使用虚拟环境安装 requirements-dev.in，然后运行：

    .\.venv\Scripts\python.exe radar.py

打开 reports/latest.html 查看本次结果。运行 pytest 验证离线行为：

    .\.venv\Scripts\python.exe -m pytest -q

退出码：0 全部成功；1 部分失败；2 全部失败。

数据存放 data/radar.db，重复采集按来源 ID 更新，不重复插入。

当前没有 AI 分析、陌生项目发现、自动调度、完整历史回补或跨日报推荐去重。
页面保留原文标题，不代表已经实现完整中文日报或研究 Agent。
运行要求联网；不要把密钥、个人数据或生成的数据库提交到仓库。
```

- [ ] 验证通过后用 `.\.venv\Scripts\python.exe -m pip freeze` 获取实际依赖版本，保存为 `requirements.lock.txt`。文档如需声称可复现安装，须实际验证该锁定文件的安装过程。
- [ ] 检查当前目录是否为独立 Git 仓库。如果尚未建立，在实施阶段建立独立仓库并检查忽略规则，再提交源代码、测试和文档；不提交数据库、虚拟环境或报告。不因文档计划完成而假称已经提交代码。

## 后续阶段入口

M1 验收后，为 M2 单独细化计划：先迁移版本化数据和运行状态，再接入陌生项目发现、结构化中文分析和来源引用，最后接入预算与定时。模型供应商在该阶段根据用户选择及可用接口确认，不预先写死。

M3 和 M4 以设计文档的阶段表为范围，分别制定研究 Agent 和反馈闭环计划。M1 的基础功能不得被当作已完成整个项目。

## 计划自检

- 需求覆盖：M1 覆盖采集、保存、去重、失败隔离和可浏览报告；完整第一版的其他要求已明确归入 M2～M4。
- 接口一致：测试仅调用 collect、save、render、run，均在实现任务定义。
- 状态诚实：本文件包含待执行代码，当前没有运行测试、安装依赖或联网采集的成功记录。

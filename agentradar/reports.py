"""Escaped, self-contained reports shared by static export and the local app."""
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analysis import CST

ASSETS = Path(__file__).parent / "assets"
ENV = Environment(loader=FileSystemLoader(ASSETS), autoescape=select_autoescape(["html"]))


def safe_url(value):
    if not isinstance(value, str):
        return "#"
    try:
        parsed = urlparse(value)
        return value if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username else "#"
    except ValueError:
        return "#"


def local_time(value):
    if not value:
        return "时间未知"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(CST).strftime("%m-%d %H:%M") if dt.tzinfo else value[:16]
    except ValueError:
        return "时间未知"


ENV.filters["safe_url"] = safe_url
ENV.filters["local_time"] = local_time


def render(report=None, **context):
    defaults = {"report": report, "live": False, "page": "today", "history": [], "feedback": [],
                "stats": {}, "csrf": "", "model": {}, "practice": report.get("practice") if report else None,
                "styles": (ASSETS / "style.css").read_text(encoding="utf-8"),
                "script": (ASSETS / "app.js").read_text(encoding="utf-8"),
                "feedback_labels": {"useful": "有用", "saved": "已收藏", "hard": "暂时太难", "ignore": "不感兴趣", "completed": "已完成实践"}}
    defaults.update(context)
    return ENV.get_template("index.html").render(**defaults)


def markdown(report):
    def text(value):
        return re.sub(r"([\\`*_{}\[\]()<>#+!|])", r"\\\1", " ".join(str(value).split()))
    lines = [f"# AgentRadar · {report['date']}", "", f"模式：{report['mode']}。来源状态与限制见下文。", ""]
    for card in report["cards"]:
        lines += ["## " + text(card["title_zh"]), "", text(card["summary"]), "",
                  "相关性：" + text(card["relevance"]), "", "限制：" + text(card["limitations"]), "",
                  "建议：" + text(card["action"]), "", "原始链接：" + safe_url(card["url"]), ""]
    lines += ["## 来源状态", ""]
    lines += [f"- {text(s['source'])}：{text(s['status'])}；{text(s.get('coverage_note', ''))}" for s in report["statuses"]]
    return "\n".join(lines) + "\n"


def export_report(root, report):
    directory = root / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    html, md = render(report), markdown(report)
    for name, content in [("latest.html", html), (report["date"] + ".html", html), (report["date"] + ".md", md)]:
        target = directory / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)

"""Collect, analyze, and publish independently of the browser process."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import time
import json
import sqlite3

import httpx

from .analysis import Analyzer, Budget, BudgetExceeded, ModelFailure, CST
from .collectors import collect
from .config import load_config, model_ready
from .storage import Store


class AlreadyRunning(Exception):
    pass


@contextmanager
def run_lock(root):
    path = root / "data/run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning("已有采集任务正在运行") from None
    try:
        yield
    finally:
        handle.close()


def rank_candidates(items, store, config, day):
    old, feedback = store.recommended_before(day), store.feedback_map()
    category_feedback = {}
    for item in store.feedback_items():
        weight = {"useful": 1, "saved": .3, "hard": -.3, "ignore": -1}.get(item["action"], 0)
        category = item.get("category", "engineering")
        category_feedback[category] = max(-2, min(2, category_feedback.get(category, 0) + weight))
    scored = []
    for item in items:
        if item["id"] in old or feedback.get(item["id"]) in {"ignore", "completed"}:
            continue
        text = (item["title"] + " " + item["raw_text"][:3000]).lower()
        hits = sum(word.lower() in text for word in config["topics"])
        score = min(hits, 8) + category_feedback.get(item["category"], 0)
        if item["category"] == "engineering":
            score += 2
        scored.append((score, item))
    scored.sort(key=lambda x: (x[0], x[1].get("published_at") or ""), reverse=True)
    # Separate category queues prevent a large research feed from crowding out engineering.
    queues = {name: [item for _, item in scored if item["category"] == name] for name in ("engineering", "research", "discovery")}
    result = []
    while any(queues.values()) and len(result) < config["candidate_limit"]:
        for name in ("engineering", "discovery", "engineering", "research"):
            if queues[name] and len(result) < config["candidate_limit"]:
                result.append(queues[name].pop(0))
    return result


def fallback(item, reason):
    excerpt = re.sub(r"<[^>]+>", " ", item["raw_text"])
    excerpt = " ".join(excerpt.split())[:500]
    return {"item_id": item["id"], "title": item["title"], "title_zh": item["title"],
            "url": item["url"], "category": item["category"], "published_at": item.get("published_at"),
            "first_seen_at": item.get("first_seen_at"), "summary": excerpt or "原始来源未提供摘要。",
            "relevance": "已通过主题规则进入候选，尚未由模型评估。", "limitations": reason,
            "action": "先阅读原始资料，确认适用场景与运行条件。", "score": None,
            "evidence": [{"id": "e0", "url": item["url"], "title": "原始资料"}],
            "trace": [], "ai": False}


def practice_for(store):
    week = datetime.now(CST).strftime("%G-W%V")
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS practice_weeks (week TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        existing = db.execute("SELECT payload FROM practice_weeks WHERE week=?", (week,)).fetchone()
    if existing:
        task = json.loads(existing[0])
        task["completed"] = store.feedback_map().get(task["item_id"]) == "completed"
        return task
    candidates = [i for i in store.feedback_items() if i["action"] == "useful"]
    if not candidates:
        return None
    item = candidates[0]
    task = {"item_id": item["id"], "title": "验证一个具体能力：" + item["title"],
            "url": item["url"], "prerequisites": "阅读项目说明；了解其语言、依赖与运行成本后再动手。",
            "estimate": "2～3 小时（建议时间盒，非实测）", "goal": "选择资料中的一个能力，设计最小输入、预期输出和一个失败场景。",
            "deliverables": "一份最小示例、正常与失败结果，以及约 300 字的适用范围说明。",
            "acceptance": "能重复运行；结果与原始资料对照；记录未验证的主张。任务完成由你确认。", "completed": False}
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT OR IGNORE INTO practice_weeks VALUES (?,?)", (week, json.dumps(task, ensure_ascii=False)))
    return task


def run(root: Path, *, client=None, config=None):
    with run_lock(root):
        config = config or load_config(root)
        config = {**config, "_deadline": time.monotonic() + config.get("run_timeout_seconds", 600)}
        store = Store(root / "data/radar.db")
        budget = Budget(store.path, config)
        budget.deadline = config["_deadline"]
        own_client = client is None
        client = client or httpx.Client(timeout=config.get("http_timeout_seconds", 20),
                                       follow_redirects=False, headers={"User-Agent": "AgentRadar/0.1 personal research"})
        try:
            items, statuses = collect(client, config, store.checkpoints())
            incoming = store.save_collection(items, statuses)
            checkpoints = store.checkpoints()
            for status in statuses:
                status["last_success_at"] = checkpoints.get(status["source"])
            day = datetime.now(CST).date().isoformat()
            candidates = rank_candidates(incoming, store, config, day)
            analyzer = Analyzer(client, config, store, budget) if model_ready(config) else None
            cards, errors = [], []
            for index, item in enumerate(candidates):
                card = fallback(item, "未配置模型，保留原文摘录；尚未进行 AI 中文分析。")
                if analyzer:
                    try:
                        card.update(analyzer.analyze(item, index < config.get("research_limit", 3)))
                        card["ai"] = True
                    except (BudgetExceeded, ModelFailure) as exc:
                        reason = str(exc)
                        errors.append(reason)
                        card["limitations"] = reason + "；此条显示原文摘录，未完成 AI 分析。"
                        if isinstance(exc, BudgetExceeded):
                            analyzer = None
                elif model_ready(config):
                    card["limitations"] = "模型预算已用尽，此条保留原文摘录。"
                cards.append(card)
            cards.sort(key=lambda c: c["score"] if c["score"] is not None else -1, reverse=True)
            limit = config.get("report_limit", 6)
            discovery = next((c for c in cards if c["category"] == "discovery"), None)
            ordinary = [c for c in cards if c is not discovery]
            selected = ordinary[:max(0, limit - int(discovery is not None))]
            research = next((c for c in ordinary if c["category"] == "research"), None)
            if research and research not in selected and len(selected) >= 3:
                selected[-1] = research
            if discovery and limit > 0:
                selected.append(discovery)
            for i, card in enumerate(selected):
                card["section"] = "意外发现" if card is discovery else ("重点阅读" if i < 3 else "补充关注")
            successes = sum(s["status"] in {"ok", "partial"} for s in statuses)
            ai_count = sum(c["ai"] for c in selected)
            mode = "ai" if selected and ai_count == len(selected) else "mixed" if ai_count else "basic"
            report = {"date": day, "generated_at": datetime.now(timezone.utc).isoformat(), "cards": selected,
                      "statuses": statuses, "mode": mode, "usage": budget.usage(), "errors": sorted(set(errors)),
                      "collected": len(incoming), "candidates": len(candidates), "analyzed": sum(c["ai"] for c in cards),
                      "skipped": max(0, len(incoming) - len(candidates)), "practice": practice_for(store),
                      "config_ready": model_ready(config)}
            store.save_report(report)
            from .reports import export_report
            export_report(root, report)
            code = 2 if not successes else 1 if any(s["status"] != "ok" for s in statuses) else 0
            return report, code
        finally:
            if own_client:
                client.close()

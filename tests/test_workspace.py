import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agentradar.agent import TaskJournal
from agentradar.config import ROOT
from agentradar.storage import Store
from agentradar.web import create_app


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config.json").write_text((ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def headers(client):
    html = client.get("/").text
    return {"X-Radar-CSRF": re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)}


def task_record(status="paused"):
    return {"id": "a123456789ab", "question": "<script>bad()</script> 研究问题", "status": status,
            "created_at": "2026-11-17T00:00:00Z", "evidence": [], "trace": [], "result": None,
            "error": None, "usage": {"calls": 0, "reserved_tokens": 0, "reported_tokens": 0},
            "messages": [{"role": "system", "content": "internal message"}], "pending": [], "seen_calls": []}


def test_workspace_home_daily_and_no_model(root):
    with TestClient(create_app(root)) as client:
        home = client.get("/").text
        assert 'data-page="overview"' in home and 'href="/settings"' in home
        assert 'href="/models"' not in home and 'href="/sources"' not in home
        settings = client.get("/settings").text
        for section in ("sources", "models", "automation", "system", "status"):
            assert f'href="/{section}"' in settings
        html = client.get("/research").text
        assert "给 Agent 一个值得研究的问题" in html
        assert "等待连接模型" in html
        assert client.get("/today").status_code == 200
        assert client.post("/api/research", json={"question": "test"}).status_code == 403
        response = client.post("/api/research", headers=headers(client), json={"question": "test"})
        assert response.status_code == 400 and "配置" in response.json()["detail"]


def test_task_render_payload_and_download_are_bounded(root):
    store = Store(root / "data/radar.db")
    journal = TaskJournal(store.path)
    task = task_record()
    journal.save(task)
    with TestClient(create_app(root)) as client:
        html = client.get("/research/" + task["id"]).text
        assert "<script>bad()</script>" not in html
        assert "&lt;script&gt;" in html
        data = client.get("/api/research/" + task["id"]).json()
        assert "messages" not in data and "pending" not in data and data["active"] is False
        assert client.get("/api/research/not-valid").status_code == 404
        downloaded = client.get("/research/" + task["id"] + "/download")
        assert downloaded.status_code == 200
        assert "attachment" in downloaded.headers["content-disposition"]
        assert "internal message" not in downloaded.text


def test_start_progress_pause_resume_and_busy_guard(root, monkeypatch):
    import agentradar.web as web
    (root / ".env").write_text("RADAR_BASE_URL=https://model.example/v1\nRADAR_MODEL=mock\nRADAR_API_KEY=fixture-key\n", encoding="utf-8")
    entered = threading.Event()
    def mock_research(project, question, resume_id, progress, should_stop):
        journal = TaskJournal(Store(project / "data/radar.db").path)
        record = journal.get(resume_id) if resume_id else task_record("running")
        record["status"] = "running"
        journal.save(record)
        progress({"type": "started", "task_id": record["id"]})
        entered.set()
        if not resume_id:
            deadline = time.monotonic() + 3
            while not should_stop() and time.monotonic() < deadline:
                time.sleep(.01)
            record["status"] = "paused"
        else:
            record["status"] = "completed"
        journal.save(record)
        return record
    monkeypatch.setattr(web, "research", mock_research)
    with TestClient(create_app(root)) as client:
        csrf = headers(client)
        response = client.post("/api/research", headers=csrf, json={"question": "研究工具"})
        assert response.status_code == 200 and entered.wait(2)
        assert client.post("/api/run", headers=csrf).status_code == 409
        state = client.get("/api/status").json()
        task_id = state["task_id"]
        assert client.get("/api/research/" + task_id).json()["active"] is True
        assert client.post("/api/research/" + task_id + "/pause", headers=csrf).status_code == 200
        for _ in range(100):
            if not client.get("/api/status").json()["running"]:
                break
            time.sleep(.01)
        assert client.get("/api/research/" + task_id).json()["status"] == "paused"
        entered.clear()
        assert client.post("/api/research/" + task_id + "/resume", headers=csrf).status_code == 200
        assert entered.wait(2)
        for _ in range(100):
            if not client.get("/api/status").json()["running"]:
                break
            time.sleep(.01)
        assert client.get("/api/research/" + task_id).json()["status"] == "completed"
        assert client.post("/api/research/" + task_id + "/resume", headers=csrf).status_code == 409

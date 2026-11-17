import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agentradar.analysis import Analyzer, Budget, BudgetExceeded, ModelFailure
from agentradar.config import ROOT, load_config
from agentradar.pipeline import run, run_lock, AlreadyRunning
from agentradar.reports import render
from agentradar.storage import Store
from agentradar.web import create_app


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config.json").write_text((ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def item():
    return {"source": "test", "external_id": "1", "version_key": "v1", "title": "Agent memory",
            "url": "https://github.com/example/test", "raw_text": "A tool for agents.", "category": "engineering",
            "published_at": "2026-11-16T00:00:00Z", "fetched_at": "2026-11-16T01:00:00Z"}


def answer(**updates):
    value = dict(title_zh="智能体记忆工具", summary="作者称可支持记忆。", relevance="可研究工具接口。",
                 limitations="未实际运行。", action="阅读原始文档。", evidence_ids=["e0"], engineering=3,
                 personal=3, credibility=2, novelty=2, feasibility=3, score_reason="有代码说明但未验证。",
                 research_tool="none", query="")
    return {**value, **updates}


def config_for_model(root):
    config = load_config(root)
    config["llm"] = {"base_url": "https://model.example/v1", "model": "fixture", "api_key": "secret-never-log"}
    config["max_token_budget"] = 1000000
    return config


def test_budget_survives_rerun_and_failed_requests(root):
    store = Store(root / "data/radar.db")
    config = {"max_llm_calls": 1, "max_token_budget": 100}
    Budget(store.path, config).reserve(50)
    with pytest.raises(BudgetExceeded):
        Budget(store.path, config).reserve(1)


def test_model_analysis_validates_citations_and_cache(root):
    store = Store(root / "data/radar.db")
    record = store.save_collection([item()], [])[0]
    config = config_for_model(root)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer())}}], "usage": {"total_tokens": 42}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        analyzer = Analyzer(client, config, store, Budget(store.path, config))
        assert analyzer.analyze(record)["score"] == 53
        assert analyzer.analyze(record)["cached"] is True
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/chat/completions"


def test_invalid_evidence_rejected_after_one_repair(root):
    store = Store(root / "data/radar.db")
    record = store.save_collection([item()], [])[0]
    config = config_for_model(root)
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(evidence_ids=["imaginary"]))}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        budget = Budget(store.path, config)
        with pytest.raises(ModelFailure):
            Analyzer(client, config, store, budget).analyze(record)
        assert budget.calls == 2


@pytest.mark.parametrize("payload", [[], {"usage": [], "choices": None}, {"choices": [None]}])
def test_malformed_model_envelopes_are_safe_failures(root, payload):
    store = Store(root / "data/radar.db")
    record = store.save_collection([item()], [])[0]
    config = config_for_model(root)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        analyzer = Analyzer(client, config, store, Budget(store.path, config))
        with pytest.raises(ModelFailure):
            analyzer.analyze(record)
        assert analyzer.tool("read_readme", "", record)["status"] == "error"


def test_weekly_practice_is_stable_and_completion_explicit(root):
    from agentradar.pipeline import practice_for
    store = Store(root / "data/radar.db")
    one, two = store.save_collection([item(), {**item(), "external_id": "2", "title": "Second"}], [])
    store.feedback(one["id"], "saved")
    assert practice_for(store) is None
    store.feedback(one["id"], "useful")
    assert practice_for(store)["item_id"] == one["id"]
    store.feedback(two["id"], "useful")
    assert practice_for(store)["item_id"] == one["id"]
    store.feedback(one["id"], "completed")
    assert practice_for(store)["completed"] is True


def test_research_tool_loop_has_limit_and_key_is_not_forwarded(root):
    store = Store(root / "data/radar.db")
    record = store.save_collection([item()], [])[0]
    config = config_for_model(root)
    config["tools_per_item"] = 1
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(research_tool="read_readme"))}}]})
        return httpx.Response(200, json={"content": "Zml4dHVyZQ=="})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Analyzer(client, config, store, Budget(store.path, config)).analyze(record, True)
    assert len(result["trace"]) == 1
    assert len(requests) == 3
    assert "authorization" not in requests[1].headers
    assert "预算已用尽" in result["limitations"]


def test_pipeline_failed_collection_replaces_stale_report(root, monkeypatch):
    from agentradar import pipeline
    ok = {"source": "fixture", "status": "ok", "count": 1, "checked_at": "2026-11-16T00:00:00Z", "coverage_note": "fixture"}
    monkeypatch.setattr(pipeline, "collect", lambda *args: ([item()], [ok]))
    first, code = run(root)
    assert code == 0 and len(first["cards"]) == 1
    monkeypatch.setattr(pipeline, "collect", lambda *args: ([], [{**ok, "status": "error", "count": 0}]))
    second, code = run(root)
    assert code == 2 and second["cards"] == []
    assert "Agent memory" not in (root / "reports/latest.html").read_text(encoding="utf-8")


def test_run_lock_prevents_parallel_run(root):
    with run_lock(root):
        with pytest.raises(AlreadyRunning):
            with run_lock(root):
                pass


def test_web_settings_csrf_and_secrets(root):
    with TestClient(create_app(root)) as client:
        page = client.get("/settings").text
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        body = {"base_url": "https://model.example/v1", "model": "fixture", "api_key": "test-secret-123"}
        assert client.post("/api/settings", json=body).status_code == 403
        headers = {"X-Radar-CSRF": token}
        assert client.post("/api/settings", headers={**headers, "Origin": "https://evil.example"}, json=body).status_code == 403
        assert client.post("/api/settings", headers=headers, json=body).status_code == 200
        assert "test-secret-123" not in client.get("/settings").text
        assert load_config(root)["llm"]["api_key"] == "test-secret-123"
        assert client.post("/api/settings", headers=headers, json={**body, "base_url": "https://other.example/v1", "api_key": ""}).status_code == 400
        assert client.post("/api/feedback", headers=headers, json={"item_id": "missing", "action": "useful"}).status_code == 400
        assert client.get("/api/status").json()["running"] is False


def test_html_escape_and_unsafe_link():
    report = {"date": "2026-11-16", "generated_at": "2026-11-16T00:00:00Z", "collected": 1, "analyzed": 0,
              "mode": "basic", "config_ready": False, "candidates": 1, "skipped": 0, "statuses": [], "errors": [],
              "usage": {"calls": 0, "reserved_tokens": 0, "reported_tokens": 0},
              "cards": [{"title_zh": "<script>alert(1)</script>", "summary": "<img src=x onerror=alert(1)>",
                         "url": "javascript:alert(1)", "category": "engineering", "section": "重点阅读", "evidence": [], "ai": False}]}
    result = render(report)
    assert "<script>alert(1)" not in result
    assert "&lt;script&gt;" in result
    assert "javascript:" not in result

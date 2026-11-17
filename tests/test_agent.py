"""Offline integration tests exercise the same native tool loop as real models."""
import base64
import json
from pathlib import Path

import httpx
import pytest

from agentradar.agent import research, TaskJournal
from agentradar.analysis import ModelFailure
from agentradar.config import ROOT, load_config
from agentradar.storage import Store


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "config.json").write_text((ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(tmp_path)
    config["llm"] = {"base_url": "https://model.example/v1", "model": "test-model", "api_key": "private-key-for-test"}
    config["max_token_budget"] = 2000000
    config["max_llm_calls"] = 40
    return tmp_path, config


def completion(name, arguments, identifier="call_1"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
                         "usage": {"total_tokens": 100}})


def conclusion(evidence="E002"):
    return {"outcome": "supported", "summary": "可先实践该项目的任务恢复示例。",
            "findings": [{"claim": "README 声称提供 checkpoint 恢复示例；未独立运行验证。", "evidence_ids": [evidence]}],
            "recommendations": ["在测试项目中验证一次中断与恢复。"], "limitations": ["本次只阅读项目资料，未运行代码。"]}


def test_autonomous_search_read_conclude_without_web(setup):
    root, config = setup
    model_calls, source_calls = [], []
    def handler(request):
        if request.url.host == "model.example":
            body = json.loads(request.content)
            model_calls.append(body)
            assert "tools" in body and "messages" in body
            assert request.headers["Authorization"] == "Bearer private-key-for-test"
            if len(model_calls) == 1:
                return completion("search_projects", {"query": "agent checkpoint language:Python"})
            if len(model_calls) == 2:
                assert "E001" in body["messages"][-1]["content"]
                return completion("read_project", {"repo": "example/checkpoints"}, "call_2")
            assert "E002" in body["messages"][-1]["content"]
            return completion("finish_research", conclusion(), "call_3")
        source_calls.append(request)
        assert "authorization" not in request.headers
        if request.url.path == "/search/repositories":
            return httpx.Response(200, json={"items": [{"full_name": "example/checkpoints", "description": "Agent recovery examples"}]})
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"# Checkpoint\nAn example to resume interrupted tasks.").decode()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = research(root, "找一个值得实践的 Agent 恢复方案", config=config, client=client)
    assert result["status"] == "completed"
    assert len(model_calls) == 3 and len(source_calls) == 2
    assert [e["type"] for e in result["trace"]] == ["tool_call", "tool_result", "tool_call", "tool_result", "finished"]
    assert len(result["evidence"]) == 2
    assert result["usage"]["calls"] == 3
    artifact = (root / "reports/research" / (result["id"] + ".md")).read_text(encoding="utf-8")
    assert "E002" in artifact and "未运行代码" in artifact
    assert "private-key-for-test" not in (root / "reports/research" / (result["id"] + ".json")).read_text(encoding="utf-8")
    saved = TaskJournal(root / "data/radar.db").get(result["id"])
    assert saved["status"] == "completed"
    assert saved["pending"] == []


def test_invalid_final_reference_is_corrected_by_model(setup):
    root, config = setup
    calls = 0
    def handler(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"Readme").decode()})
        calls += 1
        if calls == 1:
            return completion("read_project", {"repo": "example/project"})
        if calls == 2:
            return completion("finish_research", conclusion("fabricated"), "call_2")
        assert "引用不合法" in json.loads(request.content)["messages"][-1]["content"]
        return completion("finish_research", conclusion("E001"), "call_3")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = research(root, "研究项目", config=config, client=client)
    assert result["status"] == "completed"
    assert any(e["type"] == "validation_error" for e in result["trace"])
    assert result["result"]["findings"][0]["evidence_ids"] == ["E001"]


def test_failed_tool_allows_alternative_source(setup):
    root, config = setup
    calls = 0
    def handler(request):
        nonlocal calls
        if request.method == "GET":
            if request.url.path.endswith("/readme"):
                return httpx.Response(503)
            return httpx.Response(200, json=[{"tag_name": "v1", "body": "Recovery support", "published_at": "2026-11-16T00:00:00Z"}])
        calls += 1
        if calls == 1:
            return completion("read_project", {"repo": "example/project"})
        if calls == 2:
            assert '"status": "error"' in json.loads(request.content)["messages"][-1]["content"]
            return completion("read_releases", {"repo": "example/project"}, "call_2")
        return completion("finish_research", conclusion("E001"), "call_3")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = research(root, "查找发布变化", config=config, client=client)
    assert result["status"] == "completed"
    assert [e["status"] for e in result["trace"] if e["type"] == "tool_result"] == ["error", "ok"]


def test_budget_pause_then_resume_uses_saved_evidence(setup):
    root, config = setup
    config["max_llm_calls"] = 1
    gets = []
    def first(request):
        if request.method == "GET":
            gets.append(request)
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"Readme").decode()})
        return completion("read_project", {"repo": "example/project"})
    with httpx.Client(transport=httpx.MockTransport(first)) as client:
        state = research(root, "研究项目", config=config, client=client)
    assert state["status"] == "paused" and len(state["evidence"]) == 1
    assert len(gets) == 1
    config["max_llm_calls"] = 3
    def resumed(request):
        assert request.method == "POST"
        assert "E001" in json.loads(request.content)["messages"][-1]["content"]
        return completion("finish_research", conclusion("E001"), "call_final")
    with httpx.Client(transport=httpx.MockTransport(resumed)) as client:
        result = research(root, resume_id=state["id"], config=config, client=client)
    assert result["status"] == "completed" and result["usage"]["calls"] == 2


def test_unknown_tool_cannot_execute_shell(setup):
    root, config = setup
    calls = 0
    def handler(request):
        nonlocal calls
        assert request.method == "POST"
        calls += 1
        if calls == 1:
            return completion("run_shell", {"command": "arbitrary command"})
        return completion("finish_research", {"outcome": "insufficient", "summary": "没有取得可靠资料。", "findings": [],
                          "recommendations": [], "limitations": ["工具不可用，不能编造事实。"]}, "call_final")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = research(root, "研究项目", config=config, client=client)
    assert state["status"] == "completed"
    assert state["result"]["outcome"] == "insufficient"
    assert state["evidence"] == []


def test_missing_model_does_not_fake_an_agent_result(setup):
    root, config = setup
    config["llm"]["api_key"] = ""
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("must not call network"))) as client:
        with pytest.raises(ModelFailure, match="尚未配置模型"):
            research(root, "研究项目", config=config, client=client)


def test_interrupt_after_tool_result_keeps_model_observation(setup):
    root, config = setup
    source_calls = []
    def first(request):
        if request.method == "GET":
            source_calls.append(request)
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"Evidence already obtained").decode()})
        return completion("read_project", {"repo": "example/project"})
    def interrupt(event):
        if event["type"] == "tool_result":
            raise KeyboardInterrupt
    with httpx.Client(transport=httpx.MockTransport(first)) as client:
        paused = research(root, "研究项目", config=config, client=client, progress=interrupt)
    assert paused["status"] == "paused" and paused["pending"] == []
    assert len(paused["evidence"]) == 1
    assert paused["messages"][-1]["role"] == "tool"
    def resume(request):
        assert request.method == "POST"
        observation = json.loads(json.loads(request.content)["messages"][-1]["content"])
        assert observation["documents"][0]["evidence_id"] == "E001"
        return completion("finish_research", conclusion("E001"), "final")
    with httpx.Client(transport=httpx.MockTransport(resume)) as client:
        finished = research(root, resume_id=paused["id"], config=config, client=client)
    assert finished["status"] == "completed"
    assert len(source_calls) == 1


def test_cooperative_pause_keeps_observation_and_avoids_next_call(setup):
    root, config = setup
    stopped = False
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"Readme").decode()})
        return completion("read_project", {"repo": "example/project"})
    def progress(event):
        nonlocal stopped
        if event["type"] == "tool_result":
            stopped = True
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = research(root, "研究项目", config=config, client=client, progress=progress, should_stop=lambda: stopped)
    assert state["status"] == "paused"
    assert "按请求暂停" in state["error"]
    assert len(calls) == 2 and state["pending"] == []
    assert state["messages"][-1]["role"] == "tool"

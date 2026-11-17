import base64
import gzip
import json
import time

import httpx
import pytest

from agentradar.research_tools import ResearchTools
from agentradar.storage import Store


def make_tools(tmp_path, handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return ResearchTools(client, {"github_token": "github-secret"},
                         Store(tmp_path / "research.sqlite"), time.monotonic() + 30)


def test_native_definitions():
    definitions = ResearchTools.definitions()
    assert len(definitions) == 5
    for definition in definitions:
        function = definition["function"]
        assert definition["type"] == "function"
        assert function["strict"]
        assert function["parameters"]["additionalProperties"] is False
        assert len(function["parameters"]["required"]) == 1


@pytest.mark.parametrize("name,arguments", [
    ("run_shell", {"command": "whoami"}),
    ("search_projects", {"query": "agents", "url": "https://evil.test"}),
    ("search_projects", {"query": " "}),
    ("search_projects", {"query": "x" * 201}),
    ("search_projects", {"query": 123}),
    ("read_project", {"repo": "../private"}),
    ("read_project", {"repo": "owner/.."}),
    ("read_project", {"repo": "owner/."}),
    ("read_project", {"repo": "https://evil.test/a"}),
    ("read_project", {"repo": "a/b?token=secret"}),
    ("read_project", {"repo": "a/%2e%2e"}),
    ("read_project", {"repo": "a/中文"}),
])
def test_bad_arguments_do_not_request(tmp_path, name, arguments):
    calls = []
    tools = make_tools(tmp_path, lambda request: calls.append(request))
    result = tools.execute(name, arguments)
    assert result["status"] == "error"
    assert not calls


def test_search_then_read_and_releases(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer github-secret"
        assert "cookie" not in request.headers
        assert "x-api-key" not in request.headers
        assert "secret" not in str(request.url)
        assert request.extensions["timeout"]["read"] <= 15
        if request.url.path == "/search/repositories":
            assert request.url.params["per_page"] == "5"
            return httpx.Response(200, json={"items": [{"full_name": "org/project", "description": "Agent project", "stargazers_count": 3}]})
        if request.url.path.endswith("/readme"):
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"# Project\nUsage example").decode()})
        return httpx.Response(200, json=[{"name": "Release", "tag_name": "v1.0", "body": "Fix", "published_at": "2026-11-01"}])

    tools = make_tools(tmp_path, handler, auth=("user", "auth-secret"),
                       headers={"Authorization": "Bearer model-secret", "Cookie": "secret-cookie", "X-API-Key": "model-secret"},
                       cookies={"session": "cookie-secret"}, params={"api_key": "query-secret"})
    discovery = tools.execute("search_projects", {"query": "agent"})
    assert discovery["status"] == "ok"
    assert discovery["documents"][0]["kind"] == "discovery"
    assert "不构成质量" in discovery["documents"][0]["text"]
    readme = tools.execute("read_project", {"repo": discovery["documents"][0]["title"]})
    assert readme["documents"][0]["kind"] == "readme"
    assert "Usage example" in readme["documents"][0]["text"]
    releases = tools.execute("read_releases", {"repo": "org/project"})
    assert releases["documents"][0]["url"].endswith("/releases/tag/v1.0")
    assert len(calls) == 3


ATOM = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>arXiv Query</title><id>http://arxiv.org/api/query</id>
<entry><id>http://arxiv.org/abs/2601.00001v1</id><title>Agent Study</title>
<summary>Abstract only.</summary><updated>2026-01-01T00:00:00Z</updated></entry></feed>'''


def test_arxiv_parsing_no_credentials(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["search_query"] == "all:agents"
        assert request.url.params["max_results"] == "5"
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        return httpx.Response(200, content=ATOM)

    tools = make_tools(tmp_path, handler, auth=("user", "secret"), headers={"Authorization": "Bearer model-secret"}, cookies={"session": "secret"})
    result = tools.execute("search_papers", {"query": "all:agents"})
    assert result["status"] == "ok"
    document = result["documents"][0]
    assert document["url"] == "https://arxiv.org/abs/2601.00001v1"
    assert document["kind"] == "abstract"
    assert "未阅读全文" in document["text"]
    assert len(calls) == 1


def test_arxiv_rate_spacing(tmp_path, monkeypatch):
    now, sleeps, starts = [100.0], [], []
    monkeypatch.setattr("agentradar.research_tools.time.monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("agentradar.research_tools.time.sleep", sleep)

    def handler(request):
        starts.append(now[0])
        return httpx.Response(200, content=ATOM)

    tools = make_tools(tmp_path, handler)
    assert tools.execute("search_papers", {"query": "agents"})["status"] == "ok"
    assert sleeps == []
    assert tools.execute("search_papers", {"query": "agents"})["status"] == "ok"
    assert sleeps == [3]
    assert starts == [100, 103]
    tools.deadline = 104
    assert tools.execute("search_papers", {"query": "agents"})["status"] == "error"
    assert len(starts) == 2


@pytest.mark.parametrize("body", [
    b"not xml", b"<html>wrong source</html>",
    ATOM.replace(b"http://arxiv.org/abs/2601.00001v1", b"https://evil.test/abs/1"),
    ATOM.replace(b"http://arxiv.org/abs/2601.00001v1", b"http://arxiv.org/api/errors#bad_query"),
])
def test_bad_atom_errors(tmp_path, body):
    tools = make_tools(tmp_path, lambda request: httpx.Response(200, content=body))
    assert tools.execute("search_papers", {"query": "agents"})["status"] == "error"


def test_deadline_exhausted_no_network(tmp_path):
    calls = []
    tools = make_tools(tmp_path, lambda request: calls.append(request))
    tools.deadline = time.monotonic() - 1
    assert tools.execute("search_projects", {"query": "agent"})["status"] == "error"
    assert calls == []


def test_streaming_checks_deadline_each_incoming_chunk(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("agentradar.research_tools.time.monotonic", lambda: now[0])

    class SlowStream(httpx.SyncByteStream):
        received = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                now[0] += 1
                self.received += 1
                yield b"x" * 1024

        def close(self):
            self.closed = True

    stream = SlowStream()
    tools = make_tools(tmp_path, lambda request: httpx.Response(200, stream=stream))
    tools.deadline = now[0] + 5
    result = tools.execute("read_project", {"repo": "a/b"})
    assert result["status"] == "error"
    assert "时间预算已耗尽" in result["error"]
    assert stream.received <= 6
    assert now[0] <= 106
    assert stream.closed


@pytest.mark.parametrize("body", [b"github-secret not json", b"[]", b'{"items": {}}', b'{"items": [null]}'])
def test_bad_json_is_sanitized(tmp_path, body):
    tools = make_tools(tmp_path, lambda request: httpx.Response(200, content=body))
    result = tools.execute("search_projects", {"query": "agent"})
    assert result["status"] == "error"
    assert "github-secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_failure_and_redirect_not_followed(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/github-secret"}, text="github-secret")

    tools = make_tools(tmp_path, handler, follow_redirects=True)
    result = tools.execute("search_projects", {"query": "agent"})
    assert result["status"] == "error"
    assert "github-secret" not in json.dumps(result)
    assert len(calls) == 1


def test_decompressed_size_cap(tmp_path):
    body = gzip.compress(b"x" * (ResearchTools.MAX_BYTES + 1))
    tools = make_tools(tmp_path, lambda request: httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"}))
    result = tools.execute("read_project", {"repo": "a/b"})
    assert result["status"] == "error"
    assert "大小限制" in result["error"]


def test_local_history_original_url_and_limit(tmp_path):
    tools = make_tools(tmp_path, lambda request: pytest.fail("history must not request network"))
    tools.store.save_collection([{
        "source": "github", "external_id": str(i), "version_key": "1", "title": "Agent " + str(i),
        "url": f"https://github.com/org/project{i}", "raw_text": "Remembered details",
    } for i in range(7)], [])
    result = tools.execute("search_history", {"query": "agent"})
    assert result["status"] == "ok"
    assert len(result["documents"]) == 5
    assert all(document["kind"] == "local_history" for document in result["documents"])
    assert all(document["url"].startswith("https://github.com/org/project") for document in result["documents"])
    assert "不构成新的独立验证" in result["documents"][0]["text"]


def test_empty_results_are_success(tmp_path):
    tools = make_tools(tmp_path, lambda request: httpx.Response(200, json={"items": []}))
    assert tools.execute("search_projects", {"query": "no results"}) == {"status": "ok", "documents": []}

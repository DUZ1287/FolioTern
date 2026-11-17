from datetime import datetime, timedelta, timezone
import gzip
import httpx
import pytest

from agentradar.collectors import collect
from agentradar.storage import Store


def release(number=1, **changes):
    return {"id": number, "name": f"Version {number}", "tag_name": f"v{number}",
            "published_at": datetime.now(timezone.utc).isoformat(), "body": "Changed behavior", **changes}


def feed(identity="2611.00001v1", text="An abstract"):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>arXiv</title><link>https://arxiv.org</link><description>Recent</description>
    <item><title>Paper</title><guid>https://arxiv.org/abs/{identity}</guid>
    <link>https://arxiv.org/abs/{identity}</link><description>{text}</description></item></channel></rss>'''


def config(**sources):
    return {"sources": sources, "source_limit": 20, "lookback_days": 7}


def test_two_sources_and_canonical_arxiv_dedup():
    def respond(request):
        return httpx.Response(200, json=[release()]) if request.url.host == "api.github.com" else httpx.Response(200, text=feed())
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, config(github_repos=["owner/project"], arxiv_categories=["cs.AI", "cs.LG"]))
        repeated, _ = collect(client, config(arxiv_categories=["cs.AI"]))
    assert len(items) == 2
    assert [s["status"] for s in statuses] == ["ok", "ok", "ok"]
    paper = next(item for item in items if item["source"] == "arxiv")
    assert paper["external_id"] == "2611.00001"
    assert paper["published_at"] is None
    assert repeated[0]["version_key"] == paper["version_key"]
    assert "快照" in statuses[1]["coverage_note"]


def test_malformed_feed_isolated_from_release():
    def respond(request):
        return httpx.Response(200, json=[release()]) if request.url.host == "api.github.com" else httpx.Response(200, text="<rss>broken")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, config(github_repos=["owner/project"], arxiv_categories=["cs.AI"]))
    assert len(items) == 1
    assert statuses[1]["status"] == "error"
    assert statuses[1]["checkpoint"] is None


@pytest.mark.parametrize("guid", ["oai:arXiv.org:2203.01250v3", "not-an-arxiv-guid"])
def test_arxiv_official_guid_and_valid_link_fallback(guid):
    xml = feed("2203.01250v3").replace(
        "<guid>https://arxiv.org/abs/2203.01250v3</guid>",
        f'<guid isPermaLink="false">{guid}</guid>',
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))) as client:
        items, statuses = collect(client, config(arxiv_categories=["cs.AI", "cs.LG"]))
    assert len(items) == 1
    assert all(status["status"] == "ok" for status in statuses)
    assert items[0]["external_id"] == "2203.01250"
    assert items[0]["url"] == "https://arxiv.org/abs/2203.01250"


def test_release_changes_after_context_limit_create_full_evidence_versions(tmp_path):
    body = "a" * 16000 + "first tail"
    def respond(request):
        return httpx.Response(200, json=[release(body=body)])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first, _ = collect(client, config(github_repos=["owner/project"]))
        body = "a" * 16000 + "changed tail"
        second, _ = collect(client, config(github_repos=["owner/project"]))
    assert first[0]["version_key"] != second[0]["version_key"]
    assert first[0]["raw_text"].endswith("first tail")
    assert second[0]["raw_text"] == body
    store = Store(tmp_path / "radar.db")
    first_saved = store.save_collection(first, [])[0]
    second_saved = store.save_collection(second, [])[0]
    assert first_saved["id"] != second_saved["id"]
    assert store.item(first_saved["id"])["raw_text"].endswith("first tail")
    assert store.item(second_saved["id"])["raw_text"] == body
    assert store.stats()["total_versions"] == 2


def test_timeout_and_malformed_json_are_isolated():
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if "timeout" in request.url.path:
            raise httpx.ReadTimeout("secret=url-token", request=request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, text="not json secret=url-token")
        return httpx.Response(200, text=feed())
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, config(github_repos=["owner/timeout", "owner/malformed"], arxiv_categories=["cs.AI"]))
    assert len(items) == 1
    assert sum("timeout" in path for path in calls) == 3
    assert [s["status"] for s in statuses] == ["error", "error", "ok"]
    assert "secret" not in str(statuses)


def test_github_token_never_sent_to_arxiv_and_redirect_not_followed():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://private.example/"}) if request.url.host == "api.github.com" else httpx.Response(200, text=feed())
    settings = config(github_repos=["owner/project"], arxiv_categories=["cs.AI"])
    settings["github_token"] = "test-secret"
    with httpx.Client(transport=httpx.MockTransport(respond), headers={"Authorization": "accidental-global", "Cookie": "private=1"}, auth=("user", "password")) as client:
        _, statuses = collect(client, settings)
    assert len(seen) == 2
    assert seen[0].headers["Authorization"] == "Bearer test-secret"
    assert "Authorization" not in seen[1].headers
    assert all("Cookie" not in r.headers for r in seen)
    assert statuses[0]["status"] == "error"
    assert "secret" not in str(statuses)


def test_rate_limit_respects_long_retry_after_without_sleep(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "60"})
    monkeypatch.setattr("agentradar.collectors.time.sleep", lambda seconds: pytest.fail("must not sleep"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        _, statuses = collect(client, config(github_repos=["owner/project"]))
    assert len(calls) == 1
    assert statuses[0]["error"] == "来源触发限流"


def test_retry_transient_500_is_bounded(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    monkeypatch.setattr("agentradar.collectors.time.sleep", lambda seconds: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        _, statuses = collect(client, config(github_repos=["owner/project"]))
    assert len(calls) == 3
    assert statuses[0]["checkpoint"] is None


def test_partial_pagination_preserves_first_page_and_no_checkpoint():
    def respond(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[release()], headers={"Link": '<https://api.github.com/next>; rel="next"'})
        return httpx.Response(403)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, config(github_repos=["owner/project"]))
    assert len(items) == 1
    assert statuses[0]["status"] == "partial"
    assert statuses[0]["checkpoint"] is None


def test_cap_and_compressed_response():
    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[release(1), release(2), release(3)])
        return httpx.Response(200, content=gzip.compress(feed().encode()), headers={"Content-Encoding": "gzip"})
    settings = config(github_repos=["owner/project"], arxiv_categories=["cs.AI"])
    settings["source_limit"] = 2
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, settings)
    assert len(items) == 3
    assert "上限" in statuses[0]["coverage_note"]
    assert statuses[0]["status"] == "partial"
    assert statuses[0]["checkpoint"] is None
    assert statuses[1]["status"] == "ok"


def test_source_response_size_limit():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))) as client:
        items, statuses = collect(client, config(arxiv_categories=["cs.AI"]))
    assert not items
    assert "大小上限" in statuses[0]["error"]


def test_discovery_dynamic_dates_and_date_semantics():
    seen = []
    repo = {"full_name": "owner/new", "description": "Agent tooling", "topics": ["agents"],
            "created_at": "2020-01-01T00:00:00Z", "pushed_at": "2026-11-16T00:00:00Z"}
    def respond(request):
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"items": [repo], "incomplete_results": False})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items, statuses = collect(client, config(discovery_queries=["agent language:Python created:>{date}", "agent pushed:>2020-01-01"]))
    assert len(items) == 1
    expected_date = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    assert f"created:>={expected_date}" in seen[0]
    assert "pushed:" not in seen[0]
    assert f"pushed:>={expected_date}" in seen[1]
    assert all(q.count("language:Python") == 1 for q in seen)
    assert items[0]["published_at"] is None
    assert "并非版本发布时间" in items[0]["metadata"]["date_semantics"]
    assert all("不是 GitHub Trending" in s["coverage_note"] for s in statuses)


def test_expired_deadline_prevents_every_source_request(monkeypatch):
    monkeypatch.setattr("agentradar.collectors.time.monotonic", lambda: 101.0)
    settings = config(github_repos=["owner/project"], arxiv_categories=["cs.AI"], discovery_queries=["agent"])
    settings["_deadline"] = 100.0
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("request after deadline"))) as client:
        items, statuses = collect(client, settings)
    assert not items
    assert len(statuses) == 3
    assert all(status["status"] == "error" for status in statuses)
    assert all(status["error"] == "运行时间预算耗尽" for status in statuses)
    assert all(status["checkpoint"] is None for status in statuses)


def test_deadline_during_response_stops_following_sources_and_caps_timeout(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentradar.collectors.time.monotonic", lambda: clock[0])
    requests = []
    def respond(request):
        requests.append(request)
        assert all(0 < value <= 0.5 for value in request.extensions["timeout"].values())
        clock[0] = 101.0
        return httpx.Response(200, json=[release()])
    settings = config(github_repos=["owner/project"], arxiv_categories=["cs.AI"], discovery_queries=["agent"])
    settings.update({"_deadline": 100.5, "http_timeout_seconds": 20})
    with httpx.Client(transport=httpx.MockTransport(respond), timeout=10) as client:
        items, statuses = collect(client, settings)
    assert len(requests) == 1
    assert not items
    assert all(status["error"] == "运行时间预算耗尽" for status in statuses)
    assert all(status["checkpoint"] is None for status in statuses)


def test_retry_delay_cannot_exceed_remaining_deadline(monkeypatch):
    monkeypatch.setattr("agentradar.collectors.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("agentradar.collectors.time.sleep", lambda seconds: pytest.fail("retry exceeds deadline"))
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(503)
    settings = config(github_repos=["owner/project"])
    settings["_deadline"] = 100.1
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        _, statuses = collect(client, settings)
    assert len(requests) == 1
    assert statuses[0]["error"] == "运行时间预算耗尽"
    assert statuses[0]["checkpoint"] is None


def test_rss_truncation_is_partial_without_checkpoint():
    xml = feed().replace("</channel>", "<item><title>Extra paper</title><guid>https://arxiv.org/abs/2611.00002</guid></item></channel>")
    settings = config(arxiv_categories=["cs.AI"])
    settings["source_limit"] = 1
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))) as client:
        items, statuses = collect(client, settings)
    assert len(items) == 1
    assert statuses[0]["status"] == "partial"
    assert statuses[0]["checkpoint"] is None
    assert "上限" in statuses[0]["coverage_note"]


def sample(version="v1", fetched="2026-11-16T00:00:00+00:00"):
    return {"source": "arxiv", "external_id": "2611.00001", "version_key": version,
            "title": "Paper", "url": "https://arxiv.org/abs/2611.00001", "published_at": None,
            "fetched_at": fetched, "raw_text": "Abstract", "category": "research"}


def test_versioned_storage_dedup_and_first_seen(tmp_path):
    store = Store(tmp_path / "radar.db")
    first = store.save_collection([sample()], [])[0]
    same = store.save_collection([sample()], [])[0]
    changed = store.save_collection([sample("v2", "2026-11-17T00:00:00+00:00")], [])[0]
    assert same["id"] == first["id"]
    assert same["first_seen_at"] == first["first_seen_at"]
    assert changed["id"] != first["id"]
    assert store.items()[0]["id"] == changed["id"]
    assert store.item(first["id"])["version_key"] == "v1"
    assert store.stats() == {"total_items": 1, "total_versions": 2, "reports": 0}


def test_transaction_rollback_and_failed_status_cannot_advance_checkpoint(tmp_path):
    store = Store(tmp_path / "radar.db")
    store.save_collection([], [{"source": "arxiv:cs.AI", "status": "ok", "checkpoint": "old"}])
    for status in ("partial", "error"):
        store.save_collection([], [{"source": "arxiv:cs.AI", "status": status, "checkpoint": "should-not-save"}])
    assert store.checkpoints() == {"arxiv:cs.AI": "old"}
    assert len(store.collection_runs()) == 3
    assert store.collection_runs()[0]["statuses"][0]["status"] == "error"
    with pytest.raises(KeyError):
        store.save_collection([sample()], [{"status": "ok", "checkpoint": "new"}])
    assert store.stats()["total_items"] == 0
    assert store.checkpoints() == {"arxiv:cs.AI": "old"}
    assert len(store.collection_runs()) == 3


def test_feedback_validation_and_latest_action(tmp_path):
    store = Store(tmp_path / "radar.db")
    item = store.save_collection([sample()], [])[0]
    with pytest.raises(ValueError):
        store.feedback("missing", "saved")
    with pytest.raises(ValueError):
        store.feedback(item["id"], "mastered")
    store.feedback(item["id"], "saved")
    assert store.feedback_map() == {item["id"]: "saved"}
    store.feedback(item["id"], "completed")
    entries = store.feedback_items()
    assert len(entries) == 1
    assert entries[0]["action"] == "completed"
    assert entries[0]["item_id"] == item["id"]
    assert entries[0]["title"] == "Paper"


def test_same_day_report_replacement_and_earlier_day_dedup(tmp_path):
    store = Store(tmp_path / "radar.db")
    store.save_report({"date": "2026-11-15", "cards": [{"item_id": "earlier"}], "mode": "basic"})
    store.save_report({"date": "2026-11-16", "cards": [{"item_id": "first"}]})
    store.save_report({"date": "2026-11-16", "cards": [{"item_id": "replacement"}], "arbitrary": {"nested": True}})
    assert store.recommended_before("2026-11-16") == {"earlier"}
    assert store.recommended_before("2026-11-17") == {"earlier", "replacement"}
    assert store.report()["arbitrary"]["nested"] is True
    assert len(store.reports()) == 2
    assert store.report("2026-11-15")["mode"] == "basic"


def test_analysis_cache_exact_key(tmp_path):
    store = Store(tmp_path / "radar.db")
    item_id = store.save_collection([sample()], [])[0]["id"]
    store.save_analysis(item_id, "hash", "model", "p1", {"summary": "Supported"})
    assert store.analysis(item_id, "hash", "model", "p1") == {"summary": "Supported"}
    assert store.analysis(item_id, "changed", "model", "p1") is None
    assert store.analysis(item_id, "hash", "other", "p1") is None
    assert store.analysis(item_id, "hash", "model", "p2") is None
    with pytest.raises(ValueError):
        store.save_analysis("missing", "hash", "model", "p1", {})

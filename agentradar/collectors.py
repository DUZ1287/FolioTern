"""Bounded collectors for public GitHub and arXiv sources.

Upstream text is untrusted data. This module never executes it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import feedparser
import httpx


class CollectionError(Exception):
    """An intentionally sanitized error safe for reports."""


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CollectionError("运行时间预算耗尽")
    return remaining


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date(value) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _request(client: httpx.Client, url: str, token: str = "", params=None, deadline=None, timeout=None):
    host = urlsplit(url).hostname
    if host not in {"api.github.com", "rss.arxiv.org", "export.arxiv.org"}:
        raise CollectionError("不支持的来源主机")
    for attempt in range(3):
        _remaining(deadline)
        request = client.build_request("GET", url, params=params)
        # Never forward caller-provided credentials to a different public service.
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        request.headers["User-Agent"] = "AgentRadar/0.1 (local research reader)"
        if host == "api.github.com":
            request.headers["Accept"] = "application/vnd.github+json"
            if token:
                request.headers["Authorization"] = f"Bearer {token}"
        remaining = _remaining(deadline)
        caps = [value for value in (remaining, timeout) if value is not None]
        if caps:
            ceiling = min(caps)
            current = request.extensions.get("timeout", {})
            request.extensions["timeout"] = {
                phase: min(current[phase], ceiling) if current.get(phase) is not None else ceiling
                for phase in ("connect", "read", "write", "pool")
            }
        try:
            response = client.send(request, follow_redirects=False, auth=None, stream=True)
            streamed = response
            try:
                chunks, size = [], 0
                chunks_iter = response.iter_bytes()
                while True:
                    _remaining(deadline)
                    try:
                        chunk = next(chunks_iter)
                    except StopIteration:
                        break
                    _remaining(deadline)
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise CollectionError("来源响应超过 4 MiB 大小上限")
                    chunks.append(chunk)
                headers = dict(response.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                response = httpx.Response(response.status_code, headers=headers,
                                          content=b"".join(chunks), request=request)
            finally:
                streamed.close()
        except httpx.TimeoutException:
            _remaining(deadline)
            if attempt == 2:
                raise CollectionError("请求超时") from None
            continue
        except httpx.HTTPError:
            _remaining(deadline)
            raise CollectionError("网络请求失败") from None
        _remaining(deadline)
        limited = response.status_code == 429 or (
            response.status_code == 403 and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
                or "rate limit" in response.text[:2000].lower()
            )
        )
        if limited or response.status_code >= 500:
            wait = 0.25 * (attempt + 1)
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    wait = max(0, float(retry_after))
                except ValueError:
                    parsed = _date(retry_after)
                    wait = max(0, (datetime.fromisoformat(parsed) - datetime.now(timezone.utc)).total_seconds()) if parsed else 3
            elif limited and response.headers.get("x-ratelimit-reset"):
                try:
                    wait = max(0, float(response.headers["x-ratelimit-reset"]) - time.time())
                except ValueError:
                    wait = 3
            if attempt == 2 or wait > 2:
                raise CollectionError("来源触发限流" if limited else "来源暂时不可用")
            remaining = _remaining(deadline)
            if remaining is not None and wait >= remaining:
                raise CollectionError("运行时间预算耗尽")
            time.sleep(wait)
            continue
        if response.status_code != 200:
            raise CollectionError(f"来源返回 HTTP {response.status_code}")
        return response
    raise CollectionError("来源不可用")


def _json(response):
    try:
        return response.json()
    except (ValueError, TypeError):
        raise CollectionError("来源返回无效 JSON") from None


def _item(source, external_id, title, url, published_at, raw_text, category, updated=None, metadata=None):
    # Hash and retain the complete received evidence. HTTP responses already have
    # a 4 MiB limit; presentation/model callers apply their own context limits.
    title, raw_text = str(title), str(raw_text)
    meaningful = json.dumps([title, raw_text, updated], ensure_ascii=False, separators=(",", ":"))
    result = {
        "source": source, "external_id": str(external_id),
        "version_key": hashlib.sha256(meaningful.encode()).hexdigest(),
        "title": title[:1000], "url": url, "published_at": _date(published_at),
        "fetched_at": _now(), "raw_text": raw_text, "category": category,
    }
    if metadata:
        result["metadata"] = metadata
    return result


def _github_pages(client, path, token, limit, params=None, search=False, deadline=None, timeout=None):
    """Fetch at most two pages; preserve a completed page if a later one fails."""
    rows, complete, note, error = [], False, "", None
    per_page = min(100, limit + 1)
    for page in (1, 2):
        try:
            response = _request(client, "https://api.github.com/" + path, token,
                                {**(params or {}), "per_page": per_page, "page": page}, deadline, timeout)
            payload = _json(response)
            values = payload.get("items") if search and isinstance(payload, dict) else payload
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise CollectionError("来源 JSON 结构不符合预期")
            rows.extend(values)
            has_next = 'rel="next"' in response.headers.get("link", "")
            if search and payload.get("incomplete_results"):
                error = "GitHub 返回的搜索结果不完整"
                break
            if len(rows) > limit:
                note = "已达单来源条目上限，仍有条目未采集。"
                break
            if not has_next and len(values) < per_page:
                complete = True
                break
            if not has_next and not search:
                complete = True
                break
            if page == 2:
                note = "已达两页分页上限，可能仍有条目未采集。"
        except CollectionError as exc:
            error = str(exc)
            break
    return rows[:limit], complete, note, error


def collect(client: httpx.Client, config: dict, checkpoints: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Collect a bounded lookback snapshot. Checkpoints describe completed snapshots.

    Checkpoints intentionally do not skip items: releases/papers can be revised.
    """
    sources = config.get("sources", {})
    limit = max(1, min(200, int(config.get("source_limit", 20))))
    days = max(1, min(365, int(config.get("lookback_days", 7))))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    token = config.get("github_token", "") or ""
    deadline = config.get("_deadline")
    timeout = config.get("http_timeout_seconds")
    items, statuses = [], []

    def run(source, callback):
        checked = _now()
        try:
            _remaining(deadline)
            found, complete, note, error = callback()
            try:
                _remaining(deadline)
            except CollectionError as exc:
                complete, error = False, str(exc)
        except CollectionError as exc:
            found, complete, note, error = [], False, "", str(exc)
        except Exception:
            # Do not expose server body, URLs, credentials, or parser internals.
            found, complete, note, error = [], False, "", "无法解析来源数据"
        items.extend(found)
        statuses.append({"source": source, "status": "ok" if complete and not error else ("partial" if found or not error else "error"),
                         "count": len(found), "error": error, "checked_at": checked,
                         "coverage_note": note, "checkpoint": checked if complete and not error else None})

    for repo in sources.get("github_repos", []):
        def releases(repo=repo):
            if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise CollectionError("GitHub 仓库名称无效")
            rows, complete, note, error = _github_pages(client, f"repos/{repo}/releases", token, limit,
                                                       deadline=deadline, timeout=timeout)
            found = []
            for row in rows:
                if row.get("draft"):
                    continue
                published = _date(row.get("published_at"))
                if published and datetime.fromisoformat(published) < cutoff:
                    continue
                if row.get("id") is None or not row.get("tag_name"):
                    raise CollectionError("发布记录缺少标识字段")
                found.append(_item(f"github:{repo.lower()}", f"{repo.lower()}/releases/{row['id']}",
                                   row.get("name") or row["tag_name"],
                                   f"https://github.com/{repo}/releases/tag/{row['tag_name']}",
                                   published, row.get("body") or "", "engineering",
                                   row.get("updated_at"), {"tag": row["tag_name"], "prerelease": bool(row.get("prerelease"))}))
            return found, complete, note or "发布接口快照；采集范围受回看天数和条目上限约束。", error
        run(f"github:{str(repo).lower()}", releases)

    for category in sources.get("arxiv_categories", []):
        def papers(category=category):
            if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*", category):
                raise CollectionError("arXiv 分类名称无效")
            response = _request(client, f"https://rss.arxiv.org/rss/{category}", deadline=deadline, timeout=timeout)
            feed = feedparser.parse(response.content)
            if feed.bozo or not feed.get("version"):
                raise CollectionError("来源返回无效 RSS 内容")
            found = []
            for entry in feed.entries[:limit]:
                match = None
                for identity in (entry.get("id"), entry.get("link")):
                    match = re.fullmatch(
                        r"(?:https?://(?:www\.)?arxiv\.org/abs/|oai:arxiv\.org:|arxiv:)"
                        r"([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?(?:[?#].*)?",
                        str(identity or "").strip(), re.IGNORECASE,
                    )
                    if match:
                        break
                if not match:
                    raise CollectionError("论文缺少有效 arXiv 标识")
                paper_id = match.group(1)
                published = _date(entry.get("published") or entry.get("updated"))
                if published and datetime.fromisoformat(published) < cutoff:
                    continue
                found.append(_item("arxiv", paper_id, entry.get("title", "Untitled paper"),
                                   f"https://arxiv.org/abs/{paper_id}", published,
                                   entry.get("summary") or entry.get("description") or "", "research",
                                   match.group(2), {"arxiv_id": paper_id}))
            note = "arXiv RSS 仅提供近期公告快照，无法保证覆盖全部回看时段。"
            if len(feed.entries) > limit:
                note += " 已达单来源条目上限。"
            return found, len(feed.entries) <= limit, note, None
        run(f"arxiv:{category}", papers)

    for query in sources.get("discovery_queries", []):
        def discover(query=query):
            if not isinstance(query, str) or not query.strip() or len(query) > 300:
                raise CollectionError("项目发现查询无效")
            date = cutoff.date().isoformat()
            date_kind = "created" if re.search(r"\bcreated:", query) else "pushed"
            # Keep the requested discovery mode, but refresh stale date constraints.
            base = re.sub(r"\b(?:created|pushed|language):\S+", "", query).strip()
            search = f"{base} language:Python {date_kind}:>={date}"
            rows, complete, note, error = _github_pages(client, "search/repositories", token, limit,
                {"q": search, "sort": "updated", "order": "desc"}, search=True, deadline=deadline, timeout=timeout)
            found = []
            for row in rows:
                full_name = row.get("full_name", "")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
                    raise CollectionError("仓库记录缺少标识字段")
                description = row.get("description") or ""
                topics = ", ".join(str(topic) for topic in row.get("topics", []))
                found.append(_item("github_discovery", full_name.lower(), full_name,
                                   f"https://github.com/{full_name}", None,
                                   f"{description}\nTopics: {topics}", "discovery", row.get("pushed_at"),
                                   {"created_at": _date(row.get("created_at")), "pushed_at": _date(row.get("pushed_at")),
                                    "date_semantics": "创建和推送时间属于仓库活动时间，并非版本发布时间。",
                                    "stars": row.get("stargazers_count", 0)}))
            mode = "创建" if date_kind == "created" else "推送"
            return found, complete, (note + f" 按近期{mode}时间搜索 GitHub 仓库，不是 GitHub Trending 榜单；仓库日期并非版本发布日期。").strip(), error
        run(f"discovery:{query}", discover)

    unique = {(item["source"], item["external_id"], item["version_key"]): item for item in items}
    return list(unique.values()), statuses

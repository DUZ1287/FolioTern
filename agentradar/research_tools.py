"""Bounded, read-only sources exposed to the research agent as native tools."""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import feedparser
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .storage import Store


class QueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=200, description="搜索关键词或来源支持的查询表达式")

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty query")
        return value.strip()


class RepositoryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200,
                      description="GitHub 仓库，格式为 owner/name")

    @field_validator("repo")
    @classmethod
    def safe_components(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("unsafe repository")
        return value


class SourceError(Exception):
    """Contains only a fixed, user-safe description, never a remote response."""


class ResearchTools:
    MAX_BYTES = 1024 * 1024
    _specs = {
        "search_projects": (QueryArguments, "搜索公开 GitHub 项目以发现候选。描述、星数等元数据不是项目质量或能力的验证。"),
        "read_project": (RepositoryArguments, "读取指定 GitHub 项目的 README，核对项目自述、安装和使用方式；自述不是独立验证。"),
        "read_releases": (RepositoryArguments, "读取指定 GitHub 项目最近至多三个发布记录。"),
        "search_papers": (QueryArguments, "搜索 arXiv 最近论文，只读取题名和摘要，不读取全文，不可声称阅读全文。"),
        "search_history": (QueryArguments, "检索本地已收集条目的标题和正文；历史资料不是新的独立验证。"),
    }

    def __init__(self, client: httpx.Client, config: dict, store: Store, deadline: float):
        self.client = client
        self.config = config
        self.store = store
        self.deadline = deadline
        self._last_arxiv: float | None = None

    @classmethod
    def definitions(cls) -> list[dict]:
        return [{"type": "function", "function": {
            "name": name, "description": description, "strict": True,
            "parameters": model.model_json_schema(),
        }} for name, (model, description) in cls._specs.items()]

    def execute(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or name not in self._specs:
            return self._error("未知研究工具")
        try:
            if not isinstance(arguments, dict):
                return self._error("工具参数必须是对象")
            parsed = self._specs[name][0].model_validate(arguments)
            self._remaining()
            documents = getattr(self, "_" + name)(**parsed.model_dump())
            self._remaining()
            return {"status": "ok", "documents": documents}
        except ValidationError:
            return self._error("工具参数无效或包含不支持的字段")
        except SourceError as exc:
            return self._error(str(exc))
        except httpx.TimeoutException:
            return self._error("来源请求超时")
        except httpx.HTTPError:
            return self._error("来源网络请求失败")
        except Exception:
            # Exception messages, URLs and response bodies can contain secrets.
            return self._error("来源数据无效或读取失败")

    @staticmethod
    def _error(message: str) -> dict:
        return {"status": "error", "documents": [], "error": message}

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("研究时间预算已耗尽")
        return remaining

    def _request(self, url: str, params: dict | None = None) -> bytes:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"api.github.com", "export.arxiv.org"}
                or parts.username or parts.password or parts.port is not None):
            raise SourceError("来源地址不在允许范围")
        if parts.hostname == "export.arxiv.org":
            now = time.monotonic()
            wait = 0 if self._last_arxiv is None else max(0, 3 - (now - self._last_arxiv))
            if wait >= self._remaining():
                raise SourceError("研究时间不足以满足来源请求间隔")
            if wait:
                time.sleep(wait)
            self._last_arxiv = time.monotonic()
        timeout = min(15.0, self._remaining())
        headers = {"User-Agent": "FolioTern/1.0", "Accept": "application/atom+xml"}
        if parts.hostname == "api.github.com":
            headers["Accept"] = "application/vnd.github+json"
            token = self.config.get("github_token")
            if token:
                headers["Authorization"] = "Bearer " + token
        # A standalone request excludes inherited auth, cookies, query parameters,
        # and custom API-key headers from a client also used for model inference.
        request = httpx.Request("GET", url, params=params, headers=headers,
                                extensions={"timeout": httpx.Timeout(timeout).as_dict()})
        response = self.client.send(request, auth=None, follow_redirects=False, stream=True)
        try:
            if response.status_code in {403, 429}:
                raise SourceError("来源拒绝访问或请求频率受限")
            if not 200 <= response.status_code < 300:
                raise SourceError("来源返回非成功状态")
            chunks, size = [], 0
            # Preserve transport chunk boundaries so slow responses cannot delay
            # deadline checks while httpx accumulates a larger output chunk.
            for chunk in response.iter_bytes():
                self._remaining()
                size += len(chunk)
                if size > self.MAX_BYTES:
                    raise SourceError("来源响应超过大小限制")
                chunks.append(chunk)
            self._remaining()
            return b"".join(chunks)
        finally:
            response.close()

    def _json(self, url: str, params: dict | None = None):
        return json.loads(self._request(url, params))

    @staticmethod
    def _string(value, *, optional=False) -> str:
        if optional and value is None:
            return ""
        if not isinstance(value, str):
            raise SourceError("来源数据格式无效")
        return value

    @staticmethod
    def _document(title: str, url: str, text: str, kind: str) -> dict:
        return {"title": title[:1000], "url": url, "text": text[:40000], "kind": kind}

    def _search_projects(self, query: str) -> list[dict]:
        data = self._json("https://api.github.com/search/repositories",
                          {"q": query, "per_page": 5, "sort": "updated", "order": "desc"})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError("GitHub 搜索响应格式无效")
        documents = []
        for row in data["items"][:5]:
            if not isinstance(row, dict):
                raise SourceError("GitHub 搜索条目格式无效")
            repo = RepositoryArguments(repo=row.get("full_name")).repo
            description = self._string(row.get("description"), optional=True)
            metadata = {key: row[key] for key in ("stargazers_count", "language", "updated_at", "pushed_at", "archived") if key in row}
            documents.append(self._document(repo, "https://github.com/" + repo,
                "仅用于发现候选；仓库描述和元数据不构成质量或能力证明。\n" + description
                + "\n元数据：" + json.dumps(metadata, ensure_ascii=False), "discovery"))
        return documents

    def _read_project(self, repo: str) -> list[dict]:
        data = self._json(f"https://api.github.com/repos/{repo}/readme")
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise SourceError("README 响应格式无效")
        content = base64.b64decode(re.sub(r"\s", "", data["content"]), validate=True).decode("utf-8")
        return [self._document(repo + " README", "https://github.com/" + repo + "#readme",
                              "项目 README 自述，未经运行验证。\n" + content, "readme")]

    def _read_releases(self, repo: str) -> list[dict]:
        data = self._json(f"https://api.github.com/repos/{repo}/releases", {"per_page": 3})
        if not isinstance(data, list):
            raise SourceError("发布记录响应格式无效")
        documents = []
        for row in data[:3]:
            if not isinstance(row, dict):
                raise SourceError("发布记录条目格式无效")
            tag = self._string(row.get("tag_name"))
            title = self._string(row.get("name"), optional=True) or tag
            body = self._string(row.get("body"), optional=True)
            published = self._string(row.get("published_at"), optional=True)
            documents.append(self._document(repo + " · " + title,
                f"https://github.com/{repo}/releases/tag/{quote(tag, safe='')}",
                f"发布标签：{tag}\n发布时间：{published}\n发布说明（项目方自述）：\n{body}", "release"))
        return documents

    def _search_papers(self, query: str) -> list[dict]:
        raw = self._request("https://export.arxiv.org/api/query", {
            "search_query": query, "max_results": 5, "sortBy": "submittedDate", "sortOrder": "descending"})
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise SourceError("论文来源 XML 格式无效")
        root = ElementTree.fromstring(raw)
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise SourceError("论文来源不是有效 Atom 数据")
        if any(node.tag.rsplit("}", 1)[-1].lower() == "error" for node in root.iter()):
            raise SourceError("论文来源返回查询错误")
        feed = feedparser.parse(raw)
        if feed.get("bozo"):
            raise SourceError("论文来源 Atom 数据解析失败")
        documents = []
        for entry in feed.entries[:5]:
            title = self._string(entry.get("title"))
            summary = self._string(entry.get("summary"))
            parts = urlsplit(self._string(entry.get("id")))
            if (parts.scheme not in {"http", "https"} or parts.hostname != "arxiv.org"
                    or parts.username or parts.password or parts.port is not None
                    or not parts.path.startswith("/abs/") or not parts.path[5:]
                    or parts.query or parts.fragment or title.strip().lower() == "error"):
                raise SourceError("论文来源返回错误条目或无效链接")
            url = urlunsplit(("https", "arxiv.org", parts.path, "", ""))
            documents.append(self._document(title, url, "仅阅读论文摘要，未阅读全文。\n" + summary, "abstract"))
        return documents

    def _search_history(self, query: str) -> list[dict]:
        documents = []
        needle = query.casefold()
        for row in self.store.items():
            self._remaining()
            title = self._string(row.get("title"), optional=True)
            raw_text = self._string(row.get("raw_text", row.get("rawtext")), optional=True)
            if needle not in (title + "\n" + raw_text).casefold():
                continue
            url = self._string(row.get("url"))
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                continue
            documents.append(self._document(title, url,
                "本地历史记录，不构成新的独立验证。\n" + raw_text, "local_history"))
            if len(documents) == 5:
                break
        return documents

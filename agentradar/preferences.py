"""Small transactional configuration store; secrets live in Windows Credential Manager."""
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import validate_endpoint


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["github_releases", "github_search", "arxiv_category", "arxiv_search", "rss"]
    target: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list, max_length=30)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("keywords")
    @classmethod
    def keywords_valid(cls, values):
        if any(not x.strip() or len(x) > 80 for x in values):
            raise ValueError("关键词须为 1～80 字")
        return list(dict.fromkeys(x.strip().lower() for x in values))

    @model_validator(mode="after")
    def target_valid(self):
        if self.kind == "github_releases":
            if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", self.target) or self.target.split('/')[1] in {'.', '..'}:
                raise ValueError("请填写 owner/repository")
            self.target = self.target.lower()
        elif self.kind == "arxiv_category":
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{1,40}", self.target):
                raise ValueError("arXiv 分类无效")
        elif self.kind == "rss":
            from .safe_feed import validate_url
            self.target = validate_url(self.target)
        elif len(self.target) > 300:
            raise ValueError("搜索查询不能超过 300 字")
        return self


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(default="OpenAI 兼容", max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    json_mode: bool = False
    enabled: bool = True
    purpose: Literal["all", "research", "daily"] = "all"
    max_llm_calls: int = Field(default=24, ge=1, le=500)
    max_token_budget: int = Field(default=120000, ge=1000, le=10000000)
    max_output_tokens: int = Field(default=1600, ge=128, le=32000)
    api_key: str = Field(default="", max_length=2000, repr=False, exclude=True)

    @field_validator("base_url")
    @classmethod
    def address(cls, value):
        validate_endpoint(value)
        return value.rstrip("/")


class Credentials:
    def __init__(self, root):
        self.service = "AgentRadar/" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]

    def backend(self):
        # Explicit backend: never silently fall back to a plaintext keyring.
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()

    def get(self, ref):
        return self.backend().get_password(self.service, ref) or ""

    def put(self, ref, key):
        self.backend().set_password(self.service, ref, key)

    def delete(self, ref):
        if self.get(ref):
            self.backend().delete_password(self.service, ref)


class Preferences:
    def __init__(self, root, credentials=None):
        self.root = Path(root)
        self.path = self.root / "data/settings.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials = credentials or Credentials(root)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS entries (
                kind TEXT NOT NULL, id TEXT NOT NULL, identity TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(kind,id), UNIQUE(kind,identity));
                CREATE TABLE IF NOT EXISTS options (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def option(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM options WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_option(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO options VALUES (?,?)", (key, json.dumps(value)))

    def list(self, kind):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM entries WHERE kind=? ORDER BY rowid", (kind,))]

    def get(self, kind, ident):
        return next((x for x in self.list(kind) if x['id'] == ident), None)

    def save(self, kind, value, ident=None):
        ident = ident or uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise ValueError("档案 ID 无效")
        value = {**value, "id": ident}
        identity = (value["kind"] + ":" + value["target"]) if kind == "source" else value["name"].casefold()
        try:
            with self.connect() as db:
                db.execute("INSERT INTO entries VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET identity=excluded.identity,payload=excluded.payload",
                           (kind, ident, identity, json.dumps(value, ensure_ascii=False)))
        except sqlite3.IntegrityError:
            raise ValueError("相同来源或模型档案名称已经存在") from None
        return value

    def delete(self, kind, ident):
        if kind == "profile":
            old = self.get(kind, ident)
            if old:
                self.credentials.delete(old['credential_ref'])
            for use in ("research", "daily"):
                if self.option("active_" + use) == ident:
                    self.set_option("active_" + use, "")
        with self.connect() as db:
            db.execute("DELETE FROM entries WHERE kind=? AND id=?", (kind, ident))

    def seed_sources(self, config):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM options WHERE key='sources_initialized'").fetchone():
                return
            for key, kind in (("github_repos", "github_releases"), ("arxiv_categories", "arxiv_category"), ("discovery_queries", "github_search")):
                for target in config.get("sources", {}).get(key, []):
                    value = Source(name=target, kind=kind, target=target, limit=config.get('source_limit', 20)).model_dump()
                    value['id'] = uuid.uuid4().hex[:12]
                    db.execute("INSERT OR IGNORE INTO entries VALUES(?,?,?,?)", ('source', value['id'], kind + ':' + value['target'], json.dumps(value)))
            db.execute("INSERT INTO options VALUES('sources_initialized','true')")

    def save_profile(self, body, ident=None):
        old = self.get('profile', ident) if ident else None
        ident = ident or uuid.uuid4().hex[:12]
        if old and old['base_url'] != body.base_url and not body.api_key:
            raise ValueError("更换服务地址必须重新输入密钥；本地无认证服务可填写 local")
        value = body.model_dump()
        # Immutable credential references make edits atomic from a reader's view.
        ref = old['credential_ref'] if old and not body.api_key else uuid.uuid4().hex
        if not old and not body.api_key:
            raise ValueError("请输入密钥；本地无认证服务可填写 local")
        value['credential_ref'] = ref
        value['has_key'] = True
        value['last_test'] = None
        if body.api_key:
            self.credentials.put(ref, body.api_key)
        try:
            saved = self.save('profile', value, ident)
        except Exception:
            if body.api_key:
                self.credentials.delete(ref)
            raise
        if old and ref != old['credential_ref']:
            self.credentials.delete(old['credential_ref'])
        return saved

    def apply_model(self, config, purpose):
        ident = self.option('active_' + purpose)
        if ident is None:
            return config  # Legacy CLI configuration remains supported.
        profile = self.get('profile', ident)
        config['llm'] = {'base_url': '', 'model': '', 'api_key': ''}
        if profile and profile['enabled'] and profile['purpose'] in ('all', purpose):
            config['llm'] = {k: profile[k] for k in ('base_url', 'model', 'token_parameter', 'json_mode')}
            config['llm']['api_key'] = self.credentials.get(profile['credential_ref'])
            config['llm']['profile_id'] = profile['id']
            for key in ('max_llm_calls', 'max_token_budget', 'max_output_tokens'):
                config[key] = profile[key]
        return config

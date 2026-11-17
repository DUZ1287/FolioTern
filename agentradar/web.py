"""Loopback-only dashboard with CSRF-protected local writes."""
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import set_key
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .analysis import Analyzer, Budget, BudgetExceeded, ModelFailure
from .config import load_config, model_ready, validate_endpoint
from .pipeline import run, AlreadyRunning, practice_for
from .reports import render
from .storage import Store


class FeedbackBody(BaseModel):
    item_id: str = Field(max_length=100)
    action: str = Field(max_length=30)


class SettingsBody(BaseModel):
    base_url: str = Field(max_length=500)
    model: str = Field(max_length=200)
    api_key: str = Field(default="", max_length=1000)


def create_app(root: Path):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    store = Store(root / "data/radar.db")
    token = secrets.token_urlsafe(32)
    state = {"running": False, "error": None}
    state_lock = threading.Lock()

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin and origin != expected:
                return HTMLResponse('跨站请求已拒绝', status_code=403)
            if not secrets.compare_digest(request.headers.get("x-radar-csrf", ""), token):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "页面已过期或缺少校验，请刷新后重试"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def page(name, date=None):
        config = load_config(root)
        llm = config["llm"]
        return HTMLResponse(render(store.report(date), live=True, page=name, csrf=token,
                                   history=store.reports(), feedback=store.feedback_items(), stats=store.stats(),
                                   practice=practice_for(store),
                                   model={"base_url": llm.get("base_url", ""), "model": llm.get("model", ""),
                                          "has_key": bool(llm.get("api_key"))}))

    @app.get("/", response_class=HTMLResponse)
    def today(date: str | None = None):
        return page("today", date)

    @app.get("/{name}", response_class=HTMLResponse)
    def section(name: str):
        if name not in {"history", "saved", "practice", "settings"}:
            raise HTTPException(404)
        return page(name)

    @app.get("/api/status")
    def status():
        with state_lock:
            return dict(state)

    def background_run():
        try:
            run(root)
        except AlreadyRunning:
            with state_lock:
                state["error"] = "另一采集任务正在运行，请稍后刷新。"
        except Exception as exc:
            # Diagnostic class only: exception text may contain upstream URLs or secrets.
            with state_lock:
                state["error"] = "任务未完成：" + type(exc).__name__
        finally:
            with state_lock:
                state["running"] = False

    @app.post("/api/run")
    def start_run():
        with state_lock:
            if state["running"]:
                raise HTTPException(409, "已有任务正在运行")
            state.update(running=True, error=None)
        threading.Thread(target=background_run, daemon=True, name="radar-collect").start()
        return {"started": True}

    @app.post("/api/feedback")
    def feedback(body: FeedbackBody):
        try:
            store.feedback(body.item_id, body.action)
        except ValueError:
            raise HTTPException(400, "条目或反馈类型无效") from None
        return {"saved": True}

    @app.post("/api/settings")
    def settings(body: SettingsBody):
        url, model, key = body.base_url.strip().rstrip("/"), body.model.strip(), body.api_key.strip()
        if not url or not model:
            raise HTTPException(400, "请填写接口地址和模型名称")
        try:
            validate_endpoint(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        old = load_config(root)["llm"]
        if old.get("base_url", "").rstrip("/") != url and old.get("api_key") and not key:
            raise HTTPException(400, "更换接口地址时，请重新输入对应密钥")
        if not key and not old.get("api_key"):
            raise HTTPException(400, "请填写 API Key")
        env = root / ".env"
        set_key(str(env), "RADAR_BASE_URL", url)
        set_key(str(env), "RADAR_MODEL", model)
        if key:
            set_key(str(env), "RADAR_API_KEY", key)
        return {"saved": True}

    @app.post("/api/test-model")
    def test_model():
        config = load_config(root)
        if not model_ready(config):
            raise HTTPException(400, "请先保存完整模型配置")
        budget = Budget(store.path, config)
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                analyzer = Analyzer(client, config, store, budget)
                from .analysis import Assessment
                analyzer.complete({"instruction": "这是连接测试。请填入符合 schema 的示例，证据仅引用 e0，所有分数填0，research_tool=none。",
                                   "evidence": [{"id": "e0", "text": "连接测试，不是技术新闻。"}],
                                   "schema": Assessment.model_json_schema()})
        except (BudgetExceeded, ModelFailure) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"message": "连接成功，模型已返回符合格式的中文分析结果。", "usage": budget.usage()}

    return app

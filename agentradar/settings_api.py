"""Configuration routes share the app's CSRF boundary and job exclusion."""
import json
import time
import threading

import httpx
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

from .config import load_config
from .preferences import Preferences, Source, Profile
from .managed_sources import collect_source
from .pipeline import run_lock, AlreadyRunning
from .model_probe import probe


class Appearance(BaseModel):
    theme: Literal['light', 'dark', 'opaque'] = 'light'
    reduce_motion: bool = False


class Schedule(BaseModel):
    enabled: bool = False
    background: bool = False
    time: str = Field(default='08:30', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


def install_settings(app, root, store, state, state_lock, start_daily):
    prefs = Preferences(root)
    prefs.seed_sources(json.loads((root / 'config.json').read_text(encoding='utf-8-sig')))

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        # Pydantic's default detail includes input values, potentially API keys.
        return JSONResponse(status_code=422, content={'detail': '配置字段无效，请检查类型、长度、数量和地址。'})

    def require(kind, ident):
        value = prefs.get(kind, ident)
        if not value:
            raise HTTPException(404, '档案不存在')
        return value

    def ensure_idle():
        if state['running'] or state.get('closing'):
            raise HTTPException(409, '请等待当前任务完成或暂停，再修改配置。')

    def job(kind, action):
        with state_lock:
            ensure_idle()
            state.update(running=True, kind=kind, error=None, task_id=None)
        def worker():
            try:
                with run_lock(root):
                    action()
            except AlreadyRunning:
                state['error'] = '其他进程正在运行任务，请稍后重试。'
            except Exception as exc:
                state['error'] = '操作未完成：' + type(exc).__name__
            finally:
                with state_lock:
                    state['running'] = False
        threading.Thread(target=worker, daemon=True, name='radar-settings-job').start()
        return {'started': True}

    @app.get('/api/sources')
    def sources():
        return [{**x, 'last_result': prefs.option('source_result_' + x['id'])} for x in prefs.list('source')]

    @app.post('/api/sources')
    def add_source(body: Source):
        with state_lock:
            ensure_idle()
            try:
                return prefs.save('source', body.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None

    @app.put('/api/sources/{ident}')
    def edit_source(ident: str, body: Source):
        with state_lock:
            ensure_idle()
            require('source', ident)
            try:
                value = prefs.save('source', body.model_dump(), ident)
                prefs.set_option('source_result_' + ident, None)
                return value
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None

    @app.delete('/api/sources/{ident}')
    def delete_source(ident: str):
        with state_lock:
            ensure_idle()
            require('source', ident)
            prefs.delete('source', ident)
        return {'deleted': True}

    @app.post('/api/sources/{ident}/{action}')
    def source_action(ident: str, action: str):
        source = require('source', ident)
        if action not in ('test', 'collect'):
            raise HTTPException(404)
        def perform():
            config = load_config(root)
            config['_deadline'] = time.monotonic() + 60
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                items, result = collect_source(client, config, source)
            if action == 'collect':
                store.save_collection(items, [result])
            prefs.set_option('source_result_' + ident, {**result, 'action': action,
                'preview': [{'title': x['title'], 'url': x['url']} for x in items[:5]]})
        return job('source_' + action, perform)

    @app.get('/api/profiles')
    def profiles():
        return {'profiles': [{k: v for k, v in x.items() if k != 'credential_ref'} for x in prefs.list('profile')],
                'active_research': prefs.option('active_research'), 'active_daily': prefs.option('active_daily')}

    def save_profile(body, ident=None):
        with state_lock:
            ensure_idle()
            if ident:
                require('profile', ident)
            try:
                value = prefs.save_profile(body, ident)
                return {k: v for k, v in value.items() if k != 'credential_ref'}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            except Exception:
                raise HTTPException(503, 'Windows 凭据管理器无法保存密钥；未降级为明文存储。') from None

    @app.post('/api/profiles')
    def add_profile(body: Profile):
        return save_profile(body)

    @app.put('/api/profiles/{ident}')
    def edit_profile(ident: str, body: Profile):
        return save_profile(body, ident)

    @app.delete('/api/profiles/{ident}')
    def delete_profile(ident: str):
        with state_lock:
            ensure_idle()
            require('profile', ident)
            try:
                prefs.delete('profile', ident)
            except Exception:
                raise HTTPException(503, '无法删除安全凭据，请重试。') from None
        return {'deleted': True}

    @app.post('/api/profiles/{ident}/activate')
    def activate(ident: str):
        with state_lock:
            ensure_idle()
            profile = require('profile', ident)
            if not profile['enabled']:
                raise HTTPException(400, '请先启用该档案')
            for use in ('research', 'daily') if profile['purpose'] == 'all' else (profile['purpose'],):
                prefs.set_option('active_' + use, ident)
        return {'saved': True}

    def profile_config(ident):
        value = require('profile', ident)
        config = load_config(root)
        config['llm'] = {k: value[k] for k in ('base_url', 'model', 'token_parameter', 'json_mode')}
        config['llm']['api_key'] = prefs.credentials.get(value['credential_ref'])
        for key in ('max_llm_calls', 'max_token_budget', 'max_output_tokens'):
            config[key] = value[key]
        return config

    @app.post('/api/profiles/{ident}/test')
    def test_profile(ident: str):
        require('profile', ident)
        def perform():
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                result = probe(client, profile_config(ident), store.path)
            value = require('profile', ident)
            prefs.save('profile', {**value, 'last_test': result}, ident)
        return job('model_test', perform)

    @app.post('/api/profiles/{ident}/models')
    def models(ident: str):
        config = profile_config(ident)['llm']
        try:
            deadline = time.monotonic() + 25
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                with client.stream('GET', config['base_url'] + '/models', headers={'Authorization': 'Bearer ' + config['api_key']}) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 524288 or time.monotonic() >= deadline:
                            raise ValueError()
            values = json.loads(data)['data']
            return {'models': [x['id'] for x in values[:300] if isinstance(x.get('id'), str) and len(x['id']) <= 200]}
        except Exception:
            raise HTTPException(400, '无法读取模型列表；服务可能不支持 /models，可手动填写 Model ID。') from None

    @app.get('/api/system')
    def system():
        return {'appearance': prefs.option('appearance', {'theme': 'light', 'reduce_motion': False}),
                'schedule': prefs.option('schedule', {'enabled': False, 'time': '08:30'}),
                'last_auto_date': prefs.option('last_auto_date'),
                'last_external_run': prefs.option('last_external_run'),
                'data_path': str(root), 'stats': store.stats(), 'runs': store.collection_runs(15),
                'state': dict(state)}

    @app.get('/api/library')
    def library(q: str = '', saved: bool = False):
        feedback = store.feedback_map()
        rows = store.items(500)
        needle = q[:200].casefold().strip()
        return [{k: x.get(k) for k in ('id', 'title', 'url', 'source', 'published_at', 'category')} |
                {'action': feedback.get(x['id'])} for x in rows
                if (not saved or feedback.get(x['id']) == 'saved') and
                (not needle or needle in (x['title'] + ' ' + x['raw_text']).casefold())]

    @app.post('/api/appearance')
    def appearance(body: Appearance):
        prefs.set_option('appearance', body.model_dump())
        return {'saved': True}

    @app.post('/api/schedule')
    def schedule(body: Schedule):
        from .windows_schedule import configure, ScheduleError
        with state_lock:
            previous = prefs.option('schedule', {})
            if body.background and body.enabled or previous.get('background') and previous.get('enabled'):
                try:
                    configure(root, body.enabled and body.background, body.time)
                except ScheduleError as exc:
                    raise HTTPException(409, str(exc)) from None
            prefs.set_option('schedule', body.model_dump())
        message = ('已停用每日自动任务。' if not body.enabled else
                   'Windows 已登记任务，登录期间关闭窗口也能采集。' if body.background else
                   '已保存，窗口或本地服务运行期间会自动采集。')
        return {'saved': True, 'message': message}

    return prefs

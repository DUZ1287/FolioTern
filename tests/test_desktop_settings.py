import copy
import json
import re
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agentradar.config import ROOT, load_config
from agentradar.desktop import prepare_root, migrate, SingleInstance
from agentradar.preferences import Preferences, Source, Profile, Credentials
from agentradar.storage import Store
from agentradar.web import create_app
from agentradar.collectors import CollectionError
from agentradar.managed_sources import collect_source
from agentradar.model_probe import probe
from agentradar import safe_feed


@pytest.fixture
def root(tmp_path):
    return prepare_root(tmp_path / '中文 数据')


@pytest.fixture
def vault(monkeypatch):
    data = {}
    monkeypatch.setattr(Credentials, 'get', lambda self, ref: data.get((self.service, ref), ''))
    monkeypatch.setattr(Credentials, 'put', lambda self, ref, key: data.__setitem__((self.service, ref), key))
    monkeypatch.setattr(Credentials, 'delete', lambda self, ref: data.pop((self.service, ref), None))
    return data


def headers(client):
    return {'X-Radar-CSRF': re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)}


def test_sources_crud_restart_duplicate_and_disabled(root):
    app = create_app(root)
    with TestClient(app) as c:
        h = headers(c)
        source = dict(name='项目更新', kind='github_releases', target='Example/Repo', limit=7, enabled=True, keywords=['agent'])
        assert c.post('/api/sources', json=source).status_code == 403
        response = c.post('/api/sources', json=source, headers=h)
        assert response.status_code == 200
        ident = response.json()['id']
        assert c.post('/api/sources', json=source, headers=h).status_code == 400
        app.state.job_state['running'] = True
        assert c.put('/api/sources/' + ident, json=source, headers=h).status_code == 409
        app.state.job_state['running'] = False
        source['enabled'] = False
        assert c.put('/api/sources/' + ident, json=source, headers=h).status_code == 200
    with TestClient(create_app(root)) as c:
        row = next(x for x in c.get('/api/sources').json() if x['id'] == ident)
        assert row['limit'] == 7 and row['keywords'] == ['agent'] and not row['enabled']
        assert c.delete('/api/sources/' + ident, headers=headers(c)).status_code == 200
    assert not Preferences(root).get('source', ident)


def test_profiles_secret_isolation_activation_and_restart(root, vault):
    body = dict(name='研究档案', base_url='https://model.example/v1', model='actual-id', api_key='never-echo-12345')
    with TestClient(create_app(root)) as c:
        h = headers(c)
        created = c.post('/api/profiles', json=body, headers=h)
        assert created.status_code == 200
        ident = created.json()['id']
        assert body['api_key'] not in created.text and 'credential_ref' not in created.text
        assert c.post('/api/profiles/' + ident + '/activate', headers=h).status_code == 200
        cfg = load_config(root)
        assert cfg['llm']['api_key'] == body['api_key']
        assert cfg['llm']['model'] == body['model']
        changed = {**body, 'base_url': 'https://other.example/v1', 'api_key': ''}
        assert c.put('/api/profiles/' + ident, json=changed, headers=h).status_code == 400
        invalid = c.post('/api/profiles', json={**body, 'max_llm_calls': 0}, headers=h)
        assert invalid.status_code == 422 and body['api_key'] not in invalid.text
        assert c.post('/api/profiles', json=body, headers=h).status_code == 400
    assert body['api_key'].encode() not in (root / 'data/settings.db').read_bytes()
    assert not (root / '.env').exists()
    assert load_config(root)['llm']['model'] == body['model']
    assert len(vault) == 1
    with TestClient(create_app(root)) as c:
        assert c.delete('/api/profiles/' + ident, headers=headers(c)).status_code == 200
    assert not vault and load_config(root)['llm']['model'] == ''


@pytest.mark.parametrize('url', ['http://example.com/rss', 'https://127.0.0.1/', 'https://[::1]/',
    'https://169.254.169.254/', 'https://10.0.0.1/', 'https://localhost/', 'https://foo.local/rss',
    'https://user:password@example.com/rss', 'https://example.com:8080/rss', 'https://example.com/#x'])
def test_feed_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        safe_feed.validate_url(url)


def test_feed_dns_mixed_private_rejected(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(CollectionError, match='非公网'):
        safe_feed.public_addresses('public.example')


@pytest.mark.parametrize('status,size,encoding', [(302, 0, 'identity'), (200, 4194305, 'identity'), (200, 10, 'gzip')])
def test_feed_redirect_size_encoding_bounds(monkeypatch, status, size, encoding):
    class Response:
        def __init__(self):
            self.status = status
            self.sent = 0
        def getheader(self, *args):
            return encoding
        def read1(self, amount):
            n = min(amount, size-self.sent)
            self.sent += n
            return b'x' * n
    class Connection:
        sock = None
        def __init__(self, host, address, timeout):
            assert host == 'public.example' and address == '8.8.8.8'
        def request(self, *args, **kwargs):
            assert 'Authorization' not in kwargs['headers']
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(safe_feed, 'public_addresses', lambda h: ['8.8.8.8'])
    monkeypatch.setattr(safe_feed, 'PinnedHTTPS', Connection)
    with pytest.raises(CollectionError):
        safe_feed.fetch_feed('https://public.example/feed')


def test_feed_socket_pins_ip_and_keeps_tls_hostname(monkeypatch):
    from unittest.mock import Mock
    raw = Mock()
    raw.getpeername.return_value = ('8.8.8.8', 443)
    create = Mock(return_value=raw)
    monkeypatch.setattr(socket, 'create_connection', create)
    conn = safe_feed.PinnedHTTPS('public.example', '8.8.8.8', 5)
    conn._context = Mock()
    conn.connect()
    create.assert_called_once_with(('8.8.8.8', 443), 5)
    conn._context.wrap_socket.assert_called_once_with(raw, server_hostname='public.example')


def test_rss_collection_keywords_version_dedup(root, monkeypatch):
    from agentradar import managed_sources
    feed = b'<rss version="2.0"><channel><title>test</title><item><guid>stable-1</guid><title>Agent release</title><description>memory</description><link>https://example.com/1</link></item><item><guid>2</guid><title>other</title></item></channel></rss>'
    monkeypatch.setattr(managed_sources, 'fetch_feed', lambda *args: feed)
    source = {**Source(name='RSS', kind='rss', target='https://example.com/feed', keywords=['agent']).model_dump(), 'id': 'a'*12}
    with httpx.Client() as client:
        items, status = collect_source(client, load_config(root), source)
        second, _ = collect_source(client, load_config(root), source)
    assert len(items) == 1 and status['count'] == 1
    store = Store(root/'data/radar.db')
    store.save_collection(items, [status])
    store.save_collection(second, [status])
    assert store.stats()['total_versions'] == 1


def test_model_probe_requires_native_calls_and_tool_observation(root):
    config = load_config(root)
    config['llm'] = dict(base_url='https://example.com/v1', model='fixture', api_key='secret')
    def handle(r):
        body = json.loads(r.content)
        messages = body['messages']
        if 'tools' in body:
            marker = body['tools'][0]['function']['parameters']['properties']['marker']['enum'][0]
            answer = {'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'finish_research', 'arguments': json.dumps({'marker': marker})}}]}
        elif any(x['role'] == 'tool' for x in messages):
            answer = {'content': next(x['content'] for x in messages if x['role'] == 'tool')}
        else:
            answer = {'content': '{"ok":true}'}
        return httpx.Response(200, json={'choices': [{'message': answer}]})
    store = Store(root/'data/radar.db')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe(client, config, store.path)
    assert result['ok'] and len(result['stages']) == 3 and result['usage']['calls'] == 3
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': [{'message': {'content': 'I can call tools'}}]}))) as client:
        failure = probe(client, config, store.path)
    assert not failure['ok'] and failure['stages'][0]['ok'] and not failure['stages'][1]['ok']


def test_paths_migration_preserves_database_without_plaintext_keys(root, tmp_path):
    store = Store(root/'data/radar.db')
    store.save_report({'date': '2026-11-17', 'cards': [], 'mode': 'basic'})
    (root / '.env').write_text('RADAR_API_KEY=private-value', encoding='utf-8')
    dest = tmp_path / '迁移后'
    migrate(root, dest)
    assert Store(dest/'data/radar.db').report()['date'] == '2026-11-17'
    assert not (dest / '.env').exists()
    assert store.report()['date'] == '2026-11-17'
    with pytest.raises(ValueError, match='必须为空'):
        migrate(root, dest)
    (dest / 'config.json').write_text('{"preserved": true}', encoding='utf-8')
    prepare_root(dest)
    assert json.loads((dest/'config.json').read_text())['preserved']


def test_appearance_schedule_and_all_routes_survive_restart(root):
    with TestClient(create_app(root)) as c:
        h = headers(c)
        for page in ['overview','sources','models','automation','status','system','research','today','history','saved','practice']:
            assert c.get('/'+page).status_code == 200
        assert c.post('/api/appearance', headers=h, json={'theme': 'dark', 'reduce_motion': True}).status_code == 200
        assert c.post('/api/schedule', headers=h, json={'enabled': False, 'time': '09:15'}).status_code == 200
    with TestClient(create_app(root)) as c:
        values = c.get('/api/system').json()
        assert values['appearance']['theme'] == 'dark'
        assert values['schedule']['time'] == '09:15'
        assert 'data-theme="dark"' in c.get('/sources').text


def test_single_instance_and_close_lifecycle(root):
    first, second = SingleInstance(root), SingleInstance(root)
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.close()
        second.close()
    app = create_app(root)
    app.state.job_state.update(running=True, kind='research')
    assert not app.state.prepare_close()
    assert app.state.job_state['pause_requested'] and app.state.job_state['closing']
    app.state.job_state['running'] = False
    assert app.state.prepare_close()


def test_resume_other_model_rejected_without_changing_task(root):
    from agentradar.agent import TaskJournal, ResearchAgent
    from agentradar.analysis import ModelFailure
    store = Store(root/'data/radar.db')
    journal = TaskJournal(store.path)
    journal.save({'id': 'c'*12, 'question': 'question', 'status': 'paused', 'model_signature': 'different'})
    cfg = load_config(root)
    cfg['llm'] = {'base_url': 'https://example.com/v1', 'model': 'other', 'api_key': 'test'}
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No request should be made'))) as client:
        with pytest.raises(ModelFailure, match='其他模型'):
            ResearchAgent(root, cfg, client).run(resume_id='c'*12)
    assert journal.get('c'*12)['status'] == 'paused'


def test_library_search_and_feedback(root):
    store = Store(root/'data/radar.db')
    item = store.save_collection([{'source':'rss', 'external_id':'1', 'version_key':'v1', 'title':'Agent evidence',
                                  'raw_text':'memory systems', 'url':'https://example.com/', 'category':'engineering'}], [])[0]
    with TestClient(create_app(root)) as c:
        assert c.get('/api/library?q=memory').json()[0]['id'] == item['id']
        assert c.get('/api/library?saved=true').json() == []
        assert c.post('/api/feedback', headers=headers(c), json={'item_id':item['id'], 'action':'saved'}).status_code == 200
        assert len(c.get('/api/library?saved=true').json()) == 1

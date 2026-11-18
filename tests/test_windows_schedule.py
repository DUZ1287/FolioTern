import pytest
from fastapi.testclient import TestClient

from agentradar import windows_schedule as scheduler
from agentradar.preferences import Preferences
from agentradar.web import create_app
from agentradar.desktop import prepare_root
import re


def test_schedule_rollback_and_removal(tmp_path, monkeypatch):
    root = prepare_root(tmp_path)
    prefs = Preferences(root)
    calls = []
    def register(path, enabled, clock):
        calls.append((path, enabled, clock))
        if clock == '19:00':
            raise scheduler.ScheduleError('模拟系统拒绝')
    monkeypatch.setattr(scheduler, 'configure', register)
    with TestClient(create_app(root)) as client:
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/automation').text)[1]
        headers = {'X-Radar-CSRF': csrf}
        body = {'enabled': True, 'background': True, 'time': '09:45'}
        assert client.post('/api/schedule', json=body, headers=headers).is_success
        assert client.post('/api/schedule', json={**body, 'time': '19:00'}, headers=headers).status_code == 409
        assert prefs.option('schedule') == body
        assert client.post('/api/schedule', json={**body, 'background': False}, headers=headers).is_success
        assert calls[-1] == (root, False, '09:45')


@pytest.mark.parametrize('code,expected', [(0, '完成'), (1, '部分'), (2, '没有来源')])
def test_unattended_result_survives_restart(tmp_path, monkeypatch, code, expected):
    monkeypatch.setattr(scheduler, 'run', lambda root: ({'collected': 4}, code))
    assert scheduler.daily_result(tmp_path) == code
    result = Preferences(tmp_path).option('last_external_run')
    assert expected in result['message'] and '4 条' in result['message']


def test_unattended_busy_records_skip(tmp_path, monkeypatch):
    def busy(root):
        raise scheduler.AlreadyRunning()
    monkeypatch.setattr(scheduler, 'run', busy)
    assert scheduler.daily_result(tmp_path) == 3
    assert '跳过' in Preferences(tmp_path).option('last_external_run')['message']

"""Windows desktop host. UI assets are immutable; user data stays outside the bundle."""
import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import threading
import time

from .config import ROOT


def data_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'AgentRadar'


def prepare_root(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ('data', 'reports', 'logs'):
        (root / name).mkdir(exist_ok=True)
    if not (root / 'config.json').exists():
        shutil.copyfile(ROOT / 'config.json', root / 'config.json')
    return root


def migrate(source, destination):
    """Explicit, non-destructive import into a fresh directory using SQLite backup."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination in source.parents or source in destination.parents:
        raise ValueError('迁移目录不能相同或互相包含')
    if not (source / 'config.json').is_file():
        raise ValueError('所选目录不是 AgentRadar 数据目录')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('迁移目标必须为空，已有数据不会被覆盖')
    config = json.loads((source / 'config.json').read_text(encoding='utf-8-sig'))
    # Do not copy credentials from config/env; users re-enter keys in the GUI.
    config.get('llm', {}).pop('api_key', None)
    config.pop('github_token', None)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        (destination / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        (destination / 'data').mkdir(exist_ok=True)
        if (source / 'data/radar.db').exists():
            with sqlite3.connect((source / 'data/radar.db').as_uri() + '?mode=ro', uri=True) as old:
                with sqlite3.connect(destination / 'data/radar.db') as new:
                    old.backup(new)
        if (source / 'reports').is_dir():
            shutil.copytree(source / 'reports', destination / 'reports')
        # Copy non-secret source settings, not credentials bound to the old root.
        if (source / 'data/settings.db').exists():
            from .preferences import Preferences
            previous, current = Preferences(source), Preferences(destination)
            for item in previous.list('source'):
                current.save('source', item, item['id'])
            current.set_option('sources_initialized', True)
        return prepare_root(destination)
    except Exception:
        # Partial import is retained for diagnosis, never delete an unrelated directory.
        raise ValueError('迁移未完成；原目录未修改，请检查目标目录后使用新的空目录重试') from None


class SingleInstance:
    def __init__(self, root):
        self.handle = None
        self.root = root

    def acquire(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        name = 'Local\\AgentRadar-' + hashlib.sha256(str(self.root).casefold().encode()).hexdigest()[:24]
        self.handle = kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise OSError('Cannot create application mutex')
        return ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(self.handle)
            self.handle = None


def show_error(message):
    ctypes.windll.user32.MessageBoxW(None, message, 'AgentRadar', 0x10)


def launch(root):
    import uvicorn
    import webview
    from .web import create_app

    app = create_app(root)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))  # Hold the socket: no free-port probe race.
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True, name='radar-desktop-api')
    thread.start()
    until = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < until:
        time.sleep(.05)
    if not server.started:
        server.should_exit = True
        thread.join(5)
        sock.close()
        raise RuntimeError('本地服务启动失败')
    window = webview.create_window('AgentRadar', f'http://127.0.0.1:{port}/overview', width=1360, height=860,
                                    min_size=(720, 560), background_color='#edf2ff', text_select=True)
    closing = threading.Event()

    def on_closing():
        if app.state.prepare_close():
            return True
        if not closing.is_set():
            closing.set()
            window.set_title('AgentRadar · 正在保存任务，完成后自动关闭')
            def finish():
                while not app.state.prepare_close():
                    time.sleep(.2)
                window.destroy()
            threading.Thread(target=finish, daemon=True).start()
        return False
    window.events.closing += on_closing
    try:
        # Force modern WebView2; never silently use obsolete IE renderer.
        webview.start(gui='edgechromium', debug=False, private_mode=True)
    finally:
        app.state.prepare_close()
        server.should_exit = True
        thread.join(10)
        sock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=data_root())
    parser.add_argument('--migrate-from', type=Path)
    parser.add_argument('--daily', action='store_true', help='Run once without a window')
    args = parser.parse_args()
    if args.daily:
        from .windows_schedule import daily_result
        return daily_result(prepare_root(args.data_dir))
    instance = SingleInstance(args.data_dir.resolve())
    try:
        if not instance.acquire():
            if not args.daily:
                ctypes.windll.user32.MessageBoxW(None, 'AgentRadar 已在运行，请返回已有窗口。', 'AgentRadar', 0x40)
            return 0
        if args.migrate_from:
            migrate(args.migrate_from, args.data_dir)
        root = prepare_root(args.data_dir)
        handler = RotatingFileHandler(root / 'logs/desktop.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        logger = logging.getLogger('agentradar.desktop')
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.info('desktop start')
        try:
            launch(root)
        except Exception as exc:
            logger.error('desktop failure: %s', type(exc).__name__)
            if not args.daily:
                show_error('启动或运行失败：' + type(exc).__name__ + '\n请检查 WebView2 Runtime，并查看数据目录中的 logs/desktop.log。')
            return 1
        finally:
            logger.info('desktop stopped')
            handler.close()
            logger.removeHandler(handler)
    except Exception as exc:
        if not args.daily:
            show_error('无法启动：' + type(exc).__name__ + '。请检查数据目录权限与迁移目标是否为空。')
        return 1
    finally:
        instance.close()
    return 0

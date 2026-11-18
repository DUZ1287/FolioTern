"""Manage one interactive-user Windows task per AgentRadar data directory."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime

from .analysis import CST
from .config import ROOT
from .pipeline import AlreadyRunning, run
from .preferences import Preferences


class ScheduleError(Exception):
    pass


# No user input becomes PowerShell code. All variable data is passed separately.
_REGISTER = """
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\\')
$name = $env:AR_TASK_NAME
$marker = 'AgentRadar desktop: ' + $env:AR_TASK_ROOT
$old = $null
try { $old = $folder.GetTask($name) } catch {
    if ($_.Exception.HResult -ne -2147024894) { throw }
}
if ($old -and $old.Definition.RegistrationInfo.Description -cne $marker) { exit 3 }
if ($env:AR_TASK_ENABLE -eq '0') {
    if ($old) { $folder.DeleteTask($name, 0) }
    exit 0
}
$task = $service.NewTask(0)
$task.RegistrationInfo.Description = $marker
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$task.Principal.UserId = $identity
$task.Principal.LogonType = 3
$task.Principal.RunLevel = 0
$task.Settings.StartWhenAvailable = $true
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
$task.Settings.MultipleInstances = 2
$task.Settings.ExecutionTimeLimit = 'PT20M'
$trigger = $task.Triggers.Create(2)
$trigger.DaysInterval = 1
$trigger.StartBoundary = $env:AR_TASK_START
$action = $task.Actions.Create(0)
$action.Path = $env:AR_TASK_EXE
$action.Arguments = $env:AR_TASK_ARGS
$action.WorkingDirectory = $env:AR_TASK_ROOT
$folder.RegisterTaskDefinition($name, $task, 6, $identity, $null, 3) | Out-Null
"""


def task_name(root):
    digest = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()
    return 'AgentRadar-Desktop-' + digest[:16]


def configure(root, enabled, clock):
    root = Path(root).resolve()
    if sys.platform != 'win32':
        raise ScheduleError('此模式需要 Windows 任务计划程序。')
    executable = Path(sys.executable)
    arguments = ['--daily', '--data-dir', str(root)]
    if not getattr(sys, 'frozen', False):
        executable = executable.with_name('pythonw.exe')
        arguments.insert(0, str(ROOT / 'desktop.py'))
    env = dict(os.environ, AR_TASK_EXE=str(executable), AR_TASK_ARGS=subprocess.list2cmdline(arguments),
               AR_TASK_NAME=task_name(root), AR_TASK_ROOT=str(root), AR_TASK_ENABLE=str(int(enabled)),
               AR_TASK_START=f'{datetime.now(CST).date()}T{clock}:00+08:00')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                 base64.b64encode(_REGISTER.encode('utf-16le')).decode('ascii')],
                                env=env, timeout=30, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduleError('无法确认 Windows 任务更新结果，请检查任务计划程序并重试。') from None
    if result.returncode == 3:
        raise ScheduleError('已有其他程序使用同名任务，请在任务计划程序中检查。')
    if result.returncode:
        raise ScheduleError('Windows 未能更新任务。请检查当前用户权限和任务计划程序服务。')


def daily_result(root):
    """Run under the pipeline lock, including when the desktop window is open."""
    prefs = Preferences(root)
    try:
        report, code = run(root)
        message = ('采集完成', '部分来源未完整采集', '没有来源成功返回')[code]
        message += f'，本次收录 {report["collected"]} 条。'
    except AlreadyRunning:
        code, message = 3, '跳过本次采集，另一个任务正在运行。'
    except Exception as exc:
        code, message = 2, '采集失败：' + type(exc).__name__
    prefs.set_option('last_external_run', {'at': datetime.now(CST).isoformat(), 'code': code, 'message': message})
    return code

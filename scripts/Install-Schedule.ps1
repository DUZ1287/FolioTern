$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$entryPath = Join-Path $projectRoot 'scripts\run_daily.py'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Project Python environment is missing.' }
if (-not (Test-Path -LiteralPath $entryPath)) { throw 'Daily entry script is missing.' }
$taskName = 'AgentRadar-Daily-0830'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($oldTask) {
    if ($oldTask.Description -ne "AgentRadar local daily report: $projectRoot") { throw 'A different task uses this name; it was not changed.' }
}
$chinaZone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$chinaNow = [TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $chinaZone)
$chinaMorning = [datetime]::SpecifyKind($chinaNow.Date.AddHours(8).AddMinutes(30), [DateTimeKind]::Unspecified)
$localMorning = [TimeZoneInfo]::ConvertTime($chinaMorning, $chinaZone, [TimeZoneInfo]::Local)
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $entryPath + '"') -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $localMorning
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "AgentRadar local daily report: $projectRoot" -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object NextRunTime,LastTaskResult

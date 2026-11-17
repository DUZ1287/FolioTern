$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$taskName = 'AgentRadar-Daily-0830'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.Description -ne "AgentRadar local daily report: $projectRoot") { throw 'Task belongs to another location; nothing changed.' }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    'AgentRadar daily task removed. Project files and reports remain.'
}

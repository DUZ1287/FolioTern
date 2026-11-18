$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$address = 'http://127.0.0.1:8765'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Virtual environment is missing. See README.md.' }
$ready = $false
try { $status = Invoke-RestMethod -Uri "$address/api/status" -TimeoutSec 2; $ready = $null -ne $status.running } catch { }
if (-not $ready) {
    $logDirectory = Join-Path $projectRoot 'logs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    Start-Process -FilePath $pythonPath -ArgumentList @('radar.py','serve') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'server.out.log') -RedirectStandardError (Join-Path $logDirectory 'server.err.log') | Out-Null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $status = Invoke-RestMethod -Uri "$address/api/status" -TimeoutSec 1; if ($null -ne $status.running) { $ready = $true; break } } catch { }
    }
}
if (-not $ready) { throw 'FolioTern did not start. Check logs/server.err.log.' }
Start-Process $address

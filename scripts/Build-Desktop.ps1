$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $project
try {
    & '.\.venv\Scripts\python.exe' -m PyInstaller FolioTern.spec --distpath dist/windows --noconfirm
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
    Write-Output (Join-Path $project 'dist\windows\FolioTern\FolioTern.exe')
    $packageDir = Join-Path $project 'dist\windows\FolioTern'
    $expat = & '.\.venv\Scripts\python.exe' -c "from pathlib import Path; import sys; print(Path(sys.base_prefix) / 'Library/bin/libexpat.dll')"
    if (Test-Path -LiteralPath $expat) {
        $expected = (Get-FileHash -LiteralPath $expat).Hash
        $actual = (Get-FileHash -LiteralPath (Join-Path $packageDir '_internal\libexpat.dll')).Hash
        if ($actual -ne $expected) { throw 'The bundled XML parser differs from the build interpreter.' }
    }
    $archive = Join-Path $project 'dist\windows\FolioTern-Windows-x64.zip'
    Compress-Archive -LiteralPath $packageDir -DestinationPath $archive -Force
    Write-Output $archive
} finally { Pop-Location }

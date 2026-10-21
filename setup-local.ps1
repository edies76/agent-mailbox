param([int]$Port = 8795)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Instala Python 3 y vuelve a ejecutar este script.' }
    & $pythonCommand.Source -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar Python. Revisa la instalación de Python y vuelve a intentar.' }
}
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias locales.' }
$localSecrets = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $localSecrets -Force | Out-Null
$adminFile = Join-Path $localSecrets 'admin-key'
$joinFile = Join-Path $localSecrets 'join-key'
if (-not (Test-Path -LiteralPath $adminFile)) {
    & $venvPython -c 'from pathlib import Path; import secrets; Path(".local/admin-key").write_text(secrets.token_urlsafe(32), encoding="utf-8")'
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la clave local de emergencia.' }
}
if (-not (Test-Path -LiteralPath $joinFile)) {
    & $venvPython -c 'from pathlib import Path; import secrets; Path(".local/join-key").write_text(secrets.token_urlsafe(16), encoding="utf-8")'
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la clave local de inscripción.' }
}
$env:MAILBOX_ADMIN_KEY = (Get-Content -LiteralPath $adminFile -Raw).Trim()
$env:MAILBOX_JOIN_KEY = (Get-Content -LiteralPath $joinFile -Raw).Trim()
$env:MAILBOX_STORAGE = 'sqlite'
$env:MAILBOX_LOCAL_FIRST_RUN = '1'
$env:MAILBOX_DB = 'data/mailbox.sqlite3'
Remove-Item Env:MAILBOX_PUBLIC_URL -ErrorAction SilentlyContinue
Remove-Item Env:MAILBOX_MCP_ALLOWED_HOSTS -ErrorAction SilentlyContinue
& .\start.ps1 -Port $Port -BindAddress '127.0.0.1'
exit $LASTEXITCODE

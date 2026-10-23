param([int]$Port = 8795, [string]$BindAddress = '127.0.0.1')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Primero ejecuta: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
if (-not $env:MAILBOX_STORAGE) { $env:MAILBOX_STORAGE = 'sqlite' }
if ($env:MAILBOX_STORAGE -eq 'sqlite') { $env:MAILBOX_LOCAL_FIRST_RUN = '1' }
else { $env:MAILBOX_LOCAL_FIRST_RUN = '0' }
if (-not $env:MAILBOX_ADMIN_KEY) {
    $secretDirectory = Join-Path $PSScriptRoot '.local'
    $secretFile = Join-Path $secretDirectory 'admin-key'
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $secretFile)) {
        & $pythonExe -c 'import pathlib,secrets; pathlib.Path(".local/admin-key").write_text(secrets.token_urlsafe(32), encoding="utf-8")'
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la clave del servidor.' }
    }
    $env:MAILBOX_ADMIN_KEY = (Get-Content -LiteralPath $secretFile -Raw).Trim()
}
if ($env:MAILBOX_LOCAL_FIRST_RUN -eq '1' -and -not $env:MAILBOX_LOCAL_INSTANCE_SEED) {
    $localStateDirectory = Join-Path $PSScriptRoot '.local'
    $instanceSeedFile = Join-Path $localStateDirectory 'instance-seed'
    New-Item -ItemType Directory -Path $localStateDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $instanceSeedFile)) {
        & $pythonExe -c 'import pathlib,secrets; pathlib.Path(".local/instance-seed").write_text(secrets.token_urlsafe(48), encoding="utf-8")'
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar la identidad local del servidor.' }
    }
    $env:MAILBOX_LOCAL_INSTANCE_SEED = (Get-Content -LiteralPath $instanceSeedFile -Raw).Trim()
}
if (-not $env:MAILBOX_JOIN_KEY -and $env:MAILBOX_LOCAL_FIRST_RUN -ne '1') {
    $joinKeyFile = Join-Path $PSScriptRoot '.local\join-key'
    New-Item -ItemType Directory -Path (Split-Path -Parent $joinKeyFile) -Force | Out-Null
    if (-not (Test-Path -LiteralPath $joinKeyFile)) {
        & $pythonExe -c 'import pathlib,secrets; pathlib.Path(".local/join-key").write_text(secrets.token_urlsafe(16), encoding="utf-8")'
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la clave de acceso.' }
    }
    $env:MAILBOX_JOIN_KEY = (Get-Content -LiteralPath $joinKeyFile -Raw).Trim()
}
& $pythonExe -m uvicorn server:create_app --factory --host $BindAddress --port $Port
exit $LASTEXITCODE


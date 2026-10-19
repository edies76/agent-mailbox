param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [string]$PythonPath = 'python'
)

$ErrorActionPreference = 'Stop'
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$python = (Get-Command $PythonPath -ErrorAction Stop).Source
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$listener = Join-Path $repositoryRoot 'mailbox_listener.py'
$logPath = "${manifestPath}.listener.log"

& $python $listener $manifestPath 2>&1 | ForEach-Object {
    Add-Content -LiteralPath $logPath -Value $_ -Encoding utf8
}
exit $LASTEXITCODE

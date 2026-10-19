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

& $python $listener $manifestPath --log-file $logPath
exit $LASTEXITCODE

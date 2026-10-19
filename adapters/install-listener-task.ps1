param(
    [string]$Manifest,
    [string]$TaskName = 'Agent Mailbox Universal Listener',
    [string]$PythonPath = 'python',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Remove) {
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled listener task: $TaskName"
    } else {
        Write-Output "No scheduled listener task named '$TaskName'."
    }
    exit 0
}

if (-not $Manifest) { throw 'Pass -Manifest with the local JSON listener manifest.' }
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$python = (Get-Command $PythonPath -ErrorAction Stop).Source
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$listener = Join-Path $repositoryRoot 'mailbox_listener.py'
$runner = Join-Path $PSScriptRoot 'run-mailbox-listener.ps1'
& $python $listener $manifestPath --validate
if ($LASTEXITCODE -ne 0) { throw 'Listener manifest validation failed; task was not installed.' }

$escapedRunner = $runner.Replace("'", "''")
$escapedManifest = $manifestPath.Replace("'", "''")
$escapedPython = $python.Replace("'", "''")
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File '$escapedRunner' " +
    "-Manifest '$escapedManifest' -PythonPath '$escapedPython'")
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'Supervise private Agent Mailbox inboxes and local adapters.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Installed and started '$TaskName'. Log: ${manifestPath}.listener.log"

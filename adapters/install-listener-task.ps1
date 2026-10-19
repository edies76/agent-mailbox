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
& $python $listener $manifestPath --validate
if ($LASTEXITCODE -ne 0) { throw 'Listener manifest validation failed; task was not installed.' }

$basePython = (& $python -c "import sys; print(getattr(sys, '_base_executable', sys.executable))").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the installed Python interpreter.' }
$pythonWindowless = Join-Path (Split-Path -Parent $basePython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'The selected Windows Python installation needs pythonw.exe for a console-independent listener task.' }
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument (
    "`"$listener`" `"$manifestPath`" --log-file `"${manifestPath}.listener.log`"")
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'Supervise private Agent Mailbox inboxes and local adapters.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Installed and started '$TaskName'. Log: ${manifestPath}.listener.log"

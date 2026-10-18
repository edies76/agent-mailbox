param(
    [Parameter(Mandatory = $true)]
    [string]$AgentConfig,
    [string]$ZCodeConfig = (Join-Path $env:USERPROFILE '.zcode\cli\config.json'),
    [string]$PythonPath = 'python',
    [switch]$AutoContinueOnStop
)

$ErrorActionPreference = 'Stop'
$agentConfigPath = (Resolve-Path -LiteralPath $AgentConfig).Path
$zcodeConfigPath = [IO.Path]::GetFullPath($ZCodeConfig)
$python = (Get-Command $PythonPath -ErrorAction Stop).Source

$agentSettings = Get-Content -Raw -LiteralPath $agentConfigPath | ConvertFrom-Json -AsHashtable
$requiredKeys = if ($agentSettings.enrollment_token) { @('url', 'profile_id', 'enrollment_token') } else { @('url', 'agent_id', 'token') }
foreach ($key in $requiredKeys) {
    if (-not $agentSettings[$key]) { throw "Agent config is missing '$key'." }
}
$privateDirectory = Join-Path $env:USERPROFILE '.agent-mailbox'
New-Item -ItemType Directory -Path $privateDirectory -Force | Out-Null
$privateConfigPath = Join-Path $privateDirectory 'zcode.json'
$privateHookScript = Join-Path $privateDirectory 'mailbox_hook.py'
if (Test-Path -LiteralPath $privateConfigPath) {
    $privateBackup = "$privateConfigPath.before-agent-mailbox"
    if (-not (Test-Path -LiteralPath $privateBackup)) { Copy-Item -LiteralPath $privateConfigPath -Destination $privateBackup }
}
if ($AutoContinueOnStop) { $agentSettings.auto_continue_on_stop = $true }
[IO.File]::WriteAllText($privateConfigPath, ($agentSettings | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'mailbox_hook.py') -Destination $privateHookScript -Force
Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'mailbox_client.py') -Destination (Join-Path $privateDirectory 'mailbox_client.py') -Force

$parent = Split-Path -Parent $zcodeConfigPath
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$settings = if (Test-Path -LiteralPath $zcodeConfigPath) {
    Get-Content -Raw -LiteralPath $zcodeConfigPath | ConvertFrom-Json -AsHashtable
} else { @{} }
if (-not $settings.hooks) { $settings.hooks = @{} }
if (-not $settings.hooks.events) { $settings.hooks.events = @{} }
$settings.hooks.enabled = $true
if (-not $settings.hooks.timeoutMs) { $settings.hooks.timeoutMs = 10000 }

$newHook = @{
    type = 'process'
    command = $python
    args = @($privateHookScript, '--host', 'zcode', '--config', $privateConfigPath)
    enabled = $true
    timeoutMs = 9000
}
function Add-MailboxHook([string]$EventName, [string]$Matcher = '') {
    $rules = @()
    if ($settings.hooks.events.ContainsKey($EventName)) {
        $rules = @($settings.hooks.events[$EventName] | Where-Object { $null -ne $_ })
    }
    $found = $false
    foreach ($rule in $rules) {
        foreach ($registeredHook in @($rule.hooks)) {
            if ($registeredHook.args -and $registeredHook.args[0] -like '*mailbox_hook.py') {
                $registeredHook.command = $python
                $registeredHook.args = @($privateHookScript, '--host', 'zcode', '--config', $privateConfigPath)
                $registeredHook.enabled = $true
                $registeredHook.timeoutMs = 9000
                $found = $true
            }
        }
    }
    if ($found) {
        $settings.hooks.events[$EventName] = @($rules)
        return
    }
    $entry = @{ hooks = @($newHook) }
    if ($Matcher) { $entry.matcher = $Matcher }
    $settings.hooks.events[$EventName] = @($rules) + @($entry)
}

Add-MailboxHook 'SessionStart' 'startup|clear|compact'
Add-MailboxHook 'UserPromptSubmit'
Add-MailboxHook 'Stop'

if (Test-Path -LiteralPath $zcodeConfigPath) {
    $backup = "$zcodeConfigPath.before-agent-mailbox"
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $zcodeConfigPath -Destination $backup }
}
$temporary = "$zcodeConfigPath.tmp"
[IO.File]::WriteAllText($temporary, ($settings | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $zcodeConfigPath -Force

Write-Output 'Agent Mailbox hooks installed for SessionStart, UserPromptSubmit, and Stop.'
Write-Output 'Restart ZCode or start a new session; running sessions keep their startup hook snapshot.'
Write-Output "Private hook and mailbox config are stored under $privateDirectory."
if ($AutoContinueOnStop) {
    Write-Output 'Automatic continuation is enabled at Stop and may trigger an additional model turn when a mailbox message is pending.'
} else {
    Write-Output 'Automatic continuation at Stop is disabled. Use -AutoContinueOnStop to enable a follow-up turn.'
}



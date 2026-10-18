param(
    [Parameter(Mandatory = $true)]
    [string]$AgentConfig,
    [string]$ZCodeConfig = (Join-Path $env:USERPROFILE '.zcode\cli\config.json'),
    [string]$PythonPath = 'python',
    [switch]$AutoContinueOnStop
)

$ErrorActionPreference = 'Stop'
$hookScript = Join-Path $PSScriptRoot 'mailbox_hook.py'
$agentConfigPath = (Resolve-Path -LiteralPath $AgentConfig).Path
$zcodeConfigPath = [IO.Path]::GetFullPath($ZCodeConfig)
$python = (Get-Command $PythonPath -ErrorAction Stop).Source

$agentSettings = Get-Content -Raw -LiteralPath $agentConfigPath | ConvertFrom-Json -AsHashtable
foreach ($key in @('url', 'agent_id', 'token')) {
    if (-not $agentSettings[$key]) { throw "Agent config is missing '$key'." }
}

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
    args = @($hookScript, '--host', 'zcode', '--config', $agentConfigPath)
    enabled = $true
    timeoutMs = 9000
}
function Add-MailboxHook([string]$EventName, [string]$Matcher = '') {
    $rules = @()
    if ($settings.hooks.events.ContainsKey($EventName)) {
        $rules = @($settings.hooks.events[$EventName] | Where-Object { $null -ne $_ })
    }
    $existing = $rules | Where-Object {
        $_.hooks | Where-Object { $_.args -and $_.args[0] -eq $hookScript }
    }
    if ($existing) {
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

if ($AutoContinueOnStop) {
    $agentSettings.auto_continue_on_stop = $true
    $agentBackup = "$agentConfigPath.before-zcode-hook"
    if (-not (Test-Path -LiteralPath $agentBackup)) { Copy-Item -LiteralPath $agentConfigPath -Destination $agentBackup }
    $agentTemp = "$agentConfigPath.tmp"
    [IO.File]::WriteAllText($agentTemp, ($agentSettings | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $agentTemp -Destination $agentConfigPath -Force
}

if (Test-Path -LiteralPath $zcodeConfigPath) {
    $backup = "$zcodeConfigPath.before-agent-mailbox"
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $zcodeConfigPath -Destination $backup }
}
$temporary = "$zcodeConfigPath.tmp"
[IO.File]::WriteAllText($temporary, ($settings | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $zcodeConfigPath -Force

Write-Output 'Agent Mailbox hooks installed for SessionStart, UserPromptSubmit, and Stop.'
Write-Output 'Restart ZCode or start a new session; running sessions keep their startup hook snapshot.'
if ($AutoContinueOnStop) {
    Write-Output 'Automatic continuation is enabled at Stop and may trigger an additional model turn when a mailbox message is pending.'
} else {
    Write-Output 'Automatic continuation at Stop is disabled. Use -AutoContinueOnStop to enable a follow-up turn.'
}


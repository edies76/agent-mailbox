param(
    [Parameter(Mandatory = $true)]
    [string]$AgentConfig,
    [string]$ClaudeSettings = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$PythonCommand = 'python',
    [switch]$AutoContinueOnStop
)

$ErrorActionPreference = 'Stop'
$hookScript = Join-Path $PSScriptRoot 'mailbox_hook.py'
$agentConfigPath = (Resolve-Path -LiteralPath $AgentConfig).Path
$settingsPath = [IO.Path]::GetFullPath($ClaudeSettings)
$python = (Get-Command $PythonCommand -ErrorAction Stop).Source
$command = '"{0}" "{1}" --host claude --config "{2}"' -f $python, $hookScript, $agentConfigPath

$agentSettings = Get-Content -Raw -LiteralPath $agentConfigPath | ConvertFrom-Json -AsHashtable
foreach ($key in @('url', 'agent_id', 'token')) {
    if (-not $agentSettings[$key]) { throw "Agent config is missing '$key'." }
}

$parent = Split-Path -Parent $settingsPath
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$settings = if (Test-Path -LiteralPath $settingsPath) {
    Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json -AsHashtable
} else { @{} }
if (-not $settings.hooks) { $settings.hooks = @{} }

function Add-MailboxHook([string]$EventName, [string]$Matcher = '') {
    $rules = @()
    if ($settings.hooks.ContainsKey($EventName)) {
        $rules = @($settings.hooks[$EventName] | Where-Object { $null -ne $_ })
    }
    $existing = $rules | Where-Object {
        $_.hooks | Where-Object { $_.command -and $_.command -like '*mailbox_hook.py*' }
    }
    if ($existing) {
        $settings.hooks[$EventName] = @($rules)
        return
    }
    $commandHook = @{ type = 'command'; command = $command; timeout = 10 }
    $entry = @{ hooks = @($commandHook) }
    if ($Matcher) { $entry.matcher = $Matcher }
    $settings.hooks[$EventName] = @($rules) + @($entry)
}

Add-MailboxHook 'SessionStart'
Add-MailboxHook 'UserPromptSubmit'
Add-MailboxHook 'Stop'

if ($AutoContinueOnStop) {
    $agentSettings.auto_continue_on_stop = $true
    $agentBackup = "$agentConfigPath.before-claude-hook"
    if (-not (Test-Path -LiteralPath $agentBackup)) { Copy-Item -LiteralPath $agentConfigPath -Destination $agentBackup }
    $agentTemp = "$agentConfigPath.tmp"
    [IO.File]::WriteAllText($agentTemp, ($agentSettings | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $agentTemp -Destination $agentConfigPath -Force
}

if (Test-Path -LiteralPath $settingsPath) {
    $backup = "$settingsPath.before-agent-mailbox"
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $settingsPath -Destination $backup }
}
$temporary = "$settingsPath.tmp"
[IO.File]::WriteAllText($temporary, ($settings | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $settingsPath -Force

Write-Output 'Agent Mailbox hooks installed for SessionStart, UserPromptSubmit, and Stop.'
Write-Output 'Start a new Claude Code session for the new hooks to take effect.'
if ($AutoContinueOnStop) {
    Write-Output 'Automatic continuation is enabled at Stop and can trigger an additional model turn when mail is pending.'
} else {
    Write-Output 'Automatic continuation at Stop is disabled; use -AutoContinueOnStop to enable a follow-up turn.'
}


param(
    [Parameter(Mandatory = $true)]
    [string]$AgentConfig,
    [ValidateRange(5, 300)]
    [int]$IntervalSeconds = 15
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$configPath = [IO.Path]::GetFullPath((Join-Path $repo $AgentConfig))
$settings = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
if (-not $settings.url -or -not $settings.agent_id -or -not $settings.token) {
    throw 'AgentConfig debe contener url, agent_id y token.'
}

$stateDir = Join-Path $repo '.local\listener-state'
$logDir = Join-Path $repo '.local\notifications'
New-Item -ItemType Directory -Path $stateDir, $logDir -Force | Out-Null
$statePath = Join-Path $stateDir "$($settings.agent_id).json"
$logPath = Join-Path $logDir "$($settings.agent_id).jsonl"
$pidPath = Join-Path $stateDir "$($settings.agent_id).pid"
$state = if (Test-Path -LiteralPath $statePath) {
    Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
} else {
    [pscustomobject]@{ after_id = 0 }
}
$cursor = [long]$state.after_id
$baseUrl = $settings.url.TrimEnd('/')
$headers = @{ Authorization = "Bearer $($settings.token)" }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notifyIcon = [System.Windows.Forms.NotifyIcon]::new()
$notifyIcon.Icon = [System.Drawing.SystemIcons]::Information
$notifyIcon.Text = 'Agent Mailbox · escuchando'
$notifyIcon.Visible = $true
Set-Content -LiteralPath $pidPath -Value $PID -Encoding ascii

function Save-Cursor([long]$Value) {
    @{ after_id = $Value } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Show-MessageNotification($Message) {
    $sender = if ($Message.from_agent_id -eq $settings.agent_id) { 'Mensaje propio' } else { 'Nuevo mensaje de un agente' }
    $text = ([string]$Message.text -replace '\s+', ' ').Trim()
    if ($Message.attachments.Count -gt 0) {
        $imageLabel = "📷 $($Message.attachments.Count) imagen(es)"
        $text = if ($text) { "$text · $imageLabel" } else { $imageLabel }
    }
    if (-not $text) { $text = 'Mensaje nuevo en el buzón.' }
    if ($text.Length -gt 180) { $text = $text.Substring(0, 177) + '…' }
    $notifyIcon.ShowBalloonTip(8000, $sender, $text, [System.Windows.Forms.ToolTipIcon]::Info)
    [System.Windows.Forms.Application]::DoEvents()
}

try {
    while ($true) {
        $started = [DateTimeOffset]::UtcNow
        try {
            $after = $cursor
            do {
                $url = "$baseUrl/inbox?unread_only=true&after_id=$after&limit=200"
                $page = Invoke-RestMethod -Method Get -Uri $url -Headers $headers -TimeoutSec 12
                foreach ($message in $page.messages) {
                    $record = [ordered]@{
                        observed_at = [DateTimeOffset]::UtcNow.ToString('o')
                        id = $message.id
                        from_agent_id = $message.from_agent_id
                        to_agent_id = $message.to_agent_id
                        text = $message.text
                        attachments = $message.attachments
                        created_at = $message.created_at
                        acknowledged_at = $message.acknowledged_at
                    }
                    Add-Content -LiteralPath $logPath -Value ($record | ConvertTo-Json -Compress) -Encoding utf8
                    Show-MessageNotification $message
                    $cursor = [long]$message.id
                    $after = $cursor
                    Save-Cursor $cursor
                }
                if ($page.messages.Count -eq 0) { break }
            } while ($page.has_more)
        } catch {
            $line = "{0} poll_error {1}" -f [DateTimeOffset]::UtcNow.ToString('o'), $_.Exception.Message
            Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
        }
        [System.Windows.Forms.Application]::DoEvents()
        $remaining = $IntervalSeconds - ([DateTimeOffset]::UtcNow - $started).TotalSeconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int]($remaining * 1000)) }
    }
} finally {
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}


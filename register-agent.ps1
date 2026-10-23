param(
    [Parameter(Mandatory=$true)][string]$Name,
    [string]$Kind = 'other',
    [string]$Project = '',
    [string]$BaseUrl = '',
    [string]$SessionId = [guid]::NewGuid().ToString()
)
$ErrorActionPreference = 'Stop'
if (-not $BaseUrl) {
    $deploymentFile = Join-Path $PSScriptRoot '.local\deployment.json'
    if (-not (Test-Path -LiteralPath $deploymentFile)) { throw 'Indica -BaseUrl o ejecuta deploy.ps1 primero.' }
    $BaseUrl = (Get-Content -LiteralPath $deploymentFile -Raw | ConvertFrom-Json).url
}
$adminKey = if ($env:MAILBOX_ADMIN_KEY) { $env:MAILBOX_ADMIN_KEY } else {
    (Get-Content -LiteralPath (Join-Path $PSScriptRoot '.local\admin-key') -Raw).Trim()
}
$headers = @{ Authorization = 'Bearer ' + $adminKey }
$body = @{ name = $Name; kind = $Kind; project = $Project; session_key = $SessionId } | ConvertTo-Json
$registration = Invoke-RestMethod -Method Post -Uri "$($BaseUrl.TrimEnd('/'))/agents" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$directory = Join-Path $PSScriptRoot '.local\agents'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$credentialsFile = Join-Path $directory "$($registration.agent.id).json"
$instructionsFile = Join-Path $directory "$($registration.agent.id).md"
@{ url = $BaseUrl; agent_id = $registration.agent.id; session_id = $registration.session_id; agent = $registration.agent; token = $registration.token; onboarding_url = '/agent-mailbox.md' } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $credentialsFile -Encoding utf8
$instructions = "Servidor: $BaseUrl`n`n" + $registration.onboarding.prompt + "`n`n" + $registration.onboarding.guide
Set-Content -LiteralPath $instructionsFile -Value $instructions -Encoding utf8
[PSCustomObject]@{ agent_id = $registration.agent.id; name = $registration.agent.name; credentials_file = $credentialsFile; instructions_file = $instructionsFile }


param(
    [Parameter(Mandatory=$true)][string]$Name,
    [string]$Kind = 'other',
    [string]$Project = '',
    [string]$BaseUrl = '',
    [string]$SessionId = [guid]::NewGuid().ToString(),
    [string]$ExistingConfig = ''
)
$ErrorActionPreference = 'Stop'
if ($ExistingConfig) {
    $existing = Get-Content -LiteralPath $ExistingConfig -Raw | ConvertFrom-Json
    if (-not $BaseUrl) { $BaseUrl = [string]$existing.url }
    if (-not $PSBoundParameters.ContainsKey('SessionId')) { $SessionId = [string]$existing.session_id }
}
if (-not $BaseUrl) {
    $deploymentFile = Join-Path $PSScriptRoot '.local\deployment.json'
    if (-not (Test-Path -LiteralPath $deploymentFile)) { throw 'Indica -BaseUrl o ejecuta deploy.ps1 primero.' }
    $BaseUrl = (Get-Content -LiteralPath $deploymentFile -Raw | ConvertFrom-Json).url
}
$parsedUrl = [Uri]$BaseUrl
if (-not $parsedUrl.IsAbsoluteUri -or $parsedUrl.UserInfo -or $parsedUrl.Query -or $parsedUrl.Fragment -or
    ($parsedUrl.Scheme -ne 'https' -and -not ($parsedUrl.Scheme -eq 'http' -and $parsedUrl.IsLoopback))) {
    throw 'Usa HTTPS o HTTP únicamente en loopback local para proteger las claves.'
}
$adminKey = if ($env:MAILBOX_ADMIN_KEY) { $env:MAILBOX_ADMIN_KEY } else {
    (Get-Content -LiteralPath (Join-Path $PSScriptRoot '.local\admin-key') -Raw).Trim()
}
$BaseUrl = $BaseUrl.TrimEnd('/')
$capabilities = Invoke-RestMethod -Uri "$BaseUrl/capabilities" -TimeoutSec 15
if ($capabilities.secure_registration.version -ne 1) { throw 'Actualiza el servidor antes de registrar una instancia de forma segura.' }
$directory = Join-Path $PSScriptRoot '.local\agents'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$sha = [Security.Cryptography.SHA256]::Create()
try { $identityHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$BaseUrl|$SessionId"))).Replace('-', '').Substring(0, 24).ToLowerInvariant() }
finally { $sha.Dispose() }
$pendingFile = Join-Path $directory "pending-$identityHash.json"
function Protect-PrivateFile([string]$Path) {
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls $Path /inheritance:r /grant:r "*${sid}:F" '*S-1-5-18:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo proteger el archivo privado.' }
    } else {
        & chmod 600 -- $Path
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo proteger el archivo privado.' }
    }
}
$headers = @{ Authorization = 'Bearer ' + $adminKey }
$recoverySecret = ''
$originalToken = ''
if ($ExistingConfig) {
    if (($existing.url.TrimEnd('/') -cne $BaseUrl) -or ($existing.session_id -cne $SessionId)) { throw 'El JSON privado pertenece a otro servidor o sesión.' }
    if ($existing.token) { $originalToken = [string]$existing.token; $headers['X-Mailbox-Instance-Token'] = $originalToken }
    elseif ($existing.recovery_secret) { $recoverySecret = [string]$existing.recovery_secret; $headers['X-Mailbox-Recovery-Secret'] = $recoverySecret }
    else { throw 'El JSON privado no contiene token ni secreto de recuperación.' }
} else {
    if (Test-Path -LiteralPath $pendingFile) {
        Protect-PrivateFile $pendingFile
        $pending = Get-Content -LiteralPath $pendingFile -Raw | ConvertFrom-Json
        if (($pending.url -cne $BaseUrl) -or ($pending.session_id -cne $SessionId)) { throw 'El registro pendiente pertenece a otro servidor o sesión.' }
        $recoverySecret = [string]$pending.recovery_secret
    } else {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $recoverySecret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        $pending = @{ url=$BaseUrl; session_id=$SessionId; recovery_secret=$recoverySecret } | ConvertTo-Json -Compress
        $stream = [IO.File]::Open($pendingFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false)); try { $writer.Write($pending) } finally { $writer.Dispose() } }
        finally { $stream.Dispose() }
        Protect-PrivateFile $pendingFile
    }
    if ($recoverySecret.Length -lt 32) { throw 'La credencial pendiente es inválida; restaura el JSON privado.' }
    $headers['X-Mailbox-Recovery-Secret'] = $recoverySecret
}
$body = @{ name = $Name; kind = $Kind; project = $Project; session_key = $SessionId } | ConvertTo-Json
$registration = Invoke-RestMethod -Method Post -Uri "$BaseUrl/agents/secure" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
if ($originalToken) { $expectedToken = $originalToken }
else {
    $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($recoverySecret))
    try { $expectedToken = [BitConverter]::ToString($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("agent-mailbox-instance:$($registration.agent.id)"))).Replace('-', '').ToLowerInvariant() }
    finally { $hmac.Dispose() }
}
if ($registration.token -cne $expectedToken) { throw 'El servidor no confirmó la credencial privada; conserva el registro pendiente.' }
$credentialsFile = Join-Path $directory "$($registration.agent.id).json"
$instructionsFile = Join-Path $directory "$($registration.agent.id).md"
$privateConfig = @{}
if ($ExistingConfig) { foreach ($property in $existing.PSObject.Properties) { $privateConfig[$property.Name] = $property.Value } }
$privateConfig.url = $BaseUrl; $privateConfig.agent_id = $registration.agent.id; $privateConfig.session_id = $registration.session_id
$privateConfig.agent = $registration.agent; $privateConfig.token = $registration.token; $privateConfig.onboarding_url = '/agent-mailbox.md'
if ($recoverySecret) { $privateConfig.recovery_secret = $recoverySecret }
$privateConfig | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $credentialsFile -Encoding utf8
Protect-PrivateFile $credentialsFile
if (Test-Path -LiteralPath $pendingFile) { Remove-Item -LiteralPath $pendingFile }
$instructions = "Servidor: $BaseUrl`n`n" + $registration.onboarding.prompt + "`n`n" + $registration.onboarding.guide
Set-Content -LiteralPath $instructionsFile -Value $instructions -Encoding utf8
[PSCustomObject]@{ agent_id = $registration.agent.id; name = $registration.agent.name; credentials_file = $credentialsFile; instructions_file = $instructionsFile }


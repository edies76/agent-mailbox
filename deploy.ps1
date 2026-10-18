param(
    [Parameter(Mandatory = $true)]
    [string]$CloudProject,
    [string]$Region = 'us-central1',
    [string]$FirestoreDatabase = 'agent-mailbox',
    [string]$RuntimeServiceAccount = '',
    [string]$AdminSecretName = 'agent-mailbox-admin-key',
    [string]$JoinSecretName = 'agent-mailbox-join-key',
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$appVersion = (Get-Content -LiteralPath '.\VERSION' -Raw).Trim()
if ($appVersion -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION no tiene formato MAJOR.MINOR.PATCH: $appVersion" }
if (-not $RuntimeServiceAccount) { $RuntimeServiceAccount = "agent-mailbox-runtime@$CloudProject.iam.gserviceaccount.com" }
if (-not $SkipTests) {
    & '.\.venv\Scripts\python.exe' -m unittest -v test_mailbox.py
    if ($LASTEXITCODE -ne 0) { throw 'Las pruebas fallaron; no se publicara.' }
}
& gcloud run deploy agent-mailbox --source . --project=$CloudProject --region=$Region --port=8795 --service-account=$RuntimeServiceAccount --allow-unauthenticated --ingress=all --cpu=1 --memory=512Mi --min=0 --max=2 --concurrency=40 --set-env-vars="MAILBOX_STORAGE=firestore,MAILBOX_FIRESTORE_DATABASE=$FirestoreDatabase,GOOGLE_CLOUD_PROJECT=$CloudProject,MAILBOX_RELEASE=$appVersion" --set-secrets="MAILBOX_ADMIN_KEY=$($AdminSecretName):latest,MAILBOX_JOIN_KEY=$($JoinSecretName):latest" --quiet
if ($LASTEXITCODE -ne 0) { throw 'El despliegue no se completo.' }
$serviceUrl = & gcloud run services describe agent-mailbox --project=$CloudProject --region=$Region --format='value(status.url)'
if ($LASTEXITCODE -ne 0) { throw 'No se pudo obtener la URL.' }
$serviceHost = ([Uri]$serviceUrl.Trim()).Host
& gcloud run services update agent-mailbox --project=$CloudProject --region=$Region --update-env-vars="MAILBOX_MCP_ALLOWED_HOSTS=$serviceHost" --quiet
if ($LASTEXITCODE -ne 0) { throw 'No se pudo configurar el host permitido para MCP.' }
New-Item -ItemType Directory -Path '.local' -Force | Out-Null
@{ url = $serviceUrl.Trim(); project = $CloudProject; region = $Region } | ConvertTo-Json | Set-Content -LiteralPath '.local\deployment.json' -Encoding utf8
Write-Output "Service URL: $($serviceUrl.Trim())"
Invoke-RestMethod -Uri "$($serviceUrl.Trim())/health" -TimeoutSec 90


param(
    [Parameter(Mandatory = $true)]
    [string]$CloudProject,
    [string]$Region = 'us-central1',
    [string]$FirestoreDatabase = 'agent-mailbox',
    [string]$RuntimeServiceAccount = '',
    [string]$AdminSecretName = 'agent-mailbox-admin-key',
    [string]$JoinSecretName = 'agent-mailbox-join-key',
    [switch]$SkipTests,
    [switch]$SkipIndexSetup
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$appVersion = (Get-Content -LiteralPath '.\VERSION' -Raw).Trim()
if ($appVersion -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION no tiene formato MAJOR.MINOR.PATCH: $appVersion" }
if (-not $RuntimeServiceAccount) { $RuntimeServiceAccount = "agent-mailbox-runtime@$CloudProject.iam.gserviceaccount.com" }
if (-not $SkipTests) {
    $testPython = if (Test-Path '.\.venv\Scripts\python.exe') { '.\.venv\Scripts\python.exe' } else { (Get-Command python).Source }
    & $testPython -m unittest -v test_mailbox.py test_adapters.py test_collaboration.py test_control_access.py test_a2a.py test_workspaces.py
    if ($LASTEXITCODE -ne 0) { throw 'Las pruebas fallaron; no se publicara.' }
}
if (-not $SkipIndexSetup) {
    $existingJson = & gcloud firestore indexes composite list --database=$FirestoreDatabase --project=$CloudProject --format=json
    if ($LASTEXITCODE -ne 0) { throw 'No se pudieron consultar los indices de Firestore.' }
    $existingIndexes = $existingJson | ConvertFrom-Json
    $spec = Get-Content -LiteralPath '.\firestore.indexes.json' -Raw | ConvertFrom-Json
    foreach ($index in $spec.indexes) {
        $wanted = ($index.fields | ForEach-Object { "$($_.fieldPath):$($_.order)" }) -join '|'
        $found = @($existingIndexes | Where-Object {
            $fields = ($_.fields | Where-Object fieldPath -ne '__name__' | ForEach-Object { "$($_.fieldPath):$($_.order)" }) -join '|'
            $_.name -like "*/collectionGroups/$($index.collectionGroup)/indexes/*" -and $fields -eq $wanted -and $_.state -ne 'NEEDS_REPAIR'
        })
        if ($found.Count -eq 0) {
            $indexArgs = @('firestore', 'indexes', 'composite', 'create', "--database=$FirestoreDatabase", "--project=$CloudProject",
                           "--collection-group=$($index.collectionGroup)", '--query-scope=collection', '--quiet')
            foreach ($field in $index.fields) { $indexArgs += "--field-config=field-path=$($field.fieldPath),order=$($field.order.ToLowerInvariant())" }
            & gcloud @indexArgs
            if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear un indice requerido.' }
        }
    }
}
& gcloud run deploy agent-mailbox --source . --project=$CloudProject --region=$Region --port=8795 --service-account=$RuntimeServiceAccount --allow-unauthenticated --ingress=all --cpu=1 --memory=512Mi --min=0 --max=2 --concurrency=40 --update-env-vars="MAILBOX_STORAGE=firestore,MAILBOX_FIRESTORE_DATABASE=$FirestoreDatabase,GOOGLE_CLOUD_PROJECT=$CloudProject,MAILBOX_RELEASE=$appVersion" --set-secrets="MAILBOX_ADMIN_KEY=$($AdminSecretName):latest,MAILBOX_JOIN_KEY=$($JoinSecretName):latest" --quiet
if ($LASTEXITCODE -ne 0) { throw 'El despliegue no se completo.' }
$serviceJson = & gcloud run services describe agent-mailbox --project=$CloudProject --region=$Region --format=json
if ($LASTEXITCODE -ne 0) { throw 'No se pudo obtener la URL.' }
$serviceDescription = $serviceJson | ConvertFrom-Json
$serviceUrl = $serviceDescription.status.url
$mcpHosts = @(([Uri]$serviceUrl).Host)
if ($serviceDescription.metadata.annotations.'run.googleapis.com/urls') {
    $mcpHosts += @($serviceDescription.metadata.annotations.'run.googleapis.com/urls' | ConvertFrom-Json | ForEach-Object { ([Uri]$_).Host })
}
$previousHosts = $serviceDescription.spec.template.spec.containers[0].env | Where-Object name -eq 'MAILBOX_MCP_ALLOWED_HOSTS'
if ($previousHosts.value) { $mcpHosts += $previousHosts.value -split ',' }
$allowedHosts = ($mcpHosts | Where-Object { $_ } | Sort-Object -Unique) -join ','
& gcloud run services update agent-mailbox --project=$CloudProject --region=$Region --update-env-vars="^;^MAILBOX_MCP_ALLOWED_HOSTS=$allowedHosts;MAILBOX_PUBLIC_URL=$($serviceUrl.Trim())" --quiet
if ($LASTEXITCODE -ne 0) { throw 'No se pudo configurar el host permitido para MCP.' }
New-Item -ItemType Directory -Path '.local' -Force | Out-Null
@{ url = $serviceUrl.Trim(); project = $CloudProject; region = $Region } | ConvertTo-Json | Set-Content -LiteralPath '.local\deployment.json' -Encoding utf8
Write-Output "Service URL: $($serviceUrl.Trim())"
Invoke-RestMethod -Uri "$($serviceUrl.Trim())/health" -TimeoutSec 90


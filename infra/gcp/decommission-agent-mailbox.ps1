param(
    [Parameter(Mandatory = $true)]
    [string]$CloudProject,
    [string]$Region = 'us-central1',
    [string]$FirestoreDatabase = 'agent-mailbox',
    [switch]$Execute,
    [switch]$DeleteFirestoreData,
    [switch]$DeleteSecrets,
    [switch]$DeleteRuntimeServiceAccount
)

$ErrorActionPreference = 'Stop'
$service = 'agent-mailbox'
$secretNames = @('agent-mailbox-admin-key', 'agent-mailbox-join-key')
$runtimeAccount = "agent-mailbox-runtime@$CloudProject.iam.gserviceaccount.com"

function Invoke-Gcloud([string[]]$CommandArgs) {
    & gcloud @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "gcloud failed: gcloud $($CommandArgs -join ' ')" }
}

Write-Host "Agent Mailbox GCP inventory: project=$CloudProject region=$Region firestore=$FirestoreDatabase"
Write-Host "This script never touches unrelated services, databases, secrets or service accounts."

$serviceExists = $true
try { Invoke-Gcloud @('run','services','describe',$service,"--project=$CloudProject","--region=$Region") | Out-Null }
catch { $serviceExists = $false; Write-Host 'Cloud Run service not found.' }

$databaseExists = $true
try { Invoke-Gcloud @('firestore','databases','describe',"--database=$FirestoreDatabase","--project=$CloudProject") | Out-Null }
catch { $databaseExists = $false; Write-Host 'Firestore database not found.' }

$secretExists = @()
foreach ($name in $secretNames) {
    try { Invoke-Gcloud @('secrets','describe',$name,"--project=$CloudProject") | Out-Null; $secretExists += $name }
    catch { Write-Host "Secret not found: $name" }
}

$accountExists = $true
try { Invoke-Gcloud @('iam','service-accounts','describe',$runtimeAccount,"--project=$CloudProject") | Out-Null }
catch { $accountExists = $false; Write-Host 'Runtime service account not found.' }

Write-Host "`nTargets found: Cloud Run=$serviceExists Firestore=$databaseExists Secrets=$($secretExists.Count) RuntimeServiceAccount=$accountExists"
if (-not $Execute) {
    Write-Host 'Dry run only. Re-run with -Execute after reviewing the inventory.'
    exit 0
}

if ($DeleteFirestoreData -and -not $databaseExists) { throw 'Cannot delete Firestore data because the database was not found.' }
if ($DeleteFirestoreData) {
    $ack = Read-Host "Type DELETE AGENT MAILBOX DATA to disable protection and delete Firestore database '$FirestoreDatabase'"
    if ($ack -cne 'DELETE AGENT MAILBOX DATA') { throw 'Firestore deletion cancelled.' }
    Invoke-Gcloud @('firestore','databases','update',"--database=$FirestoreDatabase","--project=$CloudProject",'--delete-protection=DISABLED')
    Invoke-Gcloud @('firestore','databases','delete',"--database=$FirestoreDatabase","--project=$CloudProject",'--quiet')
}

if ($serviceExists) {
    Write-Host "Deleting Cloud Run service '$service'..."
    Invoke-Gcloud @('run','services','delete',$service,"--project=$CloudProject","--region=$Region",'--quiet')
}

if ($DeleteSecrets) {
    foreach ($name in $secretExists) {
        Write-Host "Deleting Secret Manager secret '$name'..."
        Invoke-Gcloud @('secrets','delete',$name,"--project=$CloudProject",'--quiet')
    }
}

if ($DeleteRuntimeServiceAccount -and $accountExists) {
    Write-Host "Deleting runtime service account '$runtimeAccount'..."
    Invoke-Gcloud @('iam','service-accounts','delete',$runtimeAccount,"--project=$CloudProject",'--quiet')
}

Write-Host 'Agent Mailbox GCP decommission completed for the selected targets.'

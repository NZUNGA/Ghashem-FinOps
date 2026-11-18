# =============================================================================
# Ghashem FinOps -- One-time: store the SSO app credential in the company vault
# PowerShell version
# =============================================================================
# Run this ONCE, from PowerShell, logged into your Azure account.
#
# Usage:
#   .\00-setup-sso-secret.ps1
# =============================================================================

# ── Configuration ─────────────────────────────────────────────────────────────
# Change these two values to deploy your own instance.
$AppName    = "ghashem"          # short identifier, lowercase, no spaces
$Env        = "dev"              # dev | staging | prod
$Location   = "canadacentral"   # Azure region
# ──────────────────────────────────────────────────────────────────────────────

$CompanyKV   = "kv-$AppName-company"
$SecretName  = "ghashem-sso-app-secret"
$ResourceGrp = "rg-$AppName-$Env"

Write-Host ""
Write-Host "=== Storing the Ghashem SSO app credential in $CompanyKV ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Find these two values at:"
Write-Host "  portal.azure.com -> Azure Active Directory -> App registrations"
Write-Host "  -> [your Ghashem app] -> Overview (Client ID)"
Write-Host "  -> Certificates and secrets (Client secret - shown once at creation)"
Write-Host ""

$ClientId = Read-Host "App registration Client ID"
$ClientSecretSecure = Read-Host "App registration Client Secret" -AsSecureString
$ClientSecret = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ClientSecretSecure)
)

if ([string]::IsNullOrWhiteSpace($ClientId) -or [string]::IsNullOrWhiteSpace($ClientSecret)) {
    Write-Host "Both values are required. Aborting." -ForegroundColor Red
    exit 1
}

$Combined = "${ClientId}:${ClientSecret}"
$SubscriptionId = az account show --query id -o tsv

# ── Step 1: Create the vault, checking the REAL outcome via exit code ───────
# Previous version trusted a try/catch around az, which never actually
# throws on a non-zero exit code the way PowerShell cmdlets do -- it just
# returns. That silently masked real failures as "already exists, skipping"
# even when nothing had succeeded at all.

Write-Host "-> Checking if $CompanyKV already exists..."
$existing = az keyvault show --name $CompanyKV --output json 2>$null | ConvertFrom-Json

if ($existing) {
    Write-Host "  Already exists in resource group: $($existing.resourceGroup)" -ForegroundColor Green
    $ActualRG = $existing.resourceGroup
} else {
    Write-Host "-> Creating $CompanyKV in $ResourceGrp..."

    $createOutput = az keyvault create `
        --name $CompanyKV `
        --resource-group $ResourceGrp `
        --location $Location `
        --output json 2>&1
    $createExitCode = $LASTEXITCODE

    if ($createExitCode -ne 0) {
        Write-Host ""
        Write-Host "  FAILED to create $CompanyKV (exit code $createExitCode)" -ForegroundColor Red
        Write-Host ($createOutput | Out-String) -ForegroundColor Red
        Write-Host "  Aborting -- fix the error above and re-run this script." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  Created successfully" -ForegroundColor Green
    $ActualRG = $ResourceGrp
}

$VaultScope = "/subscriptions/$SubscriptionId/resourceGroups/$ActualRG/providers/Microsoft.KeyVault/vaults/$CompanyKV"

# ── Step 2: Grant yourself access to write the secret ────────────────────────
# New Key Vaults default to RBAC authorization (not the older access-policy
# model), so the signed-in user needs an explicit role assignment before
# they can call secret set -- this is what produced the earlier
# ForbiddenByRbac error when this step was skipped.

$vaultDetails = az keyvault show --name $CompanyKV --output json | ConvertFrom-Json
$IsRbac = $vaultDetails.properties.enableRbacAuthorization

if ($IsRbac) {
    Write-Host "-> Vault uses RBAC authorization -- granting yourself Key Vault Secrets Officer..."
    $MyObjectId = az ad signed-in-user show --query id -o tsv

    az role assignment create `
        --role "Key Vault Secrets Officer" `
        --assignee $MyObjectId `
        --scope $VaultScope `
        --output none 2>$null

    Write-Host "  Role assigned (or already present). Waiting 20s for propagation..."
    Start-Sleep -Seconds 20
}

# ── Step 3: Store the secret, verifying the REAL outcome ─────────────────────

Write-Host "-> Storing secret..."
$setOutput = az keyvault secret set `
    --vault-name $CompanyKV `
    --name $SecretName `
    --value $Combined `
    --output json 2>&1
$setExitCode = $LASTEXITCODE

if ($setExitCode -ne 0) {
    Write-Host ""
    Write-Host "  FAILED to store secret (exit code $setExitCode)" -ForegroundColor Red
    Write-Host ($setOutput | Out-String) -ForegroundColor Red
    Write-Host "  If this is a permissions error, the RBAC role assignment above" -ForegroundColor Yellow
    Write-Host "  may need more time to propagate. Wait a minute and re-run this script." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Stored successfully -- verified, not assumed." -ForegroundColor Green
Write-Host "Vault: $CompanyKV (resource group: $ActualRG)"
Write-Host ""
Write-Host "Next: run .\01-provision.ps1 -- it grants the App Service's"
Write-Host "Managed Identity read access to this vault automatically (Phase 2b)."

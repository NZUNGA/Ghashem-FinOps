# =============================================================================
# Ghashem FinOps -- Deploy to rg-$AppName-$Env (PowerShell version)
# =============================================================================
# Run this from your project root in PowerShell:
#   .\01-provision.ps1
#
# Prerequisites:
#   - az login (logged into your Azure account)
#   - All files replaced per previous instructions (settings.py, urls.py, etc.)
#   - requirements.txt updated
#   - 00-setup-sso-secret.ps1 already run once (creates kv-$AppName-company secret)
# =============================================================================

# ── Configuration ─────────────────────────────────────────────────────────────
# Change these two values to deploy your own instance.
$AppName    = "ghashem"          # short identifier, lowercase, no spaces
$Env        = "dev"              # dev | staging | prod
$Location   = "canadacentral"   # Azure region
# ──────────────────────────────────────────────────────────────────────────────

# Note: deliberately NOT set to "Stop" globally. The az CLI writes routine
# informational WARNING: lines to stderr as part of normal operation (e.g.
# "Checking the existence of the resource group..."), and with
# ErrorActionPreference=Stop, PowerShell treats ANY stderr output from a
# native command as a terminating exception -- regardless of exit code.
# Invoke-AzSafe below judges success/failure by $LASTEXITCODE instead,
# which is the only reliable signal for native commands.
$ErrorActionPreference = "Continue"

$RG        = "rg-$AppName-$Env"
$APP       = "$AppName-app-$Env"
$Plan      = "$AppName-plan-$Env"
$DBServer  = "$AppName-db-$Env"
$DBName    = $AppName
$DBUser    = "$($AppName)admin"   # derived default -- override this line if your org requires a different Postgres admin username
$KV        = "kv-$AppName-$Env"
$CompanyKV = "kv-$AppName-company"

# Needed to build full ARM resource scope strings for RBAC role assignments
# (kv-$AppName-company uses Azure RBAC authorization, not the older access
# policy model -- az keyvault set-policy silently fails against it).
$SubscriptionId = az account show --query id -o tsv

function Invoke-AzSafe {
    # Runs an az command and judges success/failure purely by $LASTEXITCODE,
    # never by the mere presence of stderr text -- az routinely writes
    # harmless WARNING: status lines to stderr during normal operation.
    #
    # -ErrorAction SilentlyContinue on the invocation itself is required
    # even with $ErrorActionPreference = "Continue" at the script level --
    # PowerShell can still raise a NativeCommandError from stderr content
    # depending on how the command is invoked. Scoping ErrorAction directly
    # on this call is the only fully reliable way to prevent that.
    #
    # Only suppresses errors whose actual error text indicates the resource
    # already exists (the idempotent-rerun case). Every other non-zero exit
    # is shown in full and stops the script.
    param([string]$Description, [scriptblock]$Command)
    Write-Host "-> $Description..."

    $output = $null
    try {
        $output = & $Command 2>&1 -ErrorAction SilentlyContinue
    } catch {
        # Belt-and-suspenders: PowerShell's handling of native-command stderr
        # is inconsistent across versions, and a NativeCommandError can still
        # surface as a terminating exception despite SilentlyContinue above.
        # If it does, fall through to exit-code-based judgment instead of
        # letting the exception crash the script.
        $output = $_.Exception.Message
    }
    $exitCode = $LASTEXITCODE
    $outputText = $output | Out-String

    if ($exitCode -eq 0) {
        return
    }

    # Phrases Azure uses across different resource types to mean
    # "this already exists, nothing to do" -- expand this list as new
    # phrasings are discovered rather than guessing them all up front.
    $alreadyExistsPatterns = @(
        "already exists",
        "AlreadyExists",
        "Conflict",
        "is already registered",
        "already used",
        "NameAlreadyExists"
    )
    $isAlreadyExists = $alreadyExistsPatterns | Where-Object { $outputText -match [regex]::Escape($_) }

    if ($isAlreadyExists) {
        Write-Host "  (already exists - continuing)"
        return
    }

    Write-Host ""
    Write-Host "  FAILED: $Description (exit code $exitCode)" -ForegroundColor Red
    Write-Host $outputText -ForegroundColor Red
    Write-Host "  Script stopped. Fix the error above and re-run -- already" -ForegroundColor Yellow
    Write-Host "  completed steps will be skipped automatically." -ForegroundColor Yellow
    exit 1
}

# ── Phase 1: Provision infrastructure ────────────────────────────────────────

Write-Host ""
Write-Host "=== Phase 1: Infrastructure ===" -ForegroundColor Cyan

Write-Host "-> Creating resource group..."
az group create --name $RG --location $Location --output none

Invoke-AzSafe "Creating Postgres (B1ms)" {
    az postgres flexible-server create `
        --resource-group $RG `
        --name $DBServer `
        --location $Location `
        --admin-user $DBUser `
        --admin-password "CHANGE-THIS-DEV-PASSWORD" `
        --sku-name Standard_B1ms `
        --tier Burstable `
        --storage-size 32 `
        --version 16 `
        --output none
}

Invoke-AzSafe "Creating database" {
    az postgres flexible-server db create `
        --resource-group $RG `
        --server-name $DBServer `
        --name $DBName `
        --output none
}

Invoke-AzSafe "Opening Postgres firewall for App Service" {
    az postgres flexible-server firewall-rule create `
        --resource-group $RG `
        --server-name $DBServer `
        --name AllowAzureServices `
        --start-ip-address 0.0.0.0 `
        --end-ip-address 0.0.0.0 `
        --output none
}

Invoke-AzSafe "Creating App Service plan (F1 free)" {
    az appservice plan create `
        --name $Plan `
        --resource-group $RG `
        --is-linux `
        --sku F1 `
        --output none
}

Invoke-AzSafe "Creating App Service (Python 3.12)" {
    az webapp create `
        --name $APP `
        --resource-group $RG `
        --plan $Plan `
        --runtime "PYTHON:3.12" `
        --output none
}

Invoke-AzSafe "Creating Key Vault" {
    az keyvault create `
        --name $KV `
        --resource-group $RG `
        --location $Location `
        --output none
}

# ── Phase 2: Identity and Key Vault access ───────────────────────────────────

Write-Host ""
Write-Host "=== Phase 2: Managed Identity ===" -ForegroundColor Cyan

Write-Host "-> Enabling Managed Identity on App Service..."
az webapp identity assign --name $APP --resource-group $RG --output none

$PrincipalId = az webapp identity show --name $APP --resource-group $RG --query principalId -o tsv
Write-Host "  Principal ID: $PrincipalId"

Write-Host "-> Granting App Service access to Key Vault..."
az keyvault set-policy `
    --name $KV `
    --object-id $PrincipalId `
    --secret-permissions get list `
    --output none

# ── Phase 2b: Grant access to the COMPANY Key Vault for SSO credentials ─────
# settings.py reads ghashem-sso-app-secret from kv-$AppName-company at startup.
# Separate from kv-$AppName-$Env, which holds per-client data -- the SSO app
# credential is company-wide, not client-specific.
#
# kv-$AppName-company uses Azure's newer RBAC authorization model rather than
# the older vault access-policy model -- az keyvault set-policy is silently
# ineffective against an RBAC-mode vault (it returns success but grants
# nothing, which is exactly what caused a hard-to-diagnose container
# startup timeout previously). This checks which model the vault actually
# uses and grants access the correct way for either case.

Write-Host "-> Granting App Service access to company Key Vault (SSO credential)..."

$CompanyKVExists = $false
try {
    $vaultInfo = az keyvault show --name $CompanyKV --output json 2>$null | ConvertFrom-Json
    if ($vaultInfo) { $CompanyKVExists = $true }
} catch {
    $CompanyKVExists = $false
}

if (-not $CompanyKVExists) {
    Write-Host "  WARNING: $CompanyKV not found. Run .\00-setup-sso-secret.ps1 first." -ForegroundColor Yellow
} else {
    $CompanyKVRG = $vaultInfo.resourceGroup
    $IsRbac = $vaultInfo.properties.enableRbacAuthorization
    $CompanyKVScope = "/subscriptions/$SubscriptionId/resourceGroups/$CompanyKVRG/providers/Microsoft.KeyVault/vaults/$CompanyKV"

    if ($IsRbac) {
        Write-Host "  $CompanyKV uses RBAC authorization -- assigning Key Vault Secrets User role..."
        try {
            az role assignment create `
                --role "Key Vault Secrets User" `
                --assignee $PrincipalId `
                --scope $CompanyKVScope `
                --output none 2>$null
            Write-Host "  Granted (or already assigned)" -ForegroundColor Green
        } catch {
            Write-Host "  WARNING: RBAC role assignment failed -- verify manually" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  $CompanyKV uses access-policy authorization -- using set-policy..."
        try {
            az keyvault set-policy `
                --name $CompanyKV `
                --object-id $PrincipalId `
                --secret-permissions get list `
                --output none 2>$null
            Write-Host "  Granted" -ForegroundColor Green
        } catch {
            Write-Host "  WARNING: set-policy failed -- verify manually" -ForegroundColor Yellow
        }
    }
}

# ── Phase 3: Copy secrets from kv-$AppName-secrets to kv-$AppName-$Env ──────────
# Reads every client slug from config/clients.yaml dynamically -- no
# hardcoded slug list to maintain or forget when a new client is onboarded.

Write-Host ""
Write-Host "=== Phase 3: Copy secrets to dev Key Vault ===" -ForegroundColor Cyan

$ClientsYaml = "config/clients.yaml"

if (-not (Test-Path $ClientsYaml)) {
    Write-Host "  ERROR: $ClientsYaml not found - run this script from the project root" -ForegroundColor Red
    exit 1
}

# Extract every "slug: xxx" value under the clients: list.
# Uses a simple regex match -- no PyYAML dependency required, since
# clients.yaml's slug lines follow a consistent "  - slug: foo" format.
$ClientSlugs = Select-String -Path $ClientsYaml -Pattern '^\s*-\s*slug:\s*(.+)$' |
    ForEach-Object { $_.Matches[0].Groups[1].Value.Trim('"', "'", ' ') }

if (-not $ClientSlugs) {
    Write-Host "  ERROR: No client slugs found in $ClientsYaml - nothing to copy" -ForegroundColor Red
} else {
    Write-Host "  Found clients: $($ClientSlugs -join ', ')"
    Write-Host "  Copying from kv-$AppName-secrets -> $KV"
    Write-Host ""

    # The four secret types every client needs, per the onboarding runbook.
    $SecretSuffixes = @("tenant-id", "subscription-id", "secret", "access-group-id")

    foreach ($Slug in $ClientSlugs) {
        Write-Host "  -> $Slug"
        foreach ($Suffix in $SecretSuffixes) {
            $SecretName = "client-$Slug-$Suffix"
            $Value = ""
            try {
                $Value = az keyvault secret show `
                    --vault-name "kv-$AppName-secrets" `
                    --name $SecretName `
                    --query value -o tsv 2>$null
            } catch {
                $Value = ""
            }

            if ($Value) {
                az keyvault secret set `
                    --vault-name $KV `
                    --name $SecretName `
                    --value $Value `
                    --output none
                Write-Host "      OK: $SecretName"
            } else {
                # access-group-id is optional (SSO group check is skipped
                # without it, per sso_pipeline.py's fail-closed fallback).
                if ($Suffix -eq "access-group-id") {
                    Write-Host "      SKIP: $SecretName not set (SSO group check skipped for this client)" -ForegroundColor DarkGray
                } else {
                    Write-Host "      MISSING: $SecretName NOT FOUND - required, pipeline will fail without it" -ForegroundColor Red
                }
            }
        }
    }
}

# ── Phase 4: App Service environment variables ───────────────────────────────

Write-Host ""
Write-Host "=== Phase 4: App Service configuration ===" -ForegroundColor Cyan

$DBHost = "$DBServer.postgres.database.azure.com"
$SecretKeyBytes = New-Object byte[] 50
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($SecretKeyBytes)
$SecretKey = ($SecretKeyBytes | ForEach-Object { $_.ToString("x2") }) -join ""

az webapp config appsettings set `
    --name $APP `
    --resource-group $RG `
    --output none `
    --settings `
    DJANGO_SECRET_KEY=$SecretKey `
    DJANGO_ALLOWED_HOSTS="$APP.azurewebsites.net,localhost" `
    GHASHEM_ENVIRONMENT="development" `
    DJANGO_DEBUG="True" `
    DB_HOST=$DBHost `
    DB_NAME=$DBName `
    DB_USER=$DBUser `
    DB_PASSWORD="CHANGE-THIS-DEV-PASSWORD" `
    VAULT_URL="https://$KV.vault.azure.net/" `
    COMPANY_VAULT_URL="https://$CompanyKV.vault.azure.net/"

Write-Host "  Environment variables set" -ForegroundColor Green
Write-Host "  WARNING: Update DB_PASSWORD manually" -ForegroundColor Yellow
Write-Host "  No app client secret pasted here -- settings.py reads it from"
Write-Host "  $CompanyKV's ghashem-sso-app-secret automatically (see Phase 2b)."

# ── Phase 5: Startup command ──────────────────────────────────────────────────

Write-Host ""
Write-Host "=== Phase 5: Startup command ===" -ForegroundColor Cyan

az webapp config set `
    --name $APP `
    --resource-group $RG `
    --startup-file "gunicorn --bind=0.0.0.0 --timeout 600 ghashem.wsgi" `
    --output none
Write-Host "  Startup command set" -ForegroundColor Green

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Infrastructure ready. Next: deploy code (Phase 6)"
Write-Host " Run: .\02-deploy-code.ps1"
Write-Host "===================================================================" -ForegroundColor Cyan

# === SC-500 Security Hardening ===

Write-Host ""
Write-Host "=== SC-500 Security Hardening ===" -ForegroundColor Cyan

# --- PART 1: Log Analytics Workspace ---
$LawName = "law-$AppName-$Env"
az monitor log-analytics workspace create `
    --resource-group $RG `
    --workspace-name $LawName `
    --location $Location `
    --output none
Write-Host "✓ Log Analytics Workspace created" -ForegroundColor Green

$lawId = az monitor log-analytics workspace show `
    --resource-group $RG `
    --workspace-name $LawName `
    --query id -o tsv

# --- PART 2: Key Vault diagnostic logging (both vaults) ---
$kvDevId = az keyvault show `
    --name $KV `
    --query id -o tsv

az monitor diagnostic-settings create `
    --name "kv-$AppName-$Env-audit" `
    --resource $kvDevId `
    --workspace $lawId `
    --logs '[{"category":"AuditEvent","enabled":true}]' `
    --output none
Write-Host "✓ Key Vault diagnostic logging: $KV" -ForegroundColor Green

$kvCompanyId = az keyvault show `
    --name $CompanyKV `
    --query id -o tsv

az monitor diagnostic-settings create `
    --name "kv-$AppName-company-audit" `
    --resource $kvCompanyId `
    --workspace $lawId `
    --logs '[{"category":"AuditEvent","enabled":true}]' `
    --output none
Write-Host "✓ Key Vault diagnostic logging: $CompanyKV" -ForegroundColor Green

# --- PART 3: Defender for Cloud free tier ---
az security pricing create `
    --name CloudPosture `
    --tier Free `
    --output none
Write-Host "✓ Defender for Cloud free tier enabled" -ForegroundColor Green

# --- PART 4: Defender for App Service ---
az security pricing create `
    --name AppServices `
    --tier Standard `
    --output none
Write-Host "✓ Defender for App Service: Standard" -ForegroundColor Green

# --- PART 5: Defender for Databases ---
az security pricing create `
    --name OpenSourceRelationalDatabases `
    --tier Standard `
    --output none
Write-Host "✓ Defender for Databases: Standard" -ForegroundColor Green

# --- PART 6: Key Vault spike alert ---
$ActionGroup = "ag-$AppName-security"
az monitor action-group create `
    --resource-group $RG `
    --name $ActionGroup `
    --short-name ghsec `
    --action email admin support@ghashem.ca `
    --output none

az monitor metrics alert create `
    --name "kv-access-spike" `
    --resource-group $RG `
    --scopes $kvDevId `
    --condition "count ServiceApiHit > 200" `
    --window-size 5m `
    --evaluation-frequency 5m `
    --action $ActionGroup `
    --description "Key Vault accessed >200 times in 5 min" `
    --output none
Write-Host "✓ Key Vault spike alert configured" -ForegroundColor Green

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Security hardening complete."
Write-Host "===================================================================" -ForegroundColor Cyan

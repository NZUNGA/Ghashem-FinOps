#!/usr/bin/env bash
# =============================================================================
# Ghashem FinOps — Deploy to rg-ghashem-dev
# =============================================================================
# Run this from your project root:
#   bash deploy-dev.sh
#
# Prerequisites:
#   - az login (logged into your Azure account)
#   - All files replaced per previous instructions (settings.py, urls.py, etc.)
#   - requirements.txt updated (included in this package)
# =============================================================================

set -euo pipefail

RG="rg-ghashem-dev"
APP="ghashem-app-dev"
DB_SERVER="ghashem-db-dev"
DB_NAME="ghashem"
DB_USER="ghashemadmin"
KV="kv-ghashem-dev"
LOCATION="canadacentral"

# ── Phase 1: Provision infrastructure (skip if already exists) ────────────────

echo ""
echo "=== Phase 1: Infrastructure ==="

echo "→ Creating resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

echo "→ Creating Postgres (B1ms)..."
az postgres flexible-server create \
  --resource-group "$RG" \
  --name "$DB_SERVER" \
  --location "$LOCATION" \
  --admin-user "$DB_USER" \
  --admin-password "CHANGE-THIS-DEV-PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 16 \
  --output none 2>/dev/null || echo "  (already exists — skipping)"

az postgres flexible-server db create \
  --resource-group "$RG" \
  --server-name "$DB_SERVER" \
  --database-name "$DB_NAME" \
  --output none 2>/dev/null || echo "  (db already exists — skipping)"

echo "→ Opening Postgres firewall for App Service..."
az postgres flexible-server firewall-rule create \
  --resource-group "$RG" \
  --name "$DB_SERVER" \
  --rule-name AllowAzureServices \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 0.0.0.0 \
  --output none 2>/dev/null || echo "  (already exists — skipping)"

echo "→ Creating App Service plan (F1 free)..."
az appservice plan create \
  --name ghashem-plan-dev \
  --resource-group "$RG" \
  --is-linux \
  --sku F1 \
  --output none 2>/dev/null || echo "  (already exists — skipping)"

echo "→ Creating App Service (Python 3.12)..."
az webapp create \
  --name "$APP" \
  --resource-group "$RG" \
  --plan ghashem-plan-dev \
  --runtime "PYTHON:3.12" \
  --output none 2>/dev/null || echo "  (already exists — skipping)"

echo "→ Creating Key Vault..."
az keyvault create \
  --name "$KV" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --output none 2>/dev/null || echo "  (already exists — skipping)"

# ── Phase 2: Identity and Key Vault access ─────────────────────────────────────

echo ""
echo "=== Phase 2: Managed Identity ==="

echo "→ Enabling Managed Identity on App Service..."
az webapp identity assign \
  --name "$APP" \
  --resource-group "$RG" \
  --output none

PRINCIPAL_ID=$(az webapp identity show \
  --name "$APP" \
  --resource-group "$RG" \
  --query principalId -o tsv)
echo "  Principal ID: $PRINCIPAL_ID"

echo "→ Granting App Service access to Key Vault..."
az keyvault set-policy \
  --name "$KV" \
  --object-id "$PRINCIPAL_ID" \
  --secret-permissions get list \
  --output none

# ── Phase 2b: Grant access to the COMPANY Key Vault for SSO credentials ──────
# settings.py reads ghashem-sso-app-secret from kv-ghashem-company at startup.
# This is separate from kv-ghashem-dev/kv-ghashem-secrets, which hold
# per-client data — the SSO app credential is company-wide, not client-specific.

echo "→ Granting App Service access to company Key Vault (SSO credential)..."
az keyvault set-policy \
  --name kv-ghashem-company \
  --object-id "$PRINCIPAL_ID" \
  --secret-permissions get list \
  --output none 2>/dev/null || echo "  ⚠ kv-ghashem-company not found or already granted — verify manually"

# ── Phase 3: Copy secrets from kv-ghashem-secrets to kv-ghashem-dev ──────────
# Reads every client slug from config/clients.yaml so this works correctly
# regardless of how many tenants are configured — no hardcoded slug list to
# maintain or forget to update when a new client is onboarded.

echo ""
echo "=== Phase 3: Copy secrets to dev Key Vault ==="

CLIENTS_YAML="config/clients.yaml"

if [ ! -f "$CLIENTS_YAML" ]; then
  echo "  ✗ $CLIENTS_YAML not found — run this script from the project root"
  exit 1
fi

# Extract every "slug: xxx" value under the clients: list.
# Works without requiring PyYAML or yq to be installed — just grep + sed,
# since clients.yaml's slug lines follow a consistent "  - slug: foo" format.
CLIENT_SLUGS=$(grep -E '^\s*-\s*slug:\s*' "$CLIENTS_YAML" \
  | sed -E 's/^\s*-\s*slug:\s*//' \
  | tr -d '"' | tr -d "'")

if [ -z "$CLIENT_SLUGS" ]; then
  echo "  ✗ No client slugs found in $CLIENTS_YAML — nothing to copy"
else
  echo "  Found clients: $(echo "$CLIENT_SLUGS" | tr '\n' ' ')"
  echo "  Copying from kv-ghashem-secrets → $KV"
  echo ""

  # The four secret types every client needs, per the onboarding runbook.
  SECRET_SUFFIXES=("tenant-id" "subscription-id" "secret" "access-group-id")

  for SLUG in $CLIENT_SLUGS; do
    echo "  → $SLUG"
    for SUFFIX in "${SECRET_SUFFIXES[@]}"; do
      SECRET_NAME="client-${SLUG}-${SUFFIX}"
      VALUE=$(az keyvault secret show \
        --vault-name kv-ghashem-secrets \
        --name "$SECRET_NAME" \
        --query value -o tsv 2>/dev/null || echo "")
      if [ -n "$VALUE" ]; then
        az keyvault secret set \
          --vault-name "$KV" \
          --name "$SECRET_NAME" \
          --value "$VALUE" \
          --output none
        echo "      ✓ $SECRET_NAME"
      else
        # access-group-id is optional (SSO group check is skipped without it,
        # per sso_pipeline.py's fail-closed fallback) — the other three are required.
        if [ "$SUFFIX" = "access-group-id" ]; then
          echo "      ○ $SECRET_NAME not set (SSO group check will be skipped for this client)"
        else
          echo "      ✗ $SECRET_NAME NOT FOUND — required, pipeline will fail without it"
        fi
      fi
    done
  done
fi

# ── Phase 4: App Service environment variables ────────────────────────────────

echo ""
echo "=== Phase 4: App Service configuration ==="

DB_HOST="${DB_SERVER}.postgres.database.azure.com"
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(50))' 2>/dev/null || \
             python -c 'import secrets; print(secrets.token_hex(50))')

az webapp config appsettings set \
  --name "$APP" \
  --resource-group "$RG" \
  --output none \
  --settings \
    DJANGO_SECRET_KEY="$SECRET_KEY" \
    DJANGO_ALLOWED_HOSTS="${APP}.azurewebsites.net,localhost" \
    GHASHEM_ENVIRONMENT="development" \
    DJANGO_DEBUG="True" \
    DB_HOST="$DB_HOST" \
    DB_NAME="$DB_NAME" \
    DB_USER="$DB_USER" \
    DB_PASSWORD="CHANGE-THIS-DEV-PASSWORD" \
    VAULT_URL="https://${KV}.vault.azure.net/" \
    COMPANY_VAULT_URL="https://kv-ghashem-company.vault.azure.net/"

echo "  ✓ Environment variables set"
echo "  ⚠ Update DB_PASSWORD manually"
echo "  ⚠ No app client secret pasted here — settings.py now reads it from"
echo "    kv-ghashem-company's ghashem-sso-app-secret automatically. Make sure"
echo "    the App Service's Managed Identity has get/list on that vault too —"
echo "    see Phase 2b below."

# ── Phase 5: Startup command ──────────────────────────────────────────────────

echo ""
echo "=== Phase 5: Startup command ==="

az webapp config set \
  --name "$APP" \
  --resource-group "$RG" \
  --startup-file "gunicorn --bind=0.0.0.0 --timeout 600 ghashem.wsgi" \
  --output none
echo "  ✓ Startup command set"

echo ""
echo "==================================================================="
echo " Infrastructure ready. Next: deploy code (Phase 6)"
echo " Run: bash deploy-code.sh"
echo "==================================================================="

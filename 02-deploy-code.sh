#!/usr/bin/env bash
# =============================================================================
# Ghashem FinOps — Deploy code to rg-ghashem-dev
# =============================================================================
# Run from your project root AFTER 01-provision.sh completes:
#   bash deploy-code.sh
# =============================================================================

set -euo pipefail

RG="rg-ghashem-dev"
APP="ghashem-app-dev"
ZIP="ghashem-deploy.zip"

echo ""
echo "=== Deploying code to $APP ==="

# Collect static files
echo "→ Collecting static files..."
python manage.py collectstatic --noinput

# Create deployment zip — exclude things that shouldn't go to Azure
echo "→ Creating deployment zip..."
if command -v zip &>/dev/null; then
  zip -r "$ZIP" . \
    --exclude "*.pyc" \
    --exclude "__pycache__/*" \
    --exclude ".venv/*" \
    --exclude ".git/*" \
    --exclude "db.sqlite3" \
    --exclude "*.zip" \
    --exclude ".env" \
    --exclude ".vscode/*" \
    --exclude "debug_*.py"
else
  # Windows fallback using PowerShell
  echo "  Using PowerShell zip..."
  powershell -Command "
    \$exclude = @('*.pyc','__pycache__','.venv','.git','db.sqlite3','*.zip','.env','.vscode','debug_*.py')
    \$items = Get-ChildItem -Path . -Exclude \$exclude
    Compress-Archive -Path \$items -DestinationPath '$ZIP' -Force
  "
fi
echo "  ✓ Created $ZIP"

# Deploy
echo "→ Deploying to App Service (this takes 2-3 minutes)..."
az webapp deploy \
  --name "$APP" \
  --resource-group "$RG" \
  --src-path "$ZIP" \
  --type zip

echo "  ✓ Code deployed"

# Clean up zip
rm -f "$ZIP"

echo ""
echo "==================================================================="
echo " Code deployed. Next: run migrations (Phase 7)"
echo " App URL: https://${APP}.azurewebsites.net"
echo "==================================================================="

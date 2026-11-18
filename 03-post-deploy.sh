#!/usr/bin/env bash
# =============================================================================
# Ghashem FinOps — Post-deploy setup (run INSIDE App Service SSH console)
# =============================================================================
# Open the SSH console in the Azure portal:
#   App Service → Development Tools → SSH
# OR via CLI:
#   az webapp ssh --name ${APP_NAME}-app-${ENV} --resource-group rg-${APP_NAME}-${ENV}
#
# Then paste these commands one section at a time.
# =============================================================================

# ── Configuration ────────────────────────────────────────────────────────────
# Change these two values to deploy your own instance.
APP_NAME="ghashem"
ENV="dev"
LOCATION="canadacentral"
# ──────────────────────────────────────────────────────────────────────────────

cd /home/site/wwwroot

# ── Migrations ────────────────────────────────────────────────────────────────
echo "→ Running migrations..."
python manage.py migrate
python manage.py migrate social_django

# ── Create superuser (your admin account) ────────────────────────────────────
echo "→ Creating superuser..."
python manage.py createsuperuser
# Enter your username, email, and password when prompted

# ── Grant yourself admin access to demo-client ────────────────────────────────
echo "→ Granting admin access..."
# Replace YOUR_USERNAME with the username you just created
python manage.py grant_access YOUR_USERNAME demo-client --role admin

# ── Run first pipeline ────────────────────────────────────────────────────────
echo "→ Running first pipeline (takes several minutes)..."
python manage.py run_pipeline --client demo-client

echo ""
echo "==================================================================="
echo " Setup complete."
echo " Open: https://${APP_NAME}-app-${ENV}.azurewebsites.net"
echo "==================================================================="

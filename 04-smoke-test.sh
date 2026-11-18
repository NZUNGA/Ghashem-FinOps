#!/usr/bin/env bash
# =============================================================================
# Ghashem FinOps — Smoke test checklist for rg-ghashem-dev
# =============================================================================
# Run from your local machine after 03-post-deploy.sh completes.
# =============================================================================

APP_URL="https://ghashem-app-dev.azurewebsites.net"

echo ""
echo "=== Smoke test: $APP_URL ==="
echo ""
echo "Run through this checklist manually in your browser:"
echo ""
echo "[ ] 1. Login page loads at $APP_URL/login/"
echo "[ ] 2. 'Sign in with Microsoft 365' button visible"
echo "[ ] 3. Username/password login works (your superuser)"
echo "[ ] 4. DEV environment — GHASHEM_ENVIRONMENT=development is set"
echo "       (no banner visible, but check App Service config)"
echo "[ ] 5. Overview page shows pipeline data for demo-client"
echo "[ ] 6. Findings page shows open findings"
echo "[ ] 7. Resources page shows resources including zombie resources"
echo "[ ] 8. Run analysis button triggers pipeline successfully"
echo "[ ] 9. SSO login works with work M365 account from client tenant"
echo "[ ] 10. Tenant isolation: create a test user with no access,"
echo "        confirm they see 'No clients available' and get 404"
echo "        on direct URL: $APP_URL/client/demo-client/"
echo ""
echo "=== Quick API checks ==="

echo "→ Checking login page..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$APP_URL/login/")
if [ "$STATUS" = "200" ]; then
  echo "  ✓ Login page: $STATUS"
else
  echo "  ✗ Login page: $STATUS (expected 200)"
fi

echo "→ Checking direct client URL (should 302 to login, not 404)..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$APP_URL/client/demo-client/")
if [ "$STATUS" = "302" ]; then
  echo "  ✓ Client redirect: $STATUS (redirects to login — correct)"
else
  echo "  ! Client URL: $STATUS"
fi

echo ""
echo "If all checks pass, rg-ghashem-dev is ready."
echo "When ready for production, repeat with rg-ghashem-prod and ghashem.ca domain."

# =============================================================================
# Ghashem FinOps -- Smoke test checklist for rg-$AppName-$Env (PowerShell version)
# =============================================================================
# Run from your local machine after 03-post-deploy completes.
# =============================================================================

# ── Configuration ─────────────────────────────────────────────────────────────
# Change these two values to deploy your own instance.
$AppName    = "ghashem"          # short identifier, lowercase, no spaces
$Env        = "dev"              # dev | staging | prod
$Location   = "canadacentral"   # Azure region
# ──────────────────────────────────────────────────────────────────────────────

$AppUrl = "https://$AppName-app-$Env.azurewebsites.net"

Write-Host ""
Write-Host "=== Smoke test: $AppUrl ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Run through this checklist manually in your browser:"
Write-Host ""
Write-Host "[ ] 1. Login page loads at $AppUrl/login/"
Write-Host "[ ] 2. 'Sign in with Microsoft 365' button visible"
Write-Host "[ ] 3. Username/password login works (your superuser)"
Write-Host "[ ] 4. DEV environment -- GHASHEM_ENVIRONMENT=development is set"
Write-Host "[ ] 5. Overview page shows pipeline data for each client"
Write-Host "[ ] 6. Findings page shows open findings"
Write-Host "[ ] 7. Resources page shows resources including zombie resources"
Write-Host "[ ] 8. Run analysis button triggers pipeline successfully"
Write-Host "[ ] 9. SSO login works with a work M365 account from each client tenant"
Write-Host "[ ] 10. Tenant isolation: a test user with no access for client A"
Write-Host "        sees 'No clients available' and gets 404 on client A's URL,"
Write-Host "        even if they have access to client B"
Write-Host ""
Write-Host "=== Quick API checks ===" -ForegroundColor Cyan

Write-Host "-> Checking login page..."
try {
    $resp = Invoke-WebRequest -Uri "$AppUrl/login/" -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -eq 200) {
        Write-Host "  OK: Login page returned 200" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: Login page returned $($resp.StatusCode) (expected 200)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ERROR: Could not reach login page - $($_.Exception.Message)" -ForegroundColor Red
}

# Check tenant isolation for every client found in clients.yaml, not just demo-client.
$ClientsYaml = "config/clients.yaml"
if (Test-Path $ClientsYaml) {
    $ClientSlugs = Select-String -Path $ClientsYaml -Pattern '^\s*-\s*slug:\s*(.+)$' |
        ForEach-Object { $_.Matches[0].Groups[1].Value.Trim('"', "'", ' ') }

    foreach ($Slug in $ClientSlugs) {
        Write-Host "-> Checking direct client URL for '$Slug' (should redirect to login, not 404)..."
        try {
            $resp = Invoke-WebRequest -Uri "$AppUrl/client/$Slug/" -UseBasicParsing `
                -MaximumRedirection 0 -ErrorAction SilentlyContinue
        } catch {
            $resp = $_.Exception.Response
        }
        $code = if ($resp.StatusCode) { [int]$resp.StatusCode } else { 0 }
        if ($code -eq 302) {
            Write-Host "  OK: $Slug redirects to login (302)" -ForegroundColor Green
        } else {
            Write-Host "  CHECK MANUALLY: $Slug returned $code" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  config/clients.yaml not found - skipping per-client checks" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "If all checks pass, rg-$AppName-$Env is ready."
Write-Host "When ready for production, repeat with rg-$AppName-prod and ghashem.ca domain."

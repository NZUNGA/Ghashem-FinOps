# =============================================================================
# Ghashem FinOps -- Deploy code to rg-$AppName-$Env (PowerShell version)
# =============================================================================
# Run from your project root AFTER 01-provision.ps1 completes:
#   .\02-deploy-code.ps1
# =============================================================================

# ── Configuration ─────────────────────────────────────────────────────────────
# Change these two values to deploy your own instance.
$AppName    = "ghashem"          # short identifier, lowercase, no spaces
$Env        = "dev"              # dev | staging | prod
$Location   = "canadacentral"   # Azure region
# ──────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$RG  = "rg-$AppName-$Env"
$APP = "$AppName-app-$Env"
$Zip = "$AppName-deploy.zip"

Write-Host ""
Write-Host "=== Deploying code to $APP ===" -ForegroundColor Cyan

Write-Host "-> Collecting static files..."
python manage.py collectstatic --noinput

Write-Host "-> Cleaning local caches before zipping..."
Get-ChildItem -Path . -Recurse -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path . -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "-> Checking dependencies for known CVEs..."
if (Get-Command pip-audit -ErrorAction SilentlyContinue) {
    pip-audit -r requirements.txt --progress-spinner off
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pip-audit found vulnerabilities — review before deploying to production."
    }
}
else { Write-Warning "pip-audit not installed locally (pip install pip-audit) — skipping CVE check." }

Write-Host "-> Creating deployment zip..."
if (Test-Path $Zip) { Remove-Item $Zip -Force }

# Top-level AND nested directories/files to exclude. Compress-Archive only
# filters top-level items, which let .idea/, nested __pycache__, etc. slip
# through previously -- this version walks the full tree.
$ExcludeDirNames  = @(".venv", ".git", "__pycache__", ".vscode", ".idea")
$ExcludeFileNames = @("db.sqlite3", ".env")
$ExcludeFilePatterns = @("debug_*.py", "*.pyc", "*.zip")

function Test-ShouldExclude {
    param([System.IO.FileSystemInfo]$Item, [string]$RelativePath)

    # Exclude if any path segment matches an excluded directory name
    $segments = $RelativePath -split '[\\/]'
    foreach ($seg in $segments) {
        if ($ExcludeDirNames -contains $seg) { return $true }
    }

    if (-not $Item.PSIsContainer) {
        if ($ExcludeFileNames -contains $Item.Name) { return $true }
        foreach ($pattern in $ExcludeFilePatterns) {
            if ($Item.Name -like $pattern) { return $true }
        }
    }

    return $false
}

# Build the file list, walking the full tree and excluding by relative path
# -- this is the part Compress-Archive's flat top-level filtering missed.
$RootPath = (Get-Location).Path
$AllFiles = Get-ChildItem -Path . -Recurse -File -Force -ErrorAction SilentlyContinue

$FilesToZip = @()
foreach ($file in $AllFiles) {
    $relativePath = $file.FullName.Substring($RootPath.Length + 1)
    if (-not (Test-ShouldExclude -Item $file -RelativePath $relativePath)) {
        $FilesToZip += [PSCustomObject]@{
            FullPath     = $file.FullName
            RelativePath = $relativePath
        }
    }
}

Write-Host "  Including $($FilesToZip.Count) files"

# Build the zip directly with System.IO.Compression.ZipFile rather than
# Compress-Archive. This is the critical fix: Compress-Archive on Windows
# can store internal zip entry paths using backslashes, which Linux-based
# App Service's rsync-based deployment then fails to extract correctly
# ("Invalid argument (22)" on every nested file). ZipArchiveEntry lets us
# force forward slashes explicitly, which is what the zip format and Linux
# tooling both expect regardless of which OS created the archive.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zipStream = [System.IO.File]::Open($Zip, [System.IO.FileMode]::Create)
$archive = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)

try {
    foreach ($f in $FilesToZip) {
        # Force forward slashes in the entry name regardless of host OS
        $entryName = $f.RelativePath -replace '\\', '/'
        $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
        $entryStream = $entry.Open()
        $fileBytes = [System.IO.File]::ReadAllBytes($f.FullPath)
        $entryStream.Write($fileBytes, 0, $fileBytes.Length)
        $entryStream.Close()
    }
} finally {
    $archive.Dispose()
    $zipStream.Close()
}

Write-Host "  Created $Zip" -ForegroundColor Green

Write-Host "-> Deploying to App Service (this takes 2-3 minutes)..."
az webapp deploy `
    --name $APP `
    --resource-group $RG `
    --src-path $Zip `
    --type zip

Write-Host "  Code deployed" -ForegroundColor Green

Remove-Item $Zip -Force

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Code deployed. Next: run migrations (Phase 7)"
Write-Host " App URL: https://$APP.azurewebsites.net"
Write-Host "===================================================================" -ForegroundColor Cyan

# End-to-end local release script: build -> upload to VPS -> create GitHub Release.
#
# Usage (from repo root):
#   .\tools\release.ps1 -Version v0.8.4
#   .\tools\release.ps1 -Version v0.8.4 -SkipBuild   # if dist\ is already fresh
#
# Prerequisites:
#   * Python + project venv activated (requirements.txt installed)
#   * Inno Setup 6 + Tesseract installed
#   * VPS credentials in environment variables (or .env.local at repo root):
#       VPS_HOST, VPS_USER, VPS_SSH_KEY (path to private key file)
#       VPS_PUBLIC_URL
#   * gh CLI authenticated (gh auth login)

param(
    [Parameter(Mandatory)][string]$Version,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

# Load .env.local if present
$EnvFile = Join-Path $RepoRoot ".env.local"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | Where-Object { $_ -match "^\s*[^#]" } | ForEach-Object {
        $parts = $_ -split "=", 2
        if ($parts.Length -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
        }
    }
}

# Validate required env vars
foreach ($var in @("VPS_HOST", "VPS_USER", "VPS_SSH_KEY", "VPS_PUBLIC_URL")) {
    if (-not [System.Environment]::GetEnvironmentVariable($var)) {
        throw "Missing environment variable: $var. Set it or add it to .env.local"
    }
}

# Validate version format
if ($Version -notmatch "^v\d+\.\d+\.\d+") {
    throw "Version must match vX.Y.Z (got: $Version)"
}

Write-Host ""
Write-Host "==> SpaceDrive Release $Version" -ForegroundColor Cyan
Write-Host ""

# Step 1: Build
if (-not $SkipBuild) {
    Write-Host "--- Step 1/3: Build installer ---" -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "build_installer.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
} else {
    Write-Host "--- Step 1/3: Build skipped (-SkipBuild) ---" -ForegroundColor DarkGray
}

# Locate the produced installer
$Installer = Get-ChildItem (Join-Path $RepoRoot "dist") -Filter "SpaceDrive-Setup-*.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) { throw "No installer found in dist\ - run without -SkipBuild" }
$SizeMB = [math]::Round($Installer.Length / 1MB, 1)
Write-Host "==> Installer: $($Installer.Name) ($SizeMB MB)"

# Step 2: Upload to VPS
Write-Host ""
Write-Host "--- Step 2/3: Upload to VPS ---" -ForegroundColor Yellow

# If VPS_SSH_KEY is a file path, read its content
$KeyValue = [System.Environment]::GetEnvironmentVariable("VPS_SSH_KEY")
if (Test-Path $KeyValue) {
    $KeyContent = Get-Content $KeyValue -Raw
    [System.Environment]::SetEnvironmentVariable("VPS_SSH_KEY", $KeyContent, "Process")
}

& python (Join-Path $PSScriptRoot "upload_vps.py") $Installer.FullName
if ($LASTEXITCODE -ne 0) { throw "VPS upload failed" }

# Step 3: Git tag + GitHub Release
Write-Host ""
Write-Host "--- Step 3/3: Git tag + GitHub Release ---" -ForegroundColor Yellow

# Create tag locally if missing
if (-not (git tag --list $Version)) {
    git tag $Version
    Write-Host "==> Tag $Version created locally"
}

# Push tag if missing on remote
$remoteTag = git ls-remote --tags origin "refs/tags/$Version"
if (-not $remoteTag) {
    git push origin $Version
    if ($LASTEXITCODE -ne 0) { throw "Failed to push tag $Version" }
    Write-Host "==> Tag $Version pushed to origin"
} else {
    Write-Host "==> Tag $Version already on origin"
}

# Create GitHub Release
$PublicUrl = [System.Environment]::GetEnvironmentVariable("VPS_PUBLIC_URL").TrimEnd("/")
$DownloadUrl = "$PublicUrl/$($Installer.Name)"
$ReleaseNotes = "## Download`n`n**[$($Installer.Name)]($DownloadUrl)**`n`nHosted on VPS."

gh release create $Version `
    --title "SpaceDrive GPS $Version" `
    --generate-notes `
    --notes $ReleaseNotes
if ($LASTEXITCODE -ne 0) { throw "Failed to create GitHub Release for $Version" }

Write-Host ""
Write-Host "==> Release $Version published!" -ForegroundColor Green
Write-Host "    Download: $DownloadUrl"

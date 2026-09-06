# Prints the SHA-256 of the Tesseract installer currently pinned in
# installer\spaceDrive.iss, so TesseractSha256 can be refreshed after bumping
# TesseractVersion.
#
# Usage:
#   .\tools\get_tesseract_hash.ps1            # hash the pinned version
#   .\tools\get_tesseract_hash.ps1 -Update    # hash it and patch the .iss
#
# The download goes to the temp dir and is deleted afterwards; nothing is
# installed and nothing is executed.

[CmdletBinding()]
param(
    [switch]$Update
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$IssFile = Join-Path $RepoRoot "installer\spaceDrive.iss"

if (-not (Test-Path $IssFile)) {
    throw "Cannot find $IssFile"
}

# Read as UTF-8 explicitly (see the note in release.ps1 about Get-Content
# defaulting to the ANSI codepage on PowerShell 5.1).
$IssContent = Get-Content $IssFile -Raw -Encoding UTF8

$VersionMatch = [regex]::Match($IssContent, '#define\s+TesseractVersion\s+"([^"]+)"')
if (-not $VersionMatch.Success) {
    throw "Could not find TesseractVersion in $IssFile"
}
$TessVersion = $VersionMatch.Groups[1].Value

$InstallerName = "tesseract-ocr-w64-setup-$TessVersion.exe"
$Url = "https://github.com/UB-Mannheim/tesseract/releases/download/v$TessVersion/$InstallerName"
$OutFile = Join-Path $env:TEMP $InstallerName

Write-Host "==> Pinned Tesseract version: $TessVersion"
Write-Host "==> Downloading $Url"

try {
    # TLS 1.2 is not the default on stock PowerShell 5.1 and GitHub requires it.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing

    $Size = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
    $Hash = (Get-FileHash -Path $OutFile -Algorithm SHA256).Hash.ToLower()

    Write-Host ""
    Write-Host "==> Downloaded $Size MB"
    Write-Host "==> SHA-256: $Hash" -ForegroundColor Green
    Write-Host ""

    if ($Update) {
        $Patched = [regex]::Replace(
            $IssContent,
            '#define\s+TesseractSha256\s+"[^"]*"',
            "#define TesseractSha256 `"$Hash`"")
        if ($Patched -eq $IssContent) {
            Write-Warning "TesseractSha256 not found or already up to date; .iss left untouched."
        } else {
            [System.IO.File]::WriteAllText($IssFile, $Patched, [System.Text.UTF8Encoding]::new($false))
            Write-Host "==> Patched TesseractSha256 in installer\spaceDrive.iss" -ForegroundColor Green
        }
    } else {
        Write-Host "Paste this into installer\spaceDrive.iss:"
        Write-Host "  #define TesseractSha256 `"$Hash`""
        Write-Host ""
        Write-Host "Or re-run with -Update to patch it automatically."
    }
} finally {
    if (Test-Path $OutFile) {
        Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
    }
}

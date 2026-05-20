# Builds SpaceDrive end-to-end: PyInstaller bundle then Inno Setup installer.
#
# Output: dist\SpaceDrive-Setup-vX.Y.Z.exe (Inno Setup names it from the
# AppVersion declared in installer\spaceDrive.iss).
#
# Prerequisites on the host:
#   * Python 3.10+ with the project's requirements installed in the active venv.
#   * PyInstaller >= 6 (pip install pyinstaller).
#   * Inno Setup 6 installed; `iscc` resolvable on PATH OR the default
#     install location at C:\Program Files (x86)\Inno Setup 6\ISCC.exe.
#     Download: https://jrsoftware.org/isdl.php
#
# Usage from the repo root:
#   .\tools\build_installer.ps1
#   .\tools\build_installer.ps1 -SkipPyInstaller  # if dist\spaceDrive\ is fresh
#   .\tools\build_installer.ps1 -Clean            # wipe dist\ + build\ first

param(
    [switch]$SkipPyInstaller,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

Write-Host "==> Repo root: $RepoRoot" -ForegroundColor Cyan

if ($Clean) {
    foreach ($dir in @("dist", "build")) {
        $path = Join-Path $RepoRoot $dir
        if (Test-Path $path) {
            Write-Host "==> Cleaning $path"
            Remove-Item -Recurse -Force $path
        }
    }
}

if (-not $SkipPyInstaller) {
    Write-Host "==> Running PyInstaller (one-folder bundle)" -ForegroundColor Cyan
    & python -m PyInstaller --clean --noconfirm spaceDrive.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
}

$BundleDir = Join-Path $RepoRoot "dist\spaceDrive"
$BundleExe = Join-Path $BundleDir "spaceDrive.exe"
if (-not (Test-Path $BundleExe)) {
    throw "Expected $BundleExe to exist after PyInstaller — did the build succeed?"
}
Write-Host "==> Bundle OK: $BundleExe"

# Locate Inno Setup compiler.
$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $Iscc)) {
    throw "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php"
}
Write-Host "==> Inno Setup compiler: $Iscc"

Write-Host "==> Compiling installer" -ForegroundColor Cyan
& $Iscc (Join-Path $RepoRoot "installer\spaceDrive.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed (exit $LASTEXITCODE)" }

$Installer = Get-ChildItem (Join-Path $RepoRoot "dist") -Filter "SpaceDrive-Setup-*.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) {
    throw "Installer .exe not produced — check the Inno Setup log above"
}
Write-Host ""
Write-Host "==> Installer ready: $($Installer.FullName)" -ForegroundColor Green
Write-Host "    Size: $([math]::Round($Installer.Length / 1MB, 1)) MB"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  - Test in Windows Sandbox: double-click installer\test_in_sandbox.wsb"
Write-Host "  - Or install locally:      `"$($Installer.FullName)`""

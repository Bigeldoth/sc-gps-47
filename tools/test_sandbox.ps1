# Builds the installer, stages it for Windows Sandbox, and launches a fully
# automated install verification inside the VM.
#
# The sandbox run needs no elevation: SpaceDrive is installed with /CURRENTUSER
# into %LOCALAPPDATA%\Programs\SpaceDrive. Admin-only checks are reported SKIP.
#
# Prerequisites on the host:
#   * Python 3.10+ with the project requirements, PyInstaller >= 6
#   * Inno Setup 6
#   * Windows Sandbox feature enabled (Win 10/11 Pro/Enterprise)
#
# Usage from the repo root:
#   .\tools\test_sandbox.ps1                  # full: build both installers + run
#   .\tools\test_sandbox.ps1 -SkipPyInstaller # reuse dist\spaceDrive\
#   .\tools\test_sandbox.ps1 -SkipBuild       # reuse the installers already staged
#   .\tools\test_sandbox.ps1 -SkipBadHash     # skip the tampered-hash negative test
#   .\tools\test_sandbox.ps1 -StageOnly       # stage everything, do not launch
#   .\tools\test_sandbox.ps1 -Interactive     # show the wizard (see below)
#
# Inside the sandbox the verification runs on its own and drops
# SpaceDrive-sandbox-report.txt (plus a logs\ folder) on the Desktop.
#
# -Interactive matters for one specific reason: the Tesseract download lives in
# NextButtonClick(wpReady), and a silent install never calls that. So the
# default silent run cannot exercise the download or its SHA-256 guard, and
# reports T3 as FAILED with that explanation. Use -Interactive (click through
# the wizard inside the VM) to actually test the download path.

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipPyInstaller,
    [switch]$SkipBadHash,
    [switch]$StageOnly,
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$StageDir = Join-Path $RepoRoot "dist\sandbox-input"
$BadDir   = Join-Path $StageDir "bad-hash"
$WsbFile  = Join-Path $RepoRoot "dist\test_in_sandbox.wsb"
$IssFile  = Join-Path $RepoRoot "installer\spaceDrive.iss"

# A syntactically valid SHA-256 that cannot match anything real. Used to prove
# the installer's integrity guard actually blocks execution.
$WrongHash = "0" * 64

Write-Host "==> Repo root: $RepoRoot" -ForegroundColor Cyan

# --- Preflight ------------------------------------------------------------
if (-not (Get-Command "WindowsSandbox.exe" -ErrorAction SilentlyContinue) -and
    -not (Test-Path "$env:WINDIR\System32\WindowsSandbox.exe")) {
    Write-Warning "WindowsSandbox.exe not found. Enable the 'Windows Sandbox' Windows feature (Pro/Enterprise only)."
}

$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) { $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $Iscc)) {
    throw "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php"
}

# --- Build ----------------------------------------------------------------
if ($SkipBuild) {
    Write-Host "==> Skipping build, reusing what is already staged" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "--- Step 1/3: Build the genuine installer ---" -ForegroundColor Yellow
    $buildArgs = @()
    if ($SkipPyInstaller) { $buildArgs += "-SkipPyInstaller" }
    & "$PSScriptRoot\build_installer.ps1" @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "build_installer.ps1 failed (exit $LASTEXITCODE)" }

    if (Test-Path $StageDir) { Remove-Item $StageDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

    $Installer = Get-ChildItem (Join-Path $RepoRoot "dist") -Filter "SpaceDrive-Setup-*.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $Installer) { throw "No SpaceDrive-Setup-*.exe produced in dist\" }
    Copy-Item $Installer.FullName $StageDir
    Write-Host "==> Staged $($Installer.Name) ($([math]::Round($Installer.Length/1MB,1)) MB)" -ForegroundColor Green

    if ($SkipBadHash) {
        Write-Host ""
        Write-Host "--- Step 2/3: Tampered-hash build SKIPPED ---" -ForegroundColor Gray
        Write-Host "    The negative test (guard rejects a bad hash) will report SKIP."
    } else {
        Write-Host ""
        Write-Host "--- Step 2/3: Build the tampered-hash installer ---" -ForegroundColor Yellow
        Write-Host "    Same bundle, TesseractSha256 forced to an impossible value, so the"
        Write-Host "    sandbox can assert that the integrity guard refuses to run it."
        Write-Host "    (Recompresses the bundle - takes about as long as step 1.)"
        New-Item -ItemType Directory -Force -Path $BadDir | Out-Null
        & $Iscc $IssFile "/DTesseractSha256=$WrongHash" "/O$BadDir"
        if ($LASTEXITCODE -ne 0) { throw "Tampered-hash compile failed (exit $LASTEXITCODE)" }
        $BadInstaller = Get-ChildItem $BadDir -Filter "SpaceDrive-Setup-*.exe" |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $BadInstaller) { throw "Tampered-hash installer not produced in $BadDir" }
        Write-Host "==> Staged bad-hash\$($BadInstaller.Name)" -ForegroundColor Green
    }
}

if (-not (Test-Path $StageDir)) {
    throw "$StageDir does not exist. Run without -SkipBuild first."
}

# The verification script travels with the payload so the VM can reach it
# through the read-only mount.
Copy-Item (Join-Path $RepoRoot "installer\sandbox_verify.ps1") $StageDir -Force
Write-Host "==> Staged sandbox_verify.ps1"

# --- Generate the .wsb ----------------------------------------------------
# Written into dist\ (gitignored) because it embeds an absolute host path.
$VerifyArgs = if ($Interactive) { " -Interactive" } else { "" }
$LogonCmd = "powershell.exe -ExecutionPolicy Bypass -NoProfile -NoExit -File C:\sandbox-input\sandbox_verify.ps1$VerifyArgs"
$Wsb = @"
<Configuration>
  <!--
    GENERATED by tools\test_sandbox.ps1 - do not edit by hand, and do not
    commit it: HostFolder below is an absolute path on this machine.

    Mounts the staged installers read-only as C:\sandbox-input and runs
    sandbox_verify.ps1 automatically at logon. The script installs with
    /CURRENTUSER so it never needs elevation, verifies the result, then
    writes SpaceDrive-sandbox-report.txt to the Desktop.

    Close the sandbox window to discard everything.
  -->
  <Networking>Enable</Networking>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$StageDir</HostFolder>
      <SandboxFolder>C:\sandbox-input</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>$LogonCmd</Command>
  </LogonCommand>
  <MemoryInMB>6144</MemoryInMB>
</Configuration>
"@
# .wsb must be plain UTF-8 without BOM; the sandbox parser rejects a BOM.
[System.IO.File]::WriteAllText($WsbFile, $Wsb, [System.Text.UTF8Encoding]::new($false))
Write-Host "==> Generated $WsbFile"

Write-Host ""
Write-Host "--- Staged contents ---" -ForegroundColor Yellow
Get-ChildItem $StageDir -Recurse -File |
    Select-Object @{n = "File"; e = { $_.FullName.Substring($StageDir.Length + 1) } },
                  @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } |
    Format-Table -AutoSize | Out-String | Write-Host

if ($StageOnly) {
    Write-Host "==> -StageOnly: not launching. Double-click $WsbFile when ready." -ForegroundColor Cyan
    return
}

# --- Launch ---------------------------------------------------------------
Write-Host "--- Step 3/3: Launch Windows Sandbox ---" -ForegroundColor Yellow
Invoke-Item $WsbFile

Write-Host ""
if ($Interactive) {
    Write-Host "Sandbox starting in INTERACTIVE mode:" -ForegroundColor Green
    Write-Host "  1. the wizard opens - click through it (installs with /CURRENTUSER)"
    Write-Host "  2. checks the payload, the Tesseract download and its SHA-256"
    Write-Host "  3. the tampered-hash wizard opens - it SHOULD fail with an integrity error"
    Write-Host "  4. launches the app, then uninstalls and checks user data survives"
} else {
    Write-Host "Sandbox starting. Inside the VM, everything runs on its own:" -ForegroundColor Green
    Write-Host "  1. silent install with /CURRENTUSER (no elevation)"
    Write-Host "  2. checks the payload landed and the app runs"
    Write-Host "  3. uninstalls and checks user data survives"
    Write-Host ""
    Write-Host "  T3/T4 (download + SHA-256 guard) will report FAIL/SKIP: a silent" -ForegroundColor Yellow
    Write-Host "  install never calls NextButtonClick, so it cannot reach the download." -ForegroundColor Yellow
    Write-Host "  Re-run with -Interactive to test that path." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "A message box shows PASS/FAIL/SKIP counts when it finishes." -ForegroundColor Green
Write-Host "Full report: Desktop\SpaceDrive-sandbox-report.txt (logs\ alongside it)."
Write-Host ""
Write-Host "Note: the sandbox mount is read-only, so nothing can write back to" -ForegroundColor Gray
Write-Host "      the host. Close the window to discard the VM." -ForegroundColor Gray

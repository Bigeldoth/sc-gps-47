# Automated end-to-end install verification, executed INSIDE Windows Sandbox
# by the LogonCommand in test_in_sandbox.wsb.
#
# Designed to run WITHOUT elevation: SpaceDrive is installed with /CURRENTUSER
# into %LOCALAPPDATA%\Programs\SpaceDrive. Checks that genuinely require admin
# are reported SKIP (with the reason) instead of FAIL, so a non-elevated run
# still produces a meaningful verdict.
#
# Do not run this on a real machine: it installs, launches and uninstalls.
# It is meant for the throwaway sandbox VM only.
#
# Everything lands in %PUBLIC%\Desktop:
#   SpaceDrive-sandbox-report.txt   human-readable verdict
#   logs\                           Inno /LOG output, app log, transcript
#
# -Interactive shows the wizard instead of installing silently. That matters:
# the Tesseract download lives in NextButtonClick(wpReady), which a silent
# install never calls, so only an interactive run exercises the download and
# its SHA-256 guard. Click through the wizard and the checks resume on exit.

param([switch]$Interactive)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$InputDir  = "C:\sandbox-input"
$Desktop   = "$env:PUBLIC\Desktop"
$LogDir    = Join-Path $Desktop "logs"
$Report    = Join-Path $Desktop "SpaceDrive-sandbox-report.txt"
$AppDir    = Join-Path $env:LOCALAPPDATA "Programs\SpaceDrive"
$UserData  = Join-Path $env:LOCALAPPDATA "SpaceDrive"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Start-Transcript -Path (Join-Path $LogDir "transcript.txt") -Force | Out-Null

# --- result accumulator ---------------------------------------------------
$Results = New-Object System.Collections.ArrayList

function Add-Result {
    param(
        [string]$Id,
        [string]$Name,
        [ValidateSet("PASS", "FAIL", "SKIP", "INFO")][string]$Status,
        [string]$Detail = ""
    )
    [void]$Results.Add([pscustomobject]@{
        Id = $Id; Name = $Name; Status = $Status; Detail = $Detail
    })
    $color = switch ($Status) {
        "PASS" { "Green" } "FAIL" { "Red" } "SKIP" { "Yellow" } default { "Gray" }
    }
    Write-Host ("[{0}] {1,-6} {2}" -f $Id, $Status, $Name) -ForegroundColor $color
    if ($Detail) { Write-Host "         $Detail" -ForegroundColor DarkGray }
}

function Get-Installer {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) { return $null }
    Get-ChildItem (Join-Path $Dir "SpaceDrive-Setup-*.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

# =========================================================================
# T0 - Environment preflight
# =========================================================================
Write-Host ""
Write-Host "=== SpaceDrive sandbox verification ===" -ForegroundColor Cyan
Write-Host ""

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$IsElevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Add-Result "T0.1" "Running as $($env:USERNAME)" "INFO" `
    "Elevated: $IsElevated  |  OS build: $([System.Environment]::OSVersion.Version)"

if (-not $IsElevated) {
    Add-Result "T0.2" "Non-elevated run" "INFO" `
        "Using /CURRENTUSER; target is $AppDir. Admin-only checks will be SKIPped."
}

# Networking is required for the Tesseract download.
$net = Test-NetConnection -ComputerName "github.com" -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue `
       -ErrorAction SilentlyContinue
if ($null -eq $net) {
    # Test-NetConnection is not present on all builds; fall back to a raw socket.
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("github.com", 443)
        $net = $c.Connected
        $c.Close()
    } catch { $net = $false }
}
if ($net) {
    Add-Result "T0.3" "Network reachable (github.com:443)" "PASS"
} else {
    Add-Result "T0.3" "Network reachable (github.com:443)" "FAIL" `
        "No connectivity - the Tesseract download cannot be tested. Check <Networking>Enable</Networking> in the .wsb."
}

# A pre-existing Tesseract would make the download path unreachable.
$TessPaths = @("C:\Program Files\Tesseract-OCR\tesseract.exe",
               "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
$TessPreInstalled = @($TessPaths | Where-Object { Test-Path $_ }).Count -gt 0
Add-Result "T0.4" "Tesseract absent before install" $(if ($TessPreInstalled) { "FAIL" } else { "PASS" }) `
    $(if ($TessPreInstalled) { "Already present - the download path will be skipped by IsTesseractInstalled()." } else { "Clean slate, download path will be exercised." })

$Good = Get-Installer $InputDir
if (-not $Good) {
    Add-Result "T0.5" "Installer present in $InputDir" "FAIL" `
        "No SpaceDrive-Setup-*.exe found. Did tools\test_sandbox.ps1 build and stage it?"
    # Nothing further is testable.
    $Results | Format-Table -AutoSize | Out-String | Set-Content $Report -Encoding UTF8
    Stop-Transcript | Out-Null
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("No installer found in $InputDir.", "SpaceDrive sandbox - ERROR") | Out-Null
    exit 1
}
Add-Result "T0.5" "Installer present" "PASS" `
    "$($Good.Name)  ($([math]::Round($Good.Length/1MB,1)) MB)"

# Copy out of the read-only mount so Inno can write its own temp files freely.
$Work = "C:\sandbox-work"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$GoodLocal = Join-Path $Work $Good.Name
Copy-Item $Good.FullName $GoodLocal -Force
Unblock-File $GoodLocal -ErrorAction SilentlyContinue

# =========================================================================
# T1 - Silent install
# =========================================================================
Write-Host ""
$InnoLog = Join-Path $LogDir "inno-install.log"
if ($Interactive) {
    $args1 = @("/CURRENTUSER", "/NORESTART", "/LOG=$InnoLog")
    Write-Host "Running the wizard: $($Good.Name) $($args1 -join ' ')" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  >>> Click through the wizard in the window that just opened." -ForegroundColor Yellow
    Write-Host "  >>> Verification resumes automatically when it closes." -ForegroundColor Yellow
    Write-Host ""
    Add-Result "T1.0" "Mode" "INFO" "Interactive - the download path IS exercised."
} else {
    $args1 = @("/VERYSILENT", "/CURRENTUSER", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$InnoLog")
    Write-Host "Running: $($Good.Name) $($args1 -join ' ')" -ForegroundColor Cyan
    Add-Result "T1.0" "Mode" "INFO" "Silent - see T3.1 about the download path."
}

$sw = [Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath $GoodLocal -ArgumentList $args1 -PassThru -Wait
$sw.Stop()
$exit1 = $proc.ExitCode

Add-Result "T1.1" "Installer exit code 0" $(if ($exit1 -eq 0) { "PASS" } else { "FAIL" }) `
    "Exit code $exit1, took $([math]::Round($sw.Elapsed.TotalSeconds,1))s"

$InnoText = if (Test-Path $InnoLog) { Get-Content $InnoLog -Raw } else { "" }
Add-Result "T1.2" "Inno log produced" $(if ($InnoText) { "PASS" } else { "FAIL" }) `
    $(if ($InnoText) { "$InnoLog ($([math]::Round((Get-Item $InnoLog).Length/1KB,1)) KB)" } else { "No log written" })

# =========================================================================
# T2 - Payload landed
# =========================================================================
Write-Host ""
$Exe = Join-Path $AppDir "spaceDrive.exe"
Add-Result "T2.1" "spaceDrive.exe installed" $(if (Test-Path $Exe) { "PASS" } else { "FAIL" }) $Exe

if (Test-Path $AppDir) {
    $files = Get-ChildItem $AppDir -Recurse -File -ErrorAction SilentlyContinue
    $sizeMB = [math]::Round((($files | Measure-Object Length -Sum).Sum) / 1MB, 1)
    Add-Result "T2.2" "Bundle looks complete" $(if ($files.Count -gt 100 -and $sizeMB -gt 50) { "PASS" } else { "FAIL" }) `
        "$($files.Count) files, $sizeMB MB"

    Add-Result "T2.3" "_internal\ present (PyInstaller one-folder)" `
        $(if (Test-Path (Join-Path $AppDir "_internal")) { "PASS" } else { "FAIL" })

    if (Test-Path $Exe) {
        $vi = (Get-Item $Exe).VersionInfo
        Add-Result "T2.4" "Bundled exe version" "INFO" `
            "FileVersion=$($vi.FileVersion) ProductVersion=$($vi.ProductVersion)"
    }
} else {
    Add-Result "T2.2" "Bundle looks complete" "FAIL" "$AppDir does not exist"
}

# =========================================================================
# T3 - Tesseract download + SHA-256 verification
#      This is the path the security fix touches.
# =========================================================================
Write-Host ""
$sawDownload = $InnoText -match "Downloaded tesseract-ocr-w64-setup"
$sawMismatch = $InnoText -match "SHA-256 mismatch"
$sawRunning  = $InnoText -match "Running Tesseract installer"

if ($sawDownload) {
    Add-Result "T3.1" "Tesseract installer was downloaded" "PASS" "Found the download line in the Inno log."
    Add-Result "T3.2" "No SHA-256 mismatch on the genuine build" `
        $(if ($sawMismatch) { "FAIL" } else { "PASS" }) `
        $(if ($sawMismatch) { "Hash guard rejected the real asset - the pinned TesseractSha256 is wrong." } else { "Hash matched the pinned value." })
    Add-Result "T3.3" "Tesseract installer was executed" `
        $(if ($sawRunning) { "PASS" } else { "SKIP" }) `
        $(if ($sawRunning) { "Exec reached." } else { "Never reached Exec - see T3.4." })
} elseif ($Interactive) {
    Add-Result "T3.1" "Tesseract installer was downloaded" "FAIL" `
        "Interactive run, but no download line in the log. Either Tesseract was already present, or the download genuinely failed - check inno-install.log."
    Add-Result "T3.2" "SHA-256 guard exercised" "SKIP" "Download never ran, nothing to verify."
    Add-Result "T3.3" "Tesseract installer was executed" "SKIP" "Download never ran."
} else {
    # Silent mode never fires NextButtonClick, so the wizard-page download is
    # skipped entirely. Surface the cause rather than a vague failure.
    Add-Result "T3.1" "Tesseract installer was downloaded" "FAIL" `
        "No download line in the log. The download lives in NextButtonClick(wpReady), which a silent install never calls - so /VERYSILENT installs SpaceDrive with no OCR engine at all. Re-run with -Interactive to exercise this path."
    Add-Result "T3.2" "SHA-256 guard exercised" "SKIP" "Download never ran, nothing to verify."
    Add-Result "T3.3" "Tesseract installer was executed" "SKIP" "Download never ran."
}

if ($IsElevated) {
    $tessNow = @($TessPaths | Where-Object { Test-Path $_ }).Count -gt 0
    Add-Result "T3.4" "tesseract.exe on disk after install" $(if ($tessNow) { "PASS" } else { "FAIL" }) `
        $(if ($tessNow) { "Found under Program Files." } else { "Not installed." })
} else {
    Add-Result "T3.4" "tesseract.exe on disk after install" "SKIP" `
        "The UB-Mannheim installer writes to Program Files and needs admin; cannot succeed in a non-elevated run."
}

# =========================================================================
# T4 - Negative test: tampered hash must be rejected
# =========================================================================
Write-Host ""
$Bad = Get-Installer (Join-Path $InputDir "bad-hash")
if (-not $Bad) {
    Add-Result "T4.1" "Tampered-hash build rejected" "SKIP" `
        "No bad-hash installer staged. Re-run the host script without -SkipBadHash to build it."
} elseif (-not $sawDownload) {
    Add-Result "T4.1" "Tampered-hash build rejected" "SKIP" `
        "The genuine build never reached the download either (see T3.1), so this test cannot distinguish a rejection from a skip."
} else {
    $BadLocal = Join-Path $Work $Bad.Name
    Copy-Item $Bad.FullName $BadLocal -Force
    Unblock-File $BadLocal -ErrorAction SilentlyContinue

    $BadLog = Join-Path $LogDir "inno-badhash.log"
    $BadDir = Join-Path $env:LOCALAPPDATA "Programs\SpaceDrive-BadHash"
    # Must match the mode of the genuine run: only the wizard path reaches the
    # download, so a silent run here could never reject anything.
    if ($Interactive) {
        $argsBad = @("/CURRENTUSER", "/NORESTART", "/DIR=$BadDir", "/LOG=$BadLog")
        Write-Host ""
        Write-Host "  >>> Tampered-hash build: click through the wizard." -ForegroundColor Yellow
        Write-Host "  >>> It SHOULD fail with an integrity error - that is the expected result." -ForegroundColor Yellow
        Write-Host ""
    } else {
        $argsBad = @("/VERYSILENT", "/CURRENTUSER", "/SUPPRESSMSGBOXES", "/NORESTART",
                     "/DIR=$BadDir", "/LOG=$BadLog")
    }
    Write-Host "Running tampered-hash build (expected to reject)..." -ForegroundColor Cyan
    $pBad = Start-Process -FilePath $BadLocal -ArgumentList $argsBad -PassThru -Wait
    $badText = if (Test-Path $BadLog) { Get-Content $BadLog -Raw } else { "" }

    $rejected = ($badText -match "SHA-256 mismatch") -or ($badText -match "refusing to run") -or ($pBad.ExitCode -ne 0)
    $ranAnyway = $badText -match "Running Tesseract installer"

    Add-Result "T4.1" "Tampered-hash build rejected" $(if ($rejected) { "PASS" } else { "FAIL" }) `
        "Exit code $($pBad.ExitCode); mismatch logged: $([bool]($badText -match 'SHA-256 mismatch'))"
    Add-Result "T4.2" "Tampered build did NOT execute the binary" $(if ($ranAnyway) { "FAIL" } else { "PASS" }) `
        $(if ($ranAnyway) { "'Running Tesseract installer' appears in the log - the guard was bypassed." } else { "Exec was never reached." })

    if (Test-Path $BadDir) { Remove-Item $BadDir -Recurse -Force -ErrorAction SilentlyContinue }
}

# =========================================================================
# T5 - Application launches
# =========================================================================
Write-Host ""
if (Test-Path $Exe) {
    $appLog = Join-Path $UserData "logs\spacedrive.log"
    if (Test-Path $appLog) { Remove-Item $appLog -Force -ErrorAction SilentlyContinue }

    $p = Start-Process -FilePath $Exe -PassThru -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 20
    $alive = $p -and -not $p.HasExited

    Add-Result "T5.1" "spaceDrive.exe still running after 20s" $(if ($alive) { "PASS" } else { "FAIL" }) `
        $(if ($alive) { "PID $($p.Id)" } else { "Exited with code $(if ($p) { $p.ExitCode } else { 'n/a' }) - a PyQt6 overlay may fail without a GPU; check the app log." })

    if (Test-Path $appLog) {
        Copy-Item $appLog (Join-Path $LogDir "spacedrive.log") -Force -ErrorAction SilentlyContinue
        $errs = @(Select-String -Path $appLog -Pattern "ERROR|CRITICAL|Traceback" -ErrorAction SilentlyContinue)
        Add-Result "T5.2" "App wrote its log to user_data_dir" "PASS" $appLog
        Add-Result "T5.3" "No ERROR/CRITICAL in app log" $(if ($errs.Count -eq 0) { "PASS" } else { "FAIL" }) `
            "$($errs.Count) matching lines; copy saved next to this report."
    } else {
        Add-Result "T5.2" "App wrote its log to user_data_dir" "FAIL" "Expected $appLog"
        Add-Result "T5.3" "No ERROR/CRITICAL in app log" "SKIP" "No log to inspect."
    }

    if ($alive) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Get-Process spaceDrive -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} else {
    Add-Result "T5.1" "spaceDrive.exe still running after 20s" "SKIP" "Nothing installed to launch."
}

# =========================================================================
# T6 - Uninstall, and user data survives it
# =========================================================================
Write-Host ""
$Uninst = Get-ChildItem $AppDir -Filter "unins*.exe" -ErrorAction SilentlyContinue |
          Select-Object -First 1
if ($Uninst) {
    # Seed a marker so we can prove [UninstallDelete] leaves user data alone.
    New-Item -ItemType Directory -Force -Path $UserData | Out-Null
    $marker = Join-Path $UserData "user_poi.json"
    if (-not (Test-Path $marker)) { '{"marker":"sandbox"}' | Set-Content $marker -Encoding UTF8 }

    $pu = Start-Process -FilePath $Uninst.FullName `
          -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -PassThru -Wait
    Start-Sleep -Seconds 3

    $gone = -not (Test-Path $Exe)
    Add-Result "T6.1" "Uninstall removed the app" $(if ($gone) { "PASS" } else { "FAIL" }) `
        "Uninstaller exit code $($pu.ExitCode)"
    Add-Result "T6.2" "User data preserved (documented behaviour)" `
        $(if (Test-Path $marker) { "PASS" } else { "FAIL" }) `
        "$UserData should survive uninstall - it holds user POIs and sidecar venvs."
} else {
    Add-Result "T6.1" "Uninstall removed the app" "SKIP" "No uninstaller found in $AppDir."
    Add-Result "T6.2" "User data preserved" "SKIP" "Uninstall not run."
}

# =========================================================================
# Report
# =========================================================================
$pass = @($Results | Where-Object Status -eq "PASS").Count
$fail = @($Results | Where-Object Status -eq "FAIL").Count
$skip = @($Results | Where-Object Status -eq "SKIP").Count

$lines = New-Object System.Collections.ArrayList
[void]$lines.Add("=========================================================")
[void]$lines.Add(" SpaceDrive GPS - sandbox install verification")
[void]$lines.Add(" $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
[void]$lines.Add(" Installer : $($Good.Name)")
[void]$lines.Add(" User      : $env:USERNAME (elevated: $IsElevated)")
[void]$lines.Add(" Target    : $AppDir")
[void]$lines.Add("=========================================================")
[void]$lines.Add("")
[void]$lines.Add(("  {0,-6} {1,-7} {2}" -f "ID", "STATUS", "CHECK"))
[void]$lines.Add("  " + ("-" * 70))
foreach ($r in $Results) {
    [void]$lines.Add(("  {0,-6} {1,-7} {2}" -f $r.Id, $r.Status, $r.Name))
    if ($r.Detail) { [void]$lines.Add("                 -> $($r.Detail)") }
}
[void]$lines.Add("")
[void]$lines.Add("  PASS $pass   FAIL $fail   SKIP $skip")
[void]$lines.Add("")
if ($fail -eq 0) {
    [void]$lines.Add("  VERDICT: no failures.")
} else {
    [void]$lines.Add("  VERDICT: $fail check(s) FAILED - see the detail lines above.")
}
[void]$lines.Add("")
[void]$lines.Add("  Logs: $LogDir")
[void]$lines.Add("    inno-install.log   full Inno Setup install log")
[void]$lines.Add("    inno-badhash.log   tampered-hash run (if staged)")
[void]$lines.Add("    spacedrive.log     app log, if the app produced one")
[void]$lines.Add("    transcript.txt     this script's console output")

$text = $lines -join "`r`n"
Set-Content -Path $Report -Value $text -Encoding UTF8
Write-Host ""
Write-Host $text

Stop-Transcript | Out-Null

# Put the verdict in front of the user without them hunting for a file.
Start-Process notepad.exe $Report -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.Windows.Forms
$icon = if ($fail -eq 0) { "Information" } else { "Error" }
[System.Windows.Forms.MessageBox]::Show(
    "PASS $pass    FAIL $fail    SKIP $skip`n`nFull report on the Desktop:`n$Report",
    "SpaceDrive sandbox verification",
    "OK", $icon) | Out-Null

; SpaceDrive GPS - Inno Setup installer
;
; Wraps the PyInstaller one-folder output (dist\spaceDrive\) into a single
; .exe installer. Downloads + installs Tesseract OCR (UB-Mannheim) silently
; if not already present. PaddleOCR is installed post-install via the in-app
; Engine Manager.
;
; Build:
;   1. python -m PyInstaller --clean spaceDrive.spec        (-> dist\spaceDrive\)
;   2. iscc installer\spaceDrive.iss                        (-> dist\SpaceDrive-Setup-vX.Y.Z.exe)
;
; Or run tools\build_installer.ps1 to chain both steps.

#define MyAppName "SpaceDrive GPS"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "Bigeldoth"
#define MyAppURL "https://github.com/Bigeldoth/sc-gps-47"
#define MyAppExeName "spaceDrive.exe"
#define MyAppId "{{A4DDDFC7-9F1A-46CD-9E60-1B3C9C0F2A0B}"

; Pinned Tesseract release - UB-Mannheim Windows build. Update as needed.
; Note: the GitHub release tag carries a leading "v" but the installer
; filename does not, so we keep both as separate constants.
#define TesseractVersion "5.4.0.20240606"
#define TesseractReleaseTag "v" + TesseractVersion
#define TesseractInstaller "tesseract-ocr-w64-setup-" + TesseractVersion + ".exe"
#define TesseractUrl "https://github.com/UB-Mannheim/tesseract/releases/download/" + TesseractReleaseTag + "/" + TesseractInstaller
; SHA-256 of the pinned installer above. The setup refuses to run the download
; if it does not match, so this MUST be updated whenever TesseractVersion is.
; Refresh it with: .\tools\get_tesseract_hash.ps1
#define TesseractSha256 "c885fff6998e0608ba4bb8ab51436e1c6775c2bafc2559a19b423e18678b60c9"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\SpaceDrive
DefaultGroupName=SpaceDrive
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=SpaceDrive-Setup-v{#MyAppVersion}
SetupIconFile=..\assets\spacedrive.ico
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Require admin so the app lands in C:\Program Files\SpaceDrive (the
; expected place for a real Windows app). PrivilegesRequiredOverridesAllowed
; still lets a non-admin user opt down to %LOCALAPPDATA%\Programs via
; /CURRENTUSER on the command line or by accepting the fallback dialog.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
LicenseFile=..\LICENSE.txt
; Download support
ExtraDiskSpaceRequired=104857600
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french";  MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller one-folder bundle (must be built first: python -m PyInstaller --clean spaceDrive.spec)
Source: "..\dist\spaceDrive\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Default config snapshot for first-run seeding (the live config lives in %LOCALAPPDATA%\SpaceDrive\)
Source: "..\config.ini"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
{
  Custom code:
  - Detects Tesseract pre-install
  - If absent, downloads UB-Mannheim's installer (~70 MB) and runs it silent
  - Creates %LOCALAPPDATA%\SpaceDrive\ for user data
}

var
  DownloadPage: TDownloadWizardPage;
  TesseractDetected: Boolean;

function IsTesseractInstalled(): Boolean;
var
  Path1, Path2: String;
begin
  Path1 := ExpandConstant('{commonpf}\Tesseract-OCR\tesseract.exe');
  Path2 := ExpandConstant('{commonpf32}\Tesseract-OCR\tesseract.exe');
  Result := FileExists(Path1) or FileExists(Path2);
end;

{ Case-insensitive SHA-256 comparison. GetSHA256OfFile returns lowercase hex,
  but the constant is hand-maintained so do not depend on its casing. Any
  failure to hash (unreadable file) is treated as a mismatch: fail closed. }
function HashMatches(const FileName, ExpectedSha256: String): Boolean;
var
  Actual: String;
begin
  Result := False;
  try
    Actual := GetSHA256OfFile(FileName);
  except
    Log('Failed to hash ' + FileName + ': ' + GetExceptionMessage);
    Exit;
  end;
  Result := CompareText(Actual, Trim(ExpectedSha256)) = 0;
  { Keep the argument array on this line: Inno treats any line starting with
    '[' as a section tag, even inside [Code]. }
  if not Result then
    Log('SHA-256 mismatch for ' + FileName + ' (expected ' + ExpectedSha256 + ', got ' + Actual + ')');
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if Progress = ProgressMax then
    Log(Format('Downloaded %s', [FileName]));
  Result := True;
end;

procedure InitializeWizard;
begin
  TesseractDetected := IsTesseractInstalled();
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then begin
    if not TesseractDetected then begin
      DownloadPage.Clear;
      // Third argument is the expected SHA-256: Inno verifies it after the
      // transfer and raises an exception on mismatch, which the except block
      // below turns into an error dialog and a blocked Next button.
      DownloadPage.Add('{#TesseractUrl}', '{#TesseractInstaller}', '{#TesseractSha256}');
      DownloadPage.Show;
      try
        try
          DownloadPage.Download;
          Result := True;
        except
          if DownloadPage.AbortedByUser then
            Log('Tesseract download aborted by user')
          else
            SuppressibleMsgBox(AddPeriod(GetExceptionMessage), mbCriticalError, MB_OK, IDOK);
          Result := False;
        end;
      finally
        DownloadPage.Hide;
      end;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  InstallerPath: String;
  UserDataPath: String;
begin
  if CurStep = ssPostInstall then begin
    // Run downloaded Tesseract installer silently.
    if not TesseractDetected then begin
      InstallerPath := ExpandConstant('{tmp}\{#TesseractInstaller}');
      if FileExists(InstallerPath) then begin
        // Re-verify immediately before handing the file to Exec. The download
        // page already checked the hash, but this keeps the guarantee local to
        // the call that actually runs a third-party binary elevated.
        if not HashMatches(InstallerPath, '{#TesseractSha256}') then begin
          Log('Tesseract installer SHA-256 mismatch - refusing to run ' + InstallerPath);
          SuppressibleMsgBox(
            'The downloaded Tesseract installer failed its integrity check and was not run.'
              + #13#10#13#10
              + 'SpaceDrive GPS is installed, but OCR will not work until Tesseract is '
              + 'installed manually from https://github.com/UB-Mannheim/tesseract',
            mbCriticalError, MB_OK, IDOK);
          DeleteFile(InstallerPath);
        end else begin
          Log('Running Tesseract installer: ' + InstallerPath);
          if Exec(InstallerPath, '/S', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
            Log(Format('Tesseract installer exit code: %d', [ResultCode]))
          else
            Log('Failed to launch Tesseract installer');
        end;
      end;
    end;

    // Ensure the user data dir exists. The app will populate it (sidecar
    // venvs, user POIs, logs) on first launch.
    UserDataPath := ExpandConstant('{localappdata}\SpaceDrive');
    if not DirExists(UserDataPath) then begin
      if CreateDir(UserDataPath) then
        Log('Created user data dir: ' + UserDataPath)
      else
        Log('Failed to create user data dir: ' + UserDataPath);
    end;
  end;
end;

[UninstallDelete]
; Leave %LOCALAPPDATA%\SpaceDrive\ alone by default - it contains user POIs
; and the sidecar venvs the user may want to reuse after a reinstall. Add a
; line here if you want to nuke it on uninstall.

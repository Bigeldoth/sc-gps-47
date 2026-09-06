# Local release only. Versions must already be synchronized and committed.
# Build -> validate provenance -> tag/draft -> atomically publish VPS -> publish draft.
# Usage: .\tools\release.ps1 -Version vX.Y.Z [-SkipBuild]
param(
    [Parameter(Mandatory)][string]$Version,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $RepoRoot

if ($Version -notmatch '^v\d+\.\d+\.\d+$') { throw 'Version must match vX.Y.Z exactly' }
& python tools/versioning.py release-preflight --version $Version
if ($LASTEXITCODE -ne 0) { throw 'Release preflight failed; no build or publication performed' }
$Commit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve source commit' }

# A reused tag may never silently describe another checkout or installer.
$RemoteTags = @(git ls-remote --tags origin "refs/tags/$Version" "refs/tags/$Version^{}")
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the remote tag' }
if ($RemoteTags.Count -gt 0) {
    $Peeled = @($RemoteTags | Where-Object { $_ -match '\^\{\}$' })
    $RemoteCommit = if ($Peeled.Count) { ($Peeled[0] -split '\s+')[0] } else { ($RemoteTags[0] -split '\s+')[0] }
    if ($RemoteCommit -ne $Commit) { throw "$Version already refers to another commit on origin" }
}

& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub authentication is required before publishing' }
$ExistingRelease = & gh release view $Version --json isDraft --jq '.isDraft' 2>$null
if ($LASTEXITCODE -eq 0 -and $ExistingRelease -eq 'false') {
    throw "$Version is already published; a published version cannot be overwritten"
}

$EnvFile = Join-Path $RepoRoot '.env.local'
if (Test-Path -LiteralPath $EnvFile) {
    Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^\s*[^#]' } | ForEach-Object {
        $Parts = $_ -split '=', 2
        if ($Parts.Length -eq 2) {
            [System.Environment]::SetEnvironmentVariable($Parts[0].Trim(), $Parts[1].Trim(), 'Process')
        }
    }
}
foreach ($Variable in @('VPS_HOST', 'VPS_USER', 'VPS_SSH_KEY', 'VPS_PUBLIC_URL')) {
    if (-not [System.Environment]::GetEnvironmentVariable($Variable)) {
        throw "Missing environment variable: $Variable"
    }
}

if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build_installer.ps1') -Version $Version
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
}
& python tools/versioning.py verify-build --version $Version --release
if ($LASTEXITCODE -ne 0) { throw 'Installer/bundle do not match this clean committed version; rebuild' }
& python tools/versioning.py release-preflight --version $Version
if ($LASTEXITCODE -ne 0) { throw 'Sources changed during the release preparation' }
$InstallerPath = Join-Path $RepoRoot "dist\SpaceDrive-Setup-$Version.exe"

if (-not (git tag --list $Version)) {
    git tag -a $Version $Commit -m "SpaceDrive GPS $Version"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create release tag' }
}
if ($RemoteTags.Count -eq 0) {
    git push origin "refs/tags/$Version"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot push release tag' }
}

$PublicUrl = [System.Environment]::GetEnvironmentVariable('VPS_PUBLIC_URL').TrimEnd('/')
$InstallerName = [System.IO.Path]::GetFileName($InstallerPath)
$DownloadUrl = "$PublicUrl/$InstallerName"
$NotesPath = Join-Path ([System.IO.Path]::GetTempPath()) ("spacedrive-release-" + [guid]::NewGuid().ToString() + '.md')
try {
    $Notes = "## Download`n`n**[$InstallerName]($DownloadUrl)**`n`nSource commit: $Commit`n"
    [System.IO.File]::WriteAllText($NotesPath, $Notes, [System.Text.UTF8Encoding]::new($false))
    if ($ExistingRelease -ne 'true') {
        gh release create $Version --verify-tag --draft --title "SpaceDrive GPS $Version" --notes-file $NotesPath
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create GitHub release draft; VPS unchanged' }
    }

    $KeyValue = [System.Environment]::GetEnvironmentVariable('VPS_SSH_KEY')
    if ($KeyValue -notmatch '-----BEGIN ' -and (Test-Path -LiteralPath $KeyValue)) {
        [System.Environment]::SetEnvironmentVariable('VPS_SSH_KEY', (Get-Content -LiteralPath $KeyValue -Raw), 'Process')
    }
    & python tools/upload_vps.py $InstallerPath --dist-dir (Join-Path $RepoRoot 'dist\spaceDrive')
    if ($LASTEXITCODE -ne 0) {
        throw 'VPS publication failed. The GitHub release remains a draft; inspect the error before retrying.'
    }
    gh release edit $Version --draft=false
    if ($LASTEXITCODE -ne 0) {
        throw 'VPS publication succeeded, but GitHub remains a draft. Re-run this release to finish publication.'
    }
} finally {
    if (Test-Path -LiteralPath $NotesPath) { Remove-Item -LiteralPath $NotesPath }
}
Write-Host "Release $Version published from $Commit" -ForegroundColor Green
Write-Host "Download: $DownloadUrl"

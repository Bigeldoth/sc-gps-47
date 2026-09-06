param(
    [Parameter(Mandatory = $true)][string]$PlanPath,
    [Parameter(Mandatory = $true)][string]$PlanSha256
)

# This helper is copied outside the installation before launch. It never loads
# application DLLs and never restarts the GPS elevated. The user restarts it.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$script:Plan = $null
$script:LogPath = $null
$updateLock = $null
$canWriteMetadata = $false
$notificationMessage = $null
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Get-Field($Object, [string]$Name, $Default = $null) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Set-Field($Object, [string]$Name, $Value) {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Write-Log([string]$Message) {
    if ($script:LogPath) {
        try { [IO.File]::AppendAllText($script:LogPath, ('{0:o} {1}' -f [DateTime]::UtcNow, $Message) + "`n", $utf8) }
        catch { }
    }
}

function Assert-Version([string]$Version) {
    if ($Version -cnotmatch '^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.[0-9]+)?$') {
        throw 'Invalid transaction version'
    }
    return 'v' + $Version.TrimStart('v')
}

function Assert-Relative([string]$Name) {
    if (-not $Name -or $Name.Length -gt 2000 -or $Name.Contains('\') -or $Name.StartsWith('/')) {
        throw 'Unsafe update path'
    }
    foreach ($part in $Name.Split('/')) {
        if (-not $part -or $part -eq '.' -or $part -eq '..' -or $part -match '[ .]$' -or
            $part -match '[\x00-\x1f<>:"|?*]' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
            throw "Unsafe update path: $Name"
        }
    }
    return $Name
}

function Assert-InstallName([string]$Name) {
    $Name = Assert-Relative $Name
    $lower = $Name.ToLowerInvariant()
    $top = $lower.Split('/')[0]
    if ($lower -in @('config.ini', 'data/user_poi.json') -or
        $top -in @('logs', 'updates', '__pycache__', '.git') -or $top -match '^\.(env|venv)') {
        throw "Update would overwrite user data: $Name"
    }
    return $Name
}

function Assert-NoReparse([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if ([IO.File]::Exists($cursor) -or [IO.Directory]::Exists($cursor)) {
            if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse point in update path: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Safe-Path([string]$Root, [string]$Name) {
    $Name = Assert-Relative $Name
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $full = [IO.Path]::GetFullPath([IO.Path]::Combine($rootFull, $Name.Replace('/', '\')))
    if (-not $full.StartsWith($rootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Update path escaped its directory'
    }
    Assert-NoReparse $full
    return $full
}

function File-Hash([string]$Path) {
    Assert-NoReparse $Path
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Write-JsonAtomic([string]$Path, $Data) {
    Assert-NoReparse $Path
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path)) | Out-Null
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $Data -Depth 30), $utf8)
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Save-State([string]$State, [string]$Message = '') {
    $metadata = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:Plan.metadata_file))
    Set-Field $metadata 'state' $State
    Set-Field $metadata 'updated_at' ([DateTime]::UtcNow.ToString('o'))
    if ($Message) { Set-Field $metadata 'errors' @(@(Get-Field $metadata 'errors' @()) + $Message) }
    Write-JsonAtomic $script:Plan.metadata_file $metadata
    Write-Log ($State + ': ' + $Message)
}

function Copy-Atomic([string]$Source, [string]$Destination) {
    Assert-NoReparse $Source
    Assert-NoReparse $Destination
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Destination)) | Out-Null
    $temporary = $Destination + '.' + [Guid]::NewGuid().ToString('N') + '.update'
    try {
        [IO.File]::Copy($Source, $temporary, $false)
        if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($temporary, $Destination, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $Destination) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Installed-Version {
    $marker = Safe-Path $script:Plan.app_dir '_internal/VERSION'
    if (-not [IO.File]::Exists($marker)) { throw 'Installation has no VERSION marker; use the full installer' }
    return Assert-Version ([IO.File]::ReadAllText($marker).Trim())
}

function Validate-Plan {
    $p = $script:Plan
    if ($p.schema_version -ne 1 -or $p.action -notin @('apply', 'rollback')) { throw 'Unsupported update plan' }
    if ((Assert-Version $p.current_version) -cne $p.current_version -or
        (Assert-Version $p.target_version) -cne $p.target_version) { throw 'Noncanonical update version' }
    $appRoot = [IO.Path]::GetFullPath($p.app_dir).TrimEnd('\')
    $dataRoot = [IO.Path]::GetFullPath($p.data_dir).TrimEnd('\')
    $transaction = [IO.Path]::GetFullPath($p.transaction_dir).TrimEnd('\')
    if (-not [IO.Directory]::Exists($appRoot) -or $appRoot -eq [IO.Path]::GetPathRoot($appRoot).TrimEnd('\')) {
        throw 'Invalid application directory'
    }
    if ($transaction -eq $appRoot -or $transaction.StartsWith($appRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Staging must be outside the installation'
    }
    $stage = Safe-Path $dataRoot 'updates/staging'
    if (-not $transaction.StartsWith($stage + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid transaction directory' }
    $expected = @{
        extract_dir = (Safe-Path $transaction 'extract'); backup_dir = (Safe-Path $transaction 'backup')
        journal_file = (Safe-Path $transaction 'journal.json'); metadata_file = (Safe-Path $stage 'metadata.json')
    }
    foreach ($field in $expected.Keys) {
        if ([IO.Path]::GetFullPath($p.$field) -ne $expected[$field]) { throw "Unexpected plan path: $field" }
    }
    Assert-NoReparse $appRoot
    Assert-NoReparse $dataRoot
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in @($p.files)) {
        $name = Assert-InstallName $file.path
        if (-not $names.Add($name) -or $file.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid or duplicate payload path' }
    }
    foreach ($deleted in @($p.deleted)) {
        $name = Assert-InstallName $deleted
        if (-not $names.Add($name)) { throw 'Conflicting deletion path' }
    }
    foreach ($name in $names) {
        $destination = Safe-Path $appRoot $name
        if ([IO.Directory]::Exists($destination)) { throw 'File update targets a directory' }
    }
}

function Rollback-Transaction([bool]$Mark = $true) {
    Validate-Plan
    $installed = $null
    try { $installed = Installed-Version } catch { }
    if ($installed -and $installed -notin @($script:Plan.current_version, $script:Plan.target_version)) {
        throw 'A different version is installed; refusing stale rollback'
    }
    $journal = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:Plan.journal_file))
    if (-not $journal.ready) { throw 'No complete backup journal' }
    $expected = @(@($script:Plan.files | ForEach-Object { $_.path }) + @($script:Plan.deleted)) | Sort-Object
    $actual = @($journal.entries | ForEach-Object { Assert-InstallName $_.path }) | Sort-Object
    if (($expected -join "`n") -cne ($actual -join "`n")) { throw 'Backup journal does not match the update' }
    foreach ($entry in @($journal.entries)) {
        $null = Safe-Path $script:Plan.app_dir $entry.path
        if ($entry.existed) {
            $saved = Safe-Path $script:Plan.backup_dir $entry.path
            if ((File-Hash $saved) -cne $entry.sha256) { throw "Backup checksum mismatch: $($entry.path)" }
        }
    }
    try { Save-State 'rolling_back' } catch { Write-Log 'Could not persist rolling_back state; restoring files' }
    foreach ($entry in @($journal.entries)) {
        $destination = Safe-Path $script:Plan.app_dir $entry.path
        if ($entry.existed) {
            if (-not [IO.File]::Exists($destination) -or (File-Hash $destination) -cne $entry.sha256) {
                Copy-Atomic (Safe-Path $script:Plan.backup_dir $entry.path) $destination
            }
        }
        elseif ([IO.File]::Exists($destination)) { [IO.File]::Delete($destination) }
    }
    $directories = @(Get-Field $journal 'created_dirs' @())
    [Array]::Reverse($directories)
    foreach ($name in $directories) {
        $directory = Safe-Path $script:Plan.app_dir $name
        if ([IO.Directory]::Exists($directory) -and [IO.Directory]::GetFileSystemEntries($directory).Length -eq 0) {
            [IO.Directory]::Delete($directory, $false)
        }
    }
    if ((Installed-Version) -ne $script:Plan.current_version) { throw 'Restored version verification failed' }
    if ($Mark) { Save-State 'rolled_back' }
}

function Apply-Transaction {
    Validate-Plan
    $filesRoot = Safe-Path $script:Plan.extract_dir 'FILES'
    foreach ($file in @($script:Plan.files)) {
        if ((File-Hash (Safe-Path $filesRoot $file.path)) -cne $file.sha256) { throw "Staged checksum mismatch: $($file.path)" }
    }
    $targetMarker = Safe-Path $filesRoot '_internal/VERSION'
    if ((Assert-Version ([IO.File]::ReadAllText($targetMarker).Trim())) -ne $script:Plan.target_version) {
        throw 'Delta must contain the target VERSION marker'
    }
    if ([IO.File]::Exists($script:Plan.journal_file)) {
        $oldJournal = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:Plan.journal_file))
        if ($oldJournal.ready) { Rollback-Transaction $false }
    }
    if ((Installed-Version) -ne $script:Plan.current_version) { throw 'Installed version does not match the exact delta base' }
    $entries = New-Object 'System.Collections.Generic.List[object]'
    $createdDirs = New-Object 'System.Collections.Generic.HashSet[string]'
    $allNames = @(@($script:Plan.files | ForEach-Object { $_.path }) + @($script:Plan.deleted)) | Sort-Object -Unique
    foreach ($name in $allNames) {
        $original = Safe-Path $script:Plan.app_dir $name
        $entry = [PSCustomObject]@{ path = $name; existed = [IO.File]::Exists($original) }
        if ($entry.existed) {
            $hash = File-Hash $original
            Set-Field $entry 'sha256' $hash
            $saved = Safe-Path $script:Plan.backup_dir $name
            Copy-Atomic $original $saved
            if ((File-Hash $saved) -cne $hash) { throw 'Backup verification failed' }
        }
        $entries.Add($entry)
        $parent = [IO.Path]::GetDirectoryName($original)
        while ($parent -ne $script:Plan.app_dir) {
            if (-not [IO.Directory]::Exists($parent)) {
                $null = $createdDirs.Add($parent.Substring($script:Plan.app_dir.Length + 1).Replace('\', '/'))
            }
            $parent = [IO.Path]::GetDirectoryName($parent)
        }
    }
    $journal = [PSCustomObject]@{ ready = $true; entries = @($entries.ToArray()); created_dirs = @($createdDirs | Sort-Object { $_.Split('/').Count }, { $_ }) }
    Write-JsonAtomic $script:Plan.journal_file $journal
    Save-State 'applying'
    try {
        foreach ($file in @($script:Plan.files)) {
            $destination = Safe-Path $script:Plan.app_dir $file.path
            Copy-Atomic (Safe-Path $filesRoot $file.path) $destination
            if ((File-Hash $destination) -cne $file.sha256) { throw 'Installed file verification failed' }
        }
        foreach ($name in @($script:Plan.deleted)) {
            $destination = Safe-Path $script:Plan.app_dir $name
            if ([IO.File]::Exists($destination)) { [IO.File]::Delete($destination) }
        }
        if ((Installed-Version) -ne $script:Plan.target_version) { throw 'Installed version verification failed' }
        Save-State 'completed'
    } catch {
        $applyError = $_.Exception.Message
        try { Save-State 'failed' $applyError } catch { Write-Log 'Could not persist failure; restoring files' }
        try { Rollback-Transaction }
        catch {
            try { Save-State 'rollback_failed' $_.Exception.Message } catch { Write-Log 'Could not persist rollback failure' }
            throw "Apply failed: $applyError; rollback failed: $($_.Exception.Message)"
        }
        throw "Update failed and was rolled back: $applyError"
    }
}

try {
    if ($PlanSha256 -notmatch '^[a-fA-F0-9]{64}$' -or (File-Hash $PlanPath) -cne $PlanSha256.ToLowerInvariant()) {
        throw 'Update plan checksum mismatch'
    }
    $script:Plan = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($PlanPath))
    Validate-Plan
    $lockPath = Safe-Path $script:Plan.data_dir 'updates/apply.lock'
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($lockPath)) | Out-Null
    $updateLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $canWriteMetadata = $true
    $script:LogPath = Safe-Path $script:Plan.data_dir 'logs/update-helper.log'
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($script:LogPath)) | Out-Null
    Write-Log ('Waiting for application PID ' + $script:Plan.parent_pid)
    $parentProcess = $null
    try { $parentProcess = [Diagnostics.Process]::GetProcessById([int]$script:Plan.parent_pid) }
    catch [ArgumentException] { }
    if ($parentProcess) {
        try {
            if ($parentProcess.StartTime.ToUniversalTime().ToFileTimeUtc() -eq [long]$script:Plan.parent_started_filetime) {
                $timeoutMs = [int]([Math]::Min(600, [Math]::Max(1, $script:Plan.wait_timeout_seconds)) * 1000)
                if (-not $parentProcess.WaitForExit($timeoutMs)) { throw 'Application did not exit; update was not applied' }
            }
        } finally { $parentProcess.Dispose() }
    }
    # Recheck after waiting: no trusted path or hash is assumed immutable.
    if ((File-Hash $PlanPath) -cne $PlanSha256.ToLowerInvariant()) { throw 'Update plan changed while waiting' }
    if ($script:Plan.action -eq 'rollback') { Rollback-Transaction }
    else { Apply-Transaction }
    $notificationMessage = 'The update operation has finished. You can now start SpaceDrive GPS again.'
    exit 0
} catch {
    $notificationMessage = 'The update could not be completed: ' + $_.Exception.Message + "`n`nStart SpaceDrive GPS for recovery options, or use the full installer."
    if ($canWriteMetadata) {
        try {
            $state = (ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:Plan.metadata_file))).state
            if ($state -notin @('rolled_back', 'rollback_failed')) { Save-State 'failed' $_.Exception.Message }
        } catch { Write-Log ('Could not persist failure: ' + $_.Exception.Message) }
    }
    Write-Error $_.Exception.Message
    exit 1
} finally {
    if ($updateLock) { $updateLock.Dispose() }
    if ($canWriteMetadata -and $notificationMessage -and (Get-Field $script:Plan 'notify_user' $false)) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $owner = New-Object System.Windows.Forms.Form
            try {
                $owner.ShowInTaskbar = $false
                $owner.TopMost = $true
                $owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
                $owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
                $owner.Size = New-Object System.Drawing.Size(1, 1)
                $primaryBounds = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
                $owner.Location = New-Object System.Drawing.Point(
                    ($primaryBounds.Left + [int]($primaryBounds.Width / 2)),
                    ($primaryBounds.Top + [int]($primaryBounds.Height / 2)))
                $owner.Show()
                $owner.Activate()
                $owner.BringToFront()
                [System.Windows.Forms.MessageBox]::Show($owner, $notificationMessage, 'SpaceDrive GPS update') | Out-Null
            } finally { $owner.Dispose() }
        } catch { }
    }
}

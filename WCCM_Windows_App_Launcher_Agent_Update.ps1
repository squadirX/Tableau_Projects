# WCCM P6.3 Windows App Launcher Agent Update
# Adds a read-only Start Menu catalog and an explicit, ID-only launch endpoint.
# No arbitrary command, shell, executable path, PowerShell text, or arguments are accepted.
$ErrorActionPreference = 'Stop'

$InstallDir = Join-Path $env:LOCALAPPDATA 'WCCM'
$AgentPath  = Join-Path $InstallDir 'wccm_windows_agent.ps1'
$ConfigPath = Join-Path $InstallDir 'agent-config.json'
$Launcher   = Join-Path $InstallDir 'Start-WCCM-Agent.cmd'
$TaskName   = 'WCCM Agent AutoStart'
$Marker     = 'WCCM_LAUNCHER_CATALOG_V1'

function Get-WccmAgentListener {
    $listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$listener.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -match '(?i)wccm_windows_agent\.ps1') {
            return [pscustomobject]@{ Pid = [int]$listener.OwningProcess; Process = $owner }
        }
    }
    return $null
}

function Wait-WccmAgentListener([int]$Seconds) {
    for ($i = 0; $i -lt ($Seconds * 2); $i++) {
        $found = Get-WccmAgentListener
        if ($found) { return $found }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

Write-Host '===== WCCM WINDOWS APP LAUNCHER AGENT UPDATE ====='
foreach ($p in @($AgentPath, $ConfigPath, $Launcher)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "STOP: Required existing WCCM file not found: $p" }
}

$OriginalBytes = [System.IO.File]::ReadAllBytes($AgentPath)
$Original = [System.Text.Encoding]::UTF8.GetString($OriginalBytes)
$OriginalHash = (Get-FileHash -LiteralPath $AgentPath -Algorithm SHA256).Hash
$ConfigHashBefore = (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash
if ($Original.Contains($Marker)) {
    Write-Host 'PASS: App Launcher endpoint is already installed; no change made.'
    exit 0
}

# Require the verified P6.x architecture and exact insertion points. Fail closed on variants.
$RequiredPatterns = @(
    '(?m)^\$GetPaths\s*=\s*@\(',
    '(?m)^\$PostPaths\s*=\s*@\(',
    '(?m)^\$Listener\s*=\s*\$null\s*$',
    '(?m)^\s*if\s*\(\$Method\s*-eq\s*"GET"\s*-and\s*\$Path\s*-eq\s*"/api/health"\)\s*\{',
    '(?m)^\s*if\s*\(\$Path\s*-eq\s*"/api/p4/launch"\)\s*\{',
    '(?m)^\s*\$KnownPaths\s*=\s*@\(\$GetPaths\s*\+\s*\$PostPaths\s*\)'
)
foreach ($pattern in $RequiredPatterns) {
    if ([regex]::Matches($Original, $pattern).Count -ne 1) {
        throw "STOP: Current Agent does not match the expected guarded P6.x structure at $pattern. No file changed."
    }
}
if ($Original -notmatch 'Test-ConstantTimeEquals' -or $Original -notmatch 'Authorization' -or
    $Original -notmatch 'Get-WccmAppsPayload' -or $Original -notmatch 'Invoke-WccmAppAction' -or
    $Original -notmatch '/api/power/restart' -or $Original -notmatch '/api/power/shutdown' -or
    $Original -notmatch '/api/power/hibernate' -or $Original -notmatch 'HIBERNATE_WINDOWS_PC') {
    throw 'STOP: Existing authentication, P6.2 app control or accepted P6.3 Restart/Shutdown/Hibernate features were not found. No file changed.'
}
Write-Host 'PASS: Current P6.3 Agent structure, authentication, app control and three Power Control actions verified.'

$CatalogFunctions = @'
# WCCM_LAUNCHER_CATALOG_V1 — restricted Windows Start Menu application catalogue.
$script:WccmLauncherCache = @()
$script:WccmLauncherCacheAt = [DateTime]::MinValue

function Get-WccmLauncherIconBase64 {
    param([string]$TargetPath)
    $icon = $null; $bitmap = $null; $graphics = $null; $stream = $null
    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($TargetPath)
        if (-not $icon) { return '' }
        $bitmap = New-Object System.Drawing.Bitmap 32, 32
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.DrawIcon($icon, 1, 1, 30, 30)
        $stream = New-Object System.IO.MemoryStream
        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return [Convert]::ToBase64String($stream.ToArray())
    } catch {
        return ''
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($graphics) { $graphics.Dispose() }
        if ($bitmap) { $bitmap.Dispose() }
        if ($icon) { $icon.Dispose() }
    }
}

function Get-WccmLauncherEntries {
    # Cache only discovery results for 30 seconds; never persist executable paths or launch state.
    if ($script:WccmLauncherCacheAt -gt [DateTime]::UtcNow.AddSeconds(-30)) {
        return @($script:WccmLauncherCache)
    }
    $roots = @(
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
        (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } | Select-Object -Unique

    $blocked = @(
        'cmd','powershell','pwsh','wscript','cscript','mshta','rundll32','regsvr32',
        'shutdown','taskkill','sc','net','format','diskpart','bcdedit','reg','regedit',
        'cmdkey','control','mmc','explorer','winlogon','services','msiexec','schtasks',
        'wmic','psexec','bash','wsl','ssh','ftp','bitsadmin','certutil'
    )
    $denyTitle = '(?i)(^|\b)(uninstall|uninstaller|readme|help|support|website|documentation|repair|remove)(\b|$)'
    $shell = $null
    $items = New-Object System.Collections.Generic.List[object]
    $seenTargets = @{}
    try { $shell = New-Object -ComObject WScript.Shell -ErrorAction Stop }
    catch { return @() }

    try {
        foreach ($root in $roots) {
            $rootFull = [System.IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
            $links = @(Get-ChildItem -LiteralPath $rootFull -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue)
            foreach ($link in $links) {
                try {
                    $shortcutFull = [System.IO.Path]::GetFullPath($link.FullName)
                    if (-not $shortcutFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                    if ($link.BaseName -match $denyTitle) { continue }
                    $shortcut = $shell.CreateShortcut($shortcutFull)
                    $target = [Environment]::ExpandEnvironmentVariables([string]$shortcut.TargetPath)
                    if ([string]::IsNullOrWhiteSpace($target) -or -not [System.IO.Path]::IsPathRooted($target)) { continue }
                    $target = [System.IO.Path]::GetFullPath($target)
                    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
                    $extension = [System.IO.Path]::GetExtension($target).ToLowerInvariant()
                    if ($extension -notin @('.exe','.com')) { continue }
                    $base = [System.IO.Path]::GetFileNameWithoutExtension($target).ToLowerInvariant()
                    if ($base -in $blocked) { continue }
                    $targetKey = $target.ToLowerInvariant()
                    if ($seenTargets.ContainsKey($targetKey)) { continue }

                    $sha = [System.Security.Cryptography.SHA256]::Create()
                    try {
                        $bytes = [System.Text.Encoding]::UTF8.GetBytes($shortcutFull.ToLowerInvariant())
                        $appId = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
                    } finally { $sha.Dispose() }

                    $relative = $shortcutFull.Substring($rootFull.Length)
                    $parts = $relative -split '\\'
                    $category = if ($parts.Count -gt 1) { $parts[0] } else { 'Applications' }
                    $iconData = Get-WccmLauncherIconBase64 -TargetPath $target
                    if ($iconData.Length -gt 8000) { $iconData = '' }
                    $items.Add([pscustomobject]@{
                        app_id = $appId
                        name = [string]$link.BaseName
                        category = [string]$category
                        icon_png_base64 = [string]$iconData
                        shortcut_path = [string]$shortcutFull
                    })
                    $seenTargets[$targetKey] = $true
                    if ($items.Count -ge 48) { break }
                } catch { continue }
            }
            if ($items.Count -ge 48) { break }
        }
    } finally {
        if ($shell) { try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) } catch {} }
    }
    $script:WccmLauncherCache = @($items | Sort-Object name)
    $script:WccmLauncherCacheAt = [DateTime]::UtcNow
    return @($script:WccmLauncherCache)
}

function Get-WccmLauncherPayload {
    $entries = @(Get-WccmLauncherEntries)
    $publicApps = @($entries | ForEach-Object {
        [pscustomobject]@{
            app_id = $_.app_id
            name = $_.name
            category = $_.category
            icon_png_base64 = $_.icon_png_base64
        }
    })
    return [pscustomobject]@{
        schema_version = 1
        catalog_version = 1
        status = 'ONLINE'
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        computer_name = $env:COMPUTERNAME
        inventory_scope = 'Up to 48 launchable Windows Start Menu shortcuts whose targets are existing EXE/COM files; oversized icons use an emoji fallback. Windows Store/UWP entries and background-only apps may not appear.'
        app_count = $publicApps.Count
        apps = $publicApps
    }
}

function Invoke-WccmLauncherOpen {
    param([object]$Payload)
    $properties = @()
    if ($Payload -is [System.Collections.IDictionary]) { $properties = @($Payload.Keys) }
    elseif ($Payload -is [PSCustomObject]) { $properties = @($Payload.PSObject.Properties.Name) }
    if ($properties.Count -ne 1 -or $properties[0] -ne 'app_id' -or [string]$Payload.app_id -notmatch '^[a-f0-9]{64}$') {
        return @{ status = 400; reason = 'Bad Request'; body = @{ status = 'ONLINE'; error = 'invalid_launcher_app_id' } }
    }
    $entries = @(Get-WccmLauncherEntries)
    $match = @($entries | Where-Object { $_.app_id -ceq [string]$Payload.app_id })
    if ($match.Count -ne 1) {
        return @{ status = 404; reason = 'Not Found'; body = @{ status = 'ONLINE'; error = 'launcher_app_not_found'; message = 'That Start Menu app is no longer in the current launch catalogue. Refresh the catalogue.' } }
    }
    $entry = $match[0]
    if (-not (Test-Path -LiteralPath $entry.shortcut_path -PathType Leaf)) {
        $script:WccmLauncherCacheAt = [DateTime]::MinValue
        return @{ status = 404; reason = 'Not Found'; body = @{ status = 'ONLINE'; error = 'launcher_shortcut_missing'; message = 'The selected Start Menu shortcut no longer exists. Refresh the catalogue.' } }
    }
    try {
        # Start only the selected, current Start Menu shortcut. No URL/path/argument is accepted from Mac.
        $process = Start-Process -FilePath $entry.shortcut_path -PassThru -ErrorAction Stop
        $processId = $null
        if ($process) { $processId = [int]$process.Id }
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=launcher_open Result=requested AppId=$($entry.app_id.Substring(0,12))"
        return @{ status = 202; reason = 'Accepted'; body = @{
            status = 'ONLINE'; action = 'launcher_open'; result = 'requested'
            app_id = $entry.app_id; app_name = $entry.name; process_id = $processId
            message = "Launch requested for $($entry.name). Refresh Visible Applications to verify its window."
        } }
    } catch {
        Write-Warning "WCCM Start Menu launch failed: $($_.Exception.GetType().Name)"
        return @{ status = 500; reason = 'Internal Server Error'; body = @{ status = 'ONLINE'; error = 'launcher_start_failed'; message = 'Windows could not open the selected Start Menu shortcut.' } }
    }
}
'@

$Updated = $Original
# Expand the known GET/POST route lists with exactly one fixed route each.
foreach ($spec in @(
    @{ Name = 'GetPaths'; Pattern = '(?ms)^\$GetPaths\s*=\s*@\((?<body>.*?)\)' ; Add = '"/api/launcher/apps"' },
    @{ Name = 'PostPaths'; Pattern = '(?ms)^\$PostPaths\s*=\s*@\((?<body>.*?)\)' ; Add = '"/api/launcher/open"' }
)) {
    $m = [regex]::Match($Updated, $spec.Pattern)
    if (-not $m.Success) { throw "STOP: Could not locate $($spec.Name). No file changed." }
    if ($m.Value.Contains($spec.Add)) { throw "STOP: $($spec.Name) already contains the new route unexpectedly. No file changed." }
    $replacement = $m.Value.Substring(0, $m.Value.Length - 1).TrimEnd() + ', ' + $spec.Add + ')'
    $Updated = $Updated.Substring(0, $m.Index) + $replacement + $Updated.Substring($m.Index + $m.Length)
}

$listenerMarker = '$Listener = $null'
if ([regex]::Matches($Updated, '(?m)^\$Listener\s*=\s*\$null\s*$').Count -ne 1) { throw 'STOP: Agent listener insertion anchor is not unique. No file changed.' }
$Updated = $Updated.Replace($listenerMarker, $CatalogFunctions + "`r`n`r`n" + $listenerMarker)

$getAnchor = '            if ($Method -eq "GET" -and $Path -eq "/api/health") {'
if ([regex]::Matches($Updated, [regex]::Escape($getAnchor)).Count -ne 1) { throw 'STOP: GET route insertion anchor is not unique. No file changed.' }
$getBranch = @'
            if ($Method -eq "GET" -and $Path -eq "/api/launcher/apps") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmLauncherPayload)
                continue
            }
'@
$Updated = $Updated.Replace($getAnchor, $getBranch + $getAnchor)

$postAnchor = '                if ($Path -eq "/api/p4/launch") {'
if ([regex]::Matches($Updated, [regex]::Escape($postAnchor)).Count -ne 1) { throw 'STOP: POST route insertion anchor is not unique. No file changed.' }
$postBranch = @'
                if ($Path -eq "/api/launcher/open") {
                    $Result = Invoke-WccmLauncherOpen -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }
'@
$Updated = $Updated.Replace($postAnchor, $postBranch + $postAnchor)

# Verify PowerShell parsing and preserve every existing action/route before any write.
$tokens = $null; $parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseInput($Updated, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {
    $parseErrors | ForEach-Object { Write-Host $_.Message }
    throw 'STOP: Updated Agent failed PowerShell syntax validation. No file changed.'
}
foreach ($needle in @(
    '/api/launcher/apps','/api/launcher/open',$Marker,
    '/api/apps/action','/api/apps/process-status','/api/power/restart',
    'Get-WccmLauncherPayload','Invoke-WccmLauncherOpen','Test-ConstantTimeEquals'
)) {
    if (-not $Updated.Contains($needle)) { throw "STOP: Updated Agent post-check missing $needle. No file changed." }
}
if ($Updated -match 'Start-Process\s+-FilePath\s+\$Payload' -or $Updated -match 'Invoke-Expression') {
    throw 'STOP: Arbitrary command execution pattern detected. No file changed.'
}
Write-Host 'PASS: PowerShell parser validation.'
Write-Host 'PASS: Start Menu-only catalogue; direct EXE/COM targets; risky shell executables excluded.'
Write-Host 'PASS: Launch endpoint accepts only a current 64-character catalogue ID.'
Write-Host 'PASS: Existing application close, review, Restart/Shutdown/Hibernate and Remote Desktop functions are retained.'

# Verify existing task, single listener, token config and rollback before replacement.
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -eq 'Disabled') { throw 'STOP: Existing WCCM Agent AutoStart task is disabled. No update made.' }
$Current = Get-WccmAgentListener
if (-not $Current) { throw 'STOP: WCCM Agent is not currently listening on TCP 8765. No update made.' }
$AllListeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($AllListeners.Count -ne 1 -or [int]$AllListeners[0].OwningProcess -ne $Current.Pid) {
    throw 'STOP: TCP 8765 is not owned by exactly one verified WCCM Agent. No update made.'
}
$Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$Config.token)) { throw 'STOP: Existing WCCM token was not readable. No update made.' }

$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupPath = "$AgentPath.backup_pre_launcher_$Stamp"
Copy-Item -LiteralPath $AgentPath -Destination $BackupPath
if ((Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash -ne $OriginalHash) {
    throw 'STOP: Backup hash verification failed. Installed Agent was not changed.'
}
Write-Host "PASS: Verified rollback backup created: $BackupPath"
Write-Host "PASS: Existing WCCM Agent AutoStart task and listener PID $($Current.Pid) verified."

# Stop only the existing WCCM Agent task/process, atomically replace source, then restart same task.
try {
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        for ($i = 0; $i -lt 40; $i++) {
            if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { break }
            Start-Sleep -Milliseconds 250
        }
    }
    $Current = Get-WccmAgentListener
    if ($Current) { Stop-Process -Id $Current.Pid -Force -ErrorAction Stop }
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Verified Agent did not release port 8765. No source replacement made.'
    }
} catch {
    try { if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } } catch {}
    $Recovery = Wait-WccmAgentListener -Seconds 30
    if ($Recovery) { throw "STOP: Agent could not be stopped cleanly; original service recovered at PID $($Recovery.Pid). No file changed." }
    throw "STOP: Agent stop failed; no source file replaced. Check the existing WCCM launcher. Details: $($_.Exception.Message)"
}

$TempPath = "$AgentPath.launcher_tmp"
try {
    [System.IO.File]::WriteAllText($TempPath, $Updated, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $TempPath -Destination $AgentPath -Force
    if ((Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash -ne $ConfigHashBefore) {
        throw 'Existing agent-config.json changed unexpectedly.'
    }
    if (-not ([System.IO.File]::ReadAllText($AgentPath)).Contains($Marker)) { throw 'Installed Agent marker is missing.' }
} catch {
    Copy-Item -LiteralPath $BackupPath -Destination $AgentPath -Force
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    throw "Agent update failed; original Agent restored and existing task relaunched. Details: $($_.Exception.Message)"
}

try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
catch {
    Copy-Item -LiteralPath $BackupPath -Destination $AgentPath -Force
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    throw "Could not restart existing AutoStart task; original Agent restored. Details: $($_.Exception.Message)"
}

$Online = Wait-WccmAgentListener -Seconds 60
if (-not $Online) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    $Failed = Get-WccmAgentListener
    if ($Failed) { Stop-Process -Id $Failed.Pid -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    Copy-Item -LiteralPath $BackupPath -Destination $AgentPath -Force
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $Restored = Wait-WccmAgentListener -Seconds 60
    if ($Restored) { throw "Launcher update safely rolled back; original Agent is ONLINE again (PID $($Restored.Pid)). Backup: $BackupPath" }
    throw "CRITICAL: Launcher update rolled back, but original Agent did not return. Start the existing WCCM launcher manually. Backup: $BackupPath"
}

if ((Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash -ne $ConfigHashBefore) {
    throw 'CRITICAL: Existing token/config hash changed unexpectedly. Do not continue.'
}
if ((Get-FileHash -LiteralPath $AgentPath -Algorithm SHA256).Hash -eq $OriginalHash) {
    throw 'CRITICAL: Agent source hash was not changed as expected.'
}

# Authenticated smoke test; only reads the catalogue and does not launch anything.
try {
    $headers = @{ Authorization = 'Bearer ' + [string]$Config.token; Accept = 'application/json' }
    $catalog = Invoke-RestMethod -Uri 'http://192.168.2.162:8765/api/launcher/apps' -Headers $headers -Method Get -TimeoutSec 12
    if ($catalog.status -ne 'ONLINE' -or $null -eq $catalog.apps) { throw 'Launcher catalogue response schema is invalid.' }
    Write-Host "PASS: Authenticated launcher catalogue endpoint returned $($catalog.app_count) Start Menu shortcut(s)."
    Write-Host 'NOTE: No application was launched during installation.'
} catch {
    # The Agent remains installed only if the new endpoint is structurally live; rollback on failed smoke test.
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    $Failed = Get-WccmAgentListener
    if ($Failed) { Stop-Process -Id $Failed.Pid -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 40; $i++) { if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Milliseconds 250 }
    Copy-Item -LiteralPath $BackupPath -Destination $AgentPath -Force
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $Restored = Wait-WccmAgentListener -Seconds 60
    if ($Restored) { throw "Launcher API smoke test failed; original Agent restored and ONLINE. Backup: $BackupPath. Details: $($_.Exception.Message)" }
    throw "CRITICAL: Launcher API smoke test failed and original Agent did not return. Backup: $BackupPath"
}

Write-Host "PASS: Config/token SHA-256 unchanged."
Write-Host 'PASS: Existing Agent launcher and Scheduled Task definition untouched.'
Write-Host 'PASS: Existing Restart, Shutdown, Hibernate, Remote Desktop and application-close routes retained.'
Write-Host 'WCCM WINDOWS APP LAUNCHER AGENT UPDATE: COMPLETE'
Write-Host 'No application was launched during installation.'

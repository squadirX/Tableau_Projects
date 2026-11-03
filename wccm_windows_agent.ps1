$ErrorActionPreference = "Continue"

$InstallDir = Join-Path $env:LOCALAPPDATA "WCCM"
$ConfigPath = Join-Path $InstallDir "agent-config.json"
$script:WccmP4LaunchedNotepad = @{}

function Write-JsonResponse {
    param(
        [System.Net.Sockets.NetworkStream]$Stream,
        [int]$StatusCode,
        [string]$Reason,
        [object]$Payload
    )
    $Json = $Payload | ConvertTo-Json -Depth 7 -Compress
    $Body = [System.Text.Encoding]::UTF8.GetBytes($Json)
    $Header = "HTTP/1.1 $StatusCode $Reason`r`n" +
        "Content-Type: application/json; charset=utf-8`r`n" +
        "Content-Length: $($Body.Length)`r`n" +
        "Cache-Control: no-store`r`n" +
        "X-Content-Type-Options: nosniff`r`n" +
        "Connection: close`r`n`r`n"
    $HeaderBytes = [System.Text.Encoding]::ASCII.GetBytes($Header)
    $Stream.Write($HeaderBytes, 0, $HeaderBytes.Length)
    $Stream.Write($Body, 0, $Body.Length)
    $Stream.Flush()
}

function Test-ConstantTimeEquals {
    param([string]$Left, [string]$Right)
    if ($null -eq $Left -or $null -eq $Right -or $Left.Length -ne $Right.Length) {
        return $false
    }
    $Difference = 0
    for ($Index = 0; $Index -lt $Left.Length; $Index++) {
        $Difference = $Difference -bor ([int][char]$Left[$Index] -bxor [int][char]$Right[$Index])
    }
    return ($Difference -eq 0)
}

function Get-WccmHealthPayload {
    $Now = [DateTime]::UtcNow
    $OS = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $Computer = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    $Boot = [DateTime]$OS.LastBootUpTime
    $Uptime = [int][Math]::Max(0, ($Now - $Boot.ToUniversalTime()).TotalSeconds)

    $IPv4 = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*"
            } | Select-Object -ExpandProperty IPAddress -Unique
    )

    $TotalMemoryBytes = [int64]$OS.TotalVisibleMemorySize * 1024
    $FreeMemoryBytes = [int64]$OS.FreePhysicalMemory * 1024
    $UsedPercent = $null
    if ($TotalMemoryBytes -gt 0) {
        $UsedPercent = [int][Math]::Round((1 - ($FreeMemoryBytes / $TotalMemoryBytes)) * 100)
    }

    $DriveLetter = $env:SystemDrive
    $Disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$DriveLetter'" -ErrorAction SilentlyContinue
    $DiskTotal = $null
    $DiskFree = $null
    $DiskFreePercent = $null
    if ($Disk) {
        $DiskTotal = [int64]$Disk.Size
        $DiskFree = [int64]$Disk.FreeSpace
        if ($DiskTotal -gt 0) { $DiskFreePercent = [Math]::Round(($DiskFree / $DiskTotal) * 100, 1) }
    }

    $RdpListening = $false
    try {
        $RdpListener = @(Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction Stop)
        $RdpListening = ($RdpListener.Count -gt 0)
    } catch {}

    [PSCustomObject]@{
        schema_version = 1
        agent_version = "0.3.1-p6.3"
        status = "ONLINE"
        timestamp_utc = $Now.ToString("o")
        computer_name = $env:COMPUTERNAME
        ipv4_addresses = $IPv4
        windows_version = "$($OS.Caption) (Version $($OS.Version), Build $($OS.BuildNumber))"
        architecture = $OS.OSArchitecture
        current_user = if ($Computer -and $Computer.UserName) { $Computer.UserName } else { $env:USERNAME }
        uptime_seconds = $Uptime
        boot_time_utc = $Boot.ToUniversalTime().ToString("o")
        memory = [PSCustomObject]@{ total_bytes = $TotalMemoryBytes; available_bytes = $FreeMemoryBytes; used_percent = $UsedPercent }
        system_drive = [PSCustomObject]@{ drive = $DriveLetter; total_bytes = $DiskTotal; free_bytes = $DiskFree; free_percent = $DiskFreePercent }
        rdp_listener_local = $RdpListening
        mode = "P4_ALLOWLISTED_TEST_APP_CONTROL"
    }
}

function Get-WccmAppsPayload {
    $Now = [DateTime]::UtcNow
    $CurrentSessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    $Apps = @(
        Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
            $Process = $_
            $ProcessSessionId = -1
            try { $ProcessSessionId = [int]$Process.SessionId } catch { }
            if ($Process.Id -ne $PID -and $ProcessSessionId -eq $CurrentSessionId) {
                $Handle = [int64]0
                $HasWindow = $false
                try {
                    $Handle = [int64]$Process.MainWindowHandle
                    $HasWindow = -not [string]::IsNullOrWhiteSpace([string]$Process.MainWindowTitle)
                } catch { $Handle = [int64]0; $HasWindow = $false }
                if ($Handle -ne 0 -and $HasWindow) {
                    $StartedUtc = $null
                    try { $StartedUtc = $Process.StartTime.ToUniversalTime().ToString("o") } catch { }
                    [PSCustomObject]@{
                        process_name = [string]$Process.ProcessName
                        process_id = [int]$Process.Id
                        process_started_utc = $StartedUtc
                        session_id = $ProcessSessionId
                        visible_window = $true
                    }
                }
            }
        } | Sort-Object process_name, process_id
    )
    [PSCustomObject]@{
        schema_version = 1
        agent_version = "0.3.1-p6.3"
        status = "ONLINE"
        timestamp_utc = $Now.ToString("o")
        computer_name = $env:COMPUTERNAME
        current_user = "$env:USERDOMAIN\$env:USERNAME"
        inventory_scope = "Visible top-level windows in the current agent session; window titles are not returned. Background processes without a visible window are excluded."
        app_count = $Apps.Count
        apps = @($Apps)
        mode = "P4_ALLOWLISTED_TEST_APP_CONTROL"
    }
}

function Get-WccmP4TestAppPayload {
    $Active = @()
    foreach ($Key in @($script:WccmP4LaunchedNotepad.Keys)) {
        $Process = $script:WccmP4LaunchedNotepad[$Key]
        try {
            if ($Process -and -not $Process.HasExited) {
                $Active += [PSCustomObject]@{
                    app_id = "notepad"
                    process_id = [int]$Process.Id
                    lifecycle = "launched_by_wccm_p4"
                    close_policy = "graceful_only"
                }
            } else {
                $script:WccmP4LaunchedNotepad.Remove($Key)
            }
        } catch {
            $script:WccmP4LaunchedNotepad.Remove($Key)
        }
    }
    [PSCustomObject]@{
        schema_version = 1
        agent_version = "0.3.1-p6.3"
        status = "ONLINE"
        timestamp_utc = [DateTime]::UtcNow.ToString("o")
        app_id = "notepad"
        allowlisted = $true
        active_count = $Active.Count
        instances = @($Active)
        scope = "Only Notepad processes launched by this running WCCM P4 agent instance are eligible for graceful close. Existing user-launched Notepad processes are never controlled."
    }
}

function Invoke-WccmP4Launch {
    param([object]$Payload)
    if ([string]$Payload.app_id -ne "notepad") {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "app_not_allowlisted"; allowed_app_ids = @("notepad") } }
    }

    foreach ($Key in @($script:WccmP4LaunchedNotepad.Keys)) {
        $Existing = $script:WccmP4LaunchedNotepad[$Key]
        try {
            if ($Existing -and -not $Existing.HasExited) {
                return @{ status = 409; reason = "Conflict"; body = @{ status = "ONLINE"; error = "test_app_already_running"; app_id = "notepad"; process_id = [int]$Existing.Id; message = "The WCCM-launched test Notepad instance is already running." } }
            }
            $script:WccmP4LaunchedNotepad.Remove($Key)
        } catch { $script:WccmP4LaunchedNotepad.Remove($Key) }
    }

    $NotepadPath = Join-Path $env:WINDIR "System32\notepad.exe"
    if (-not (Test-Path -LiteralPath $NotepadPath -PathType Leaf)) {
        return @{ status = 500; reason = "Internal Server Error"; body = @{ status = "ONLINE"; error = "allowlisted_app_not_found"; app_id = "notepad" } }
    }

    try {
        $Process = Start-Process -FilePath $NotepadPath -PassThru -ErrorAction Stop
        Start-Sleep -Milliseconds 350
        if ($Process.HasExited) {
            return @{ status = 500; reason = "Internal Server Error"; body = @{ status = "ONLINE"; error = "test_app_exited_during_launch"; app_id = "notepad"; message = "The allowlisted test process exited immediately; no further action was attempted." } }
        }
        $script:WccmP4LaunchedNotepad[[int]$Process.Id] = $Process
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=launch Result=started App=notepad PID=$($Process.Id)"
        return @{ status = 201; reason = "Created"; body = @{ status = "ONLINE"; action = "launch"; result = "started"; app_id = "notepad"; process_id = [int]$Process.Id; message = "One allowlisted WCCM test Notepad instance was started."; note = "Only this tracked instance is eligible for graceful close." } }
    } catch {
        Write-Warning "WCCM P4 allowlisted launch failed: $($_.Exception.GetType().Name)"
        return @{ status = 500; reason = "Internal Server Error"; body = @{ status = "ONLINE"; error = "launch_failed"; app_id = "notepad"; message = "The allowlisted test app could not be started." } }
    }
}

function Invoke-WccmP4Close {
    param([object]$Payload)
    $ProcessId = 0
    if (-not [int]::TryParse([string]$Payload.process_id, [ref]$ProcessId) -or $ProcessId -le 0) {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "invalid_process_id" } }
    }
    if ([string]$Payload.confirmation -ne "CLOSE_WCCM_TEST_NOTEPAD") {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "explicit_confirmation_required"; message = "The explicit WCCM test-app close confirmation was missing." } }
    }
    if (-not $script:WccmP4LaunchedNotepad.ContainsKey($ProcessId)) {
        return @{ status = 404; reason = "Not Found"; body = @{ status = "ONLINE"; error = "not_a_wccm_test_instance"; message = "Only a Notepad instance launched by this running WCCM P4 agent can be closed by this endpoint." } }
    }

    $Process = $script:WccmP4LaunchedNotepad[$ProcessId]
    try {
        if ($Process.HasExited) {
            $script:WccmP4LaunchedNotepad.Remove($ProcessId)
            return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; action = "close"; result = "already_exited"; app_id = "notepad"; process_id = $ProcessId; message = "The tracked test process had already exited." } }
        }
        $Requested = $Process.CloseMainWindow()
        if (-not $Requested) {
            return @{ status = 409; reason = "Conflict"; body = @{ status = "ONLINE"; action = "close"; result = "close_not_requested"; app_id = "notepad"; process_id = $ProcessId; message = "Windows did not accept a graceful close request. The process was not force-killed." } }
        }
        $null = $Process.WaitForExit(2500)
        if ($Process.HasExited) {
            $script:WccmP4LaunchedNotepad.Remove($ProcessId)
            Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=close Result=closed App=notepad PID=$ProcessId"
            return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; action = "close"; result = "closed"; app_id = "notepad"; process_id = $ProcessId; message = "The tracked WCCM test Notepad process exited after the graceful-close request."; force_kill = $false } }
        }
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=close Result=still_running App=notepad PID=$ProcessId"
        return @{ status = 202; reason = "Accepted"; body = @{ status = "ONLINE"; action = "close"; result = "close_requested"; app_id = "notepad"; process_id = $ProcessId; message = "The app is still running after the graceful request. It may be waiting on its own save or confirmation dialog. No force-kill was performed."; force_kill = $false } }
    } catch {
        Write-Warning "WCCM P4 graceful-close request failed: $($_.Exception.GetType().Name)"
        return @{ status = 500; reason = "Internal Server Error"; body = @{ status = "ONLINE"; action = "close"; error = "graceful_close_failed"; app_id = "notepad"; process_id = $ProcessId; message = "Graceful close could not be confirmed; no force-kill was attempted."; force_kill = $false } }
    }
}


$script:WccmProtectedAppNames = @(
    "ApplicationFrameHost", "TextInputHost", "dwm", "explorer", "winlogon",
    "wininit", "csrss", "smss", "services", "lsass", "svchost", "spoolsv",
    "fontdrvhost", "sihost", "StartMenuExperienceHost", "ShellExperienceHost",
    "SearchHost", "SearchApp", "RuntimeBroker", "System", "Registry", "Idle",
    "WmiPrvSE", "conhost", "ctfmon", "powershell", "powershell_ise", "pwsh"
)

function Get-WccmExactProcess {
    param(
        [int]$ProcessId,
        [string]$ProcessName,
        [string]$ProcessStartedUtc,
        [bool]$RequireVisibleWindow = $false
    )

    if ($ProcessId -le 0 -or [string]::IsNullOrWhiteSpace($ProcessName) -or
        [string]::IsNullOrWhiteSpace($ProcessStartedUtc)) {
        return @{ ok = $false; status = 400; reason = "Bad Request"; error = "invalid_process_identity"; message = "A complete process identity is required." }
    }

    if ($ProcessId -eq $PID) {
        return @{ ok = $false; status = 403; reason = "Forbidden"; error = "agent_process_protected"; message = "The WCCM Agent process is protected." }
    }

    try {
        $Process = Get-Process -Id $ProcessId -ErrorAction Stop
        $ActualName = [string]$Process.ProcessName
        $ActualStartedUtc = $Process.StartTime.ToUniversalTime().ToString("o")
        $ActualSession = [int]$Process.SessionId
        $AgentSession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    } catch {
        return @{ ok = $false; status = 409; reason = "Conflict"; error = "process_identity_unavailable"; message = "Windows could not verify this process identity. No action was taken." }
    }

    if (-not $ActualName.Equals($ProcessName, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $ActualStartedUtc.Equals($ProcessStartedUtc, [System.StringComparison]::Ordinal) -or
        $ActualSession -ne $AgentSession) {
        return @{ ok = $false; status = 409; reason = "Conflict"; error = "process_identity_changed"; message = "The PID now refers to a different process or session. No action was taken." }
    }

    if ($ActualName -in $script:WccmProtectedAppNames) {
        return @{ ok = $false; status = 403; reason = "Forbidden"; error = "protected_windows_process"; message = "This Windows shell or system process is protected from application control." }
    }

    if ($RequireVisibleWindow) {
        try {
            if ([int64]$Process.MainWindowHandle -eq 0) {
                return @{ ok = $false; status = 409; reason = "Conflict"; error = "visible_window_not_available"; message = "The selected process no longer has a visible top-level window. Refresh the inventory before retrying." }
            }
        } catch {
            return @{ ok = $false; status = 409; reason = "Conflict"; error = "window_check_failed"; message = "The selected window could not be verified. No action was taken." }
        }
    }

    return @{ ok = $true; process = $Process; process_id = $ProcessId; process_name = $ActualName; process_started_utc = $ActualStartedUtc }
}

function Invoke-WccmAppAction {
    param([object]$Payload)

    $Action = [string]$Payload.action
    $ProcessId = 0
    if (-not [int]::TryParse([string]$Payload.process_id, [ref]$ProcessId)) {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "invalid_process_id" } }
    }

    $ExpectedConfirmation = switch ($Action) {
        "save_and_close" { "SAVE_AND_CLOSE_SELECTED_APP" }
        "close_without_saving" { "CLOSE_WITHOUT_SAVING_SELECTED_APP" }
        default { "" }
    }
    if (-not $ExpectedConfirmation -or [string]$Payload.confirmation -ne $ExpectedConfirmation) {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "explicit_action_confirmation_required"; message = "The selected application action requires its exact confirmation value." } }
    }

    $Selected = Get-WccmExactProcess -ProcessId $ProcessId -ProcessName ([string]$Payload.process_name) -ProcessStartedUtc ([string]$Payload.process_started_utc) -RequireVisibleWindow $true
    if (-not $Selected.ok) {
        return @{ status = $Selected.status; reason = $Selected.reason; body = @{ status = "ONLINE"; error = $Selected.error; message = $Selected.message; process_id = $ProcessId } }
    }

    $Process = $Selected.process
    try {
        if ($Action -eq "save_and_close") {
            # WM_CLOSE lets the application handle its own Save dialog. WCCM cannot universally save arbitrary documents.
            $Requested = $Process.CloseMainWindow()
            if (-not $Requested) {
                return @{ status = 409; reason = "Conflict"; body = @{ status = "ONLINE"; action = $Action; result = "close_not_requested"; process_id = $ProcessId; message = "Windows did not accept the normal close request. No force termination was performed." } }
            }
            $null = $Process.WaitForExit(1000)
            if ($Process.HasExited) {
                Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=save_and_close Result=closed PID=$ProcessId"
                return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; action = $Action; result = "closed"; process_id = $ProcessId; message = "The application exited after its normal close handler."; force_kill = $false } }
            }
            Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=save_and_close Result=close_requested PID=$ProcessId"
            return @{ status = 202; reason = "Accepted"; body = @{ status = "ONLINE"; action = $Action; result = "close_requested"; process_id = $ProcessId; message = "Normal close requested. If the application displays its own Save prompt, choose Save on Windows, then check status here."; force_kill = $false; native_save_prompt_possible = $true } }
        }

        if ($Action -eq "close_without_saving") {
            # Explicit user confirmation is required; terminate only this exact verified PID.
            $Process.Kill()
            $null = $Process.WaitForExit(2500)

            if ($Process.HasExited) {
                Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=close_without_saving Result=closed PID=$ProcessId"
                return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; action = $Action; result = "closed"; process_id = $ProcessId; message = "The selected process exited after explicit force-close authorization."; force_kill = $true } }
            }
            return @{ status = 202; reason = "Accepted"; body = @{ status = "ONLINE"; action = $Action; result = "close_requested"; process_id = $ProcessId; message = "Force-close was requested, but the same process is still running. Check status again."; force_kill = $true } }
        }
    } catch {
        Write-Warning "WCCM selected-app action failed: $($_.Exception.GetType().Name)"
        return @{ status = 500; reason = "Internal Server Error"; body = @{ status = "ONLINE"; action = $Action; error = "application_action_failed"; process_id = $ProcessId; message = "The selected application action failed. Its current state must be checked." } }
    }
}

function Get-WccmProcessStatus {
    param([object]$Payload)

    $ProcessId = 0
    if (-not [int]::TryParse([string]$Payload.process_id, [ref]$ProcessId) -or $ProcessId -le 0) {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "invalid_process_id" } }
    }
    $Name = [string]$Payload.process_name
    $Started = [string]$Payload.process_started_utc
    if ([string]::IsNullOrWhiteSpace($Name) -or [string]::IsNullOrWhiteSpace($Started)) {
        return @{ status = 400; reason = "Bad Request"; body = @{ status = "ONLINE"; error = "invalid_process_identity" } }
    }

    try {
        $Current = Get-Process -Id $ProcessId -ErrorAction Stop
        $CurrentStarted = $Current.StartTime.ToUniversalTime().ToString("o")
        $CurrentSession = [int]$Current.SessionId
        $AgentSession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        $Same = ([string]$Current.ProcessName).Equals($Name, [System.StringComparison]::OrdinalIgnoreCase) -and
            $CurrentStarted.Equals($Started, [System.StringComparison]::Ordinal) -and
            $CurrentSession -eq $AgentSession
    } catch {
        if ($_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound) {
            return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; result = "closed"; process_id = $ProcessId; process_name = $Name; message = "Windows confirms the original process is no longer present." } }
        }
        return @{ status = 503; reason = "Service Unavailable"; body = @{ status = "ONLINE"; result = "unknown"; error = "process_status_unavailable"; process_id = $ProcessId; process_name = $Name; message = "Windows could not verify process status. Closed was not claimed." } }
    }

    if ($Same) {
        return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; result = "running"; process_id = $ProcessId; process_name = $Name; message = "The original process instance is still running." } }
    }
    return @{ status = 200; reason = "OK"; body = @{ status = "ONLINE"; result = "closed"; process_id = $ProcessId; process_name = $Name; message = "The original process instance is no longer running in this session." } }
}


if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "WCCM agent config not found: $ConfigPath. Existing P2 configuration must be present."
    exit 2
}
try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$Config.token) -or [string]::IsNullOrWhiteSpace([string]$Config.allowed_prefix)) {
        throw "Required token or allowed_prefix is missing from agent config."
    }
    $Port = [int]$Config.port
    if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid agent port." }
} catch {
    Write-Error "Could not load WCCM configuration: $($_.Exception.Message)"
    exit 2
}

$HibernateInteropLines = @(
    'using System;',
    'using System.Runtime.InteropServices;',
    'public static class WccmPowerHibernateNative {',
    '[StructLayout(LayoutKind.Sequential)] public struct LUID { public uint LowPart; public int HighPart; }',
    '[StructLayout(LayoutKind.Sequential)] public struct TOKEN_PRIVILEGES { public uint PrivilegeCount; public LUID Luid; public uint Attributes; }',
    '[DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();',
    '[DllImport("kernel32.dll", SetLastError=true)] private static extern bool CloseHandle(IntPtr hObject);',
    '[DllImport("advapi32.dll", SetLastError=true)] private static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);',
    '[DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern bool LookupPrivilegeValue(string lpSystemName, string lpName, out LUID lpLuid);',
    '[DllImport("advapi32.dll", SetLastError=true)] private static extern bool AdjustTokenPrivileges(IntPtr TokenHandle, bool DisableAllPrivileges, ref TOKEN_PRIVILEGES NewState, uint BufferLength, IntPtr PreviousState, IntPtr ReturnLength);',
    '[DllImport("powrprof.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.U1)] private static extern bool SetSuspendState([MarshalAs(UnmanagedType.U1)] bool bHibernate, [MarshalAs(UnmanagedType.U1)] bool bForce, [MarshalAs(UnmanagedType.U1)] bool bWakeupEventsDisabled);',
    'public static bool EnterHibernate(out int error) { IntPtr token=IntPtr.Zero; if (!OpenProcessToken(GetCurrentProcess(),0x20U|0x8U,out token)) { error=Marshal.GetLastWin32Error(); return false; } try { LUID luid; if (!LookupPrivilegeValue(null,"SeShutdownPrivilege",out luid)) { error=Marshal.GetLastWin32Error(); return false; } TOKEN_PRIVILEGES tp=new TOKEN_PRIVILEGES(); tp.PrivilegeCount=1; tp.Luid=luid; tp.Attributes=0x2; if (!AdjustTokenPrivileges(token,false,ref tp,0,IntPtr.Zero,IntPtr.Zero)) { error=Marshal.GetLastWin32Error(); return false; } error=Marshal.GetLastWin32Error(); if (error==1300) return false; bool ok=SetSuspendState(true,false,false); error=ok?0:Marshal.GetLastWin32Error(); return ok; } finally { CloseHandle(token); } }',
    '}'
)
$HibernateInteropSource = $HibernateInteropLines -join [Environment]::NewLine
Add-Type -TypeDefinition $HibernateInteropSource -Language CSharp -ErrorAction Stop

# WCCM_LAUNCHER_CATALOG_V1 â€” restricted Windows Start Menu application catalogue.
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
    param([switch]$ForceRefresh)

    # A catalogue GET can request a fresh scan. Only discovery results are cached.
    if (-not $ForceRefresh -and
        $script:WccmLauncherCacheAt -gt [DateTime]::UtcNow.AddSeconds(-30)) {
        return @($script:WccmLauncherCache)
    }

    $roots = @(
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
        (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs')
    ) | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Container)
    } | Select-Object -Unique

    $blocked = @(
        'cmd','powershell','pwsh','wscript','cscript','mshta','rundll32','regsvr32',
        'shutdown','taskkill','sc','net','format','diskpart','bcdedit','reg','regedit',
        'cmdkey','control','mmc','explorer','winlogon','services','msiexec','schtasks',
        'wmic','psexec','bash','wsl','ssh','ftp','bitsadmin','certutil'
    )

    $safeAdvanced = @('msinfo32','dxdiag','resmon')
    $denyTitle = '(?i)(^|\b)(uninstall|uninstaller|readme|help|support|website|documentation|repair|remove)(\b|$)'

    $shell = $null
    $items = New-Object System.Collections.Generic.List[object]
    $seenTargets = @{}

    try {
        $shell = New-Object -ComObject WScript.Shell -ErrorAction Stop
    } catch {
        return @()
    }

    try {
        foreach ($root in $roots) {
            $rootFull = [System.IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
            $links = @(
                Get-ChildItem -LiteralPath $rootFull -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue
            )

            foreach ($link in $links) {
                try {
                    $shortcutFull = [System.IO.Path]::GetFullPath($link.FullName)

                    if (-not $shortcutFull.StartsWith(
                        $rootFull, [System.StringComparison]::OrdinalIgnoreCase
                    )) { continue }

                    if ($link.BaseName -match $denyTitle) { continue }

                    $shortcut = $shell.CreateShortcut($shortcutFull)
                    $target = [Environment]::ExpandEnvironmentVariables([string]$shortcut.TargetPath)

                    if ([string]::IsNullOrWhiteSpace($target)) { continue }
                    if (-not [System.IO.Path]::IsPathRooted($target)) { continue }

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
                        $bytes = [System.Text.Encoding]::UTF8.GetBytes(
                            $shortcutFull.ToLowerInvariant()
                        )
                        $appId = ([BitConverter]::ToString(
                            $sha.ComputeHash($bytes)
                        )).Replace('-','').ToLowerInvariant()
                    } finally {
                        $sha.Dispose()
                    }

                    $relative = $shortcutFull.Substring($rootFull.Length)
                    $parts = $relative -split '\\'
                    $category = if ($parts.Count -gt 1) { $parts[0] } else { 'Applications' }

                    $appGroup = if ($base -in $safeAdvanced) { 'advanced' } else { 'main' }

                    $iconData = Get-WccmLauncherIconBase64 -TargetPath $target
                    if ($iconData.Length -gt 8000) { $iconData = '' }

                    $items.Add([pscustomobject]@{
                        app_id = $appId
                        name = [string]$link.BaseName
                        category = [string]$category
                        app_group = [string]$appGroup
                        target_path = [string]$target
                        icon_png_base64 = [string]$iconData
                        shortcut_path = [string]$shortcutFull
                    })

                    $seenTargets[$targetKey] = $true

                    if ($items.Count -ge 48) { break }
                } catch {
                    continue
                }
            }

            if ($items.Count -ge 48) { break }
        }
    } finally {
        if ($shell) {
            try {
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
            } catch {}
        }
    }

    $script:WccmLauncherCache = @($items | Sort-Object name)
    $script:WccmLauncherCacheAt = [DateTime]::UtcNow

    return @($script:WccmLauncherCache)
}


function Get-WccmLauncherProcessStates {
    param([object[]]$Entries)

    # Build one process inventory for all catalogue entries.
    # Only exact executable paths count as a verified match.
    $stateMap = @{}
    $targetBasenames = @{}

    foreach ($entry in $Entries) {
        try {
            $fullPath = [System.IO.Path]::GetFullPath([string]$entry.target_path)
            $key = $fullPath.ToLowerInvariant()
            $base = [System.IO.Path]::GetFileNameWithoutExtension($fullPath).ToLowerInvariant()

            if (-not $stateMap.ContainsKey($key)) {
                $stateMap[$key] = @{
                    running_count = 0
                    unknown = $false
                    state = 'NOT_RUNNING'
                }
            }

            if (-not $targetBasenames.ContainsKey($base)) {
                $targetBasenames[$base] = New-Object System.Collections.Generic.List[string]
            }

            if (-not $targetBasenames[$base].Contains($key)) {
                $targetBasenames[$base].Add($key)
            }
        } catch {
            continue
        }
    }

    $processes = @(Get-Process -ErrorAction SilentlyContinue)

    foreach ($process in $processes) {
        try {
            $modulePath = [string]$process.MainModule.FileName

            if ([string]::IsNullOrWhiteSpace($modulePath)) {
                throw "Executable path unavailable"
            }

            $actualKey = [System.IO.Path]::GetFullPath($modulePath).ToLowerInvariant()

            if ($stateMap.ContainsKey($actualKey)) {
                $stateMap[$actualKey].running_count =
                    [int]$stateMap[$actualKey].running_count + 1
            }
        } catch {
            # An inaccessible process with the same executable name makes identity unknown.
            $processBase = ([string]$process.ProcessName).ToLowerInvariant()

            if ($targetBasenames.ContainsKey($processBase)) {
                foreach ($key in $targetBasenames[$processBase]) {
                    $stateMap[$key].unknown = $true
                }
            }
        }
    }

    foreach ($key in @($stateMap.Keys)) {
        if ($stateMap[$key].unknown) {
            $stateMap[$key].state = 'UNKNOWN'
        } elseif ([int]$stateMap[$key].running_count -gt 0) {
            $stateMap[$key].state = 'RUNNING'
        } else {
            $stateMap[$key].state = 'NOT_RUNNING'
        }
    }

    return $stateMap
}


function Get-WccmLauncherPayload {
    # Every catalogue refresh deliberately bypasses the 30-second discovery cache.
    $entries = @(Get-WccmLauncherEntries -ForceRefresh)
    $processStates = Get-WccmLauncherProcessStates -Entries $entries

    $publicApps = @(
        foreach ($entry in $entries) {
            $key = ([System.IO.Path]::GetFullPath(
                [string]$entry.target_path
            )).ToLowerInvariant()

            $state = $processStates[$key]

            [pscustomobject]@{
                app_id = $entry.app_id
                name = $entry.name
                category = $entry.category
                app_group = $entry.app_group
                icon_png_base64 = $entry.icon_png_base64
                running_state = [string]$state.state
                running_count = [int]$state.running_count
            }
        }
    )

    return [pscustomobject]@{
        schema_version = 1
        catalog_version = 2
        status = 'ONLINE'
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        computer_name = $env:COMPUTERNAME
        inventory_scope = 'Up to 48 supported Start Menu EXE/COM shortcuts. Running state is based on exact executable path; running_count is process count, not window count. Advanced Tools are limited to discovered approved diagnostic shortcuts.'
        app_count = $publicApps.Count
        apps = $publicApps
    }
}


function Invoke-WccmLauncherOpen {
    param([object]$Payload)

    $properties = @()

    if ($Payload -is [System.Collections.IDictionary]) {
        $properties = @($Payload.Keys)
    } elseif ($Payload -is [PSCustomObject]) {
        $properties = @($Payload.PSObject.Properties.Name)
    }

    if (
        $properties.Count -ne 1 -or
        $properties[0] -ne 'app_id' -or
        [string]$Payload.app_id -notmatch '^[a-f0-9]{64}$'
    ) {
        return @{
            status = 400
            reason = 'Bad Request'
            body = @{ status = 'ONLINE'; error = 'invalid_launcher_app_id' }
        }
    }

    $entries = @(Get-WccmLauncherEntries -ForceRefresh)
    $match = @($entries | Where-Object { $_.app_id -ceq [string]$Payload.app_id })

    if ($match.Count -ne 1) {
        return @{
            status = 404
            reason = 'Not Found'
            body = @{
                status = 'ONLINE'
                error = 'launcher_app_not_found'
                message = 'That Start Menu app is no longer in the current launch catalogue. Refresh the catalogue.'
            }
        }
    }

    $entry = $match[0]

    if (
        -not (Test-Path -LiteralPath $entry.shortcut_path -PathType Leaf) -or
        -not (Test-Path -LiteralPath $entry.target_path -PathType Leaf)
    ) {
        $script:WccmLauncherCacheAt = [DateTime]::MinValue

        return @{
            status = 404
            reason = 'Not Found'
            body = @{
                status = 'ONLINE'
                error = 'launcher_target_missing'
                message = 'The selected shortcut or executable no longer exists. Refresh the catalogue.'
            }
        }
    }

    $processStates = Get-WccmLauncherProcessStates -Entries $entries
    $key = ([System.IO.Path]::GetFullPath(
        [string]$entry.target_path
    )).ToLowerInvariant()

    $state = $processStates[$key]

    if ($state.state -eq 'RUNNING') {
        return @{
            status = 409
            reason = 'Conflict'
            body = @{
                status = 'ONLINE'
                error = 'launcher_already_running'
                running_count = [int]$state.running_count
                message = "$($entry.name) is already running. Duplicate launch was prevented."
            }
        }
    }

    if ($state.state -eq 'UNKNOWN') {
        return @{
            status = 409
            reason = 'Conflict'
            body = @{
                status = 'ONLINE'
                error = 'launcher_process_state_unknown'
                message = 'Windows could not verify this application process safely. Check the Windows PC before launching again.'
            }
        }
    }

    try {
        # Preserve the original restricted Start Menu shortcut launch mechanism.
        $process = Start-Process -FilePath $entry.shortcut_path -PassThru -ErrorAction Stop
        $processId = $null

        if ($process) { $processId = [int]$process.Id }

        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=launcher_open Result=requested AppId=$($entry.app_id.Substring(0,12))"

        return @{
            status = 202
            reason = 'Accepted'
            body = @{
                status = 'ONLINE'
                action = 'launcher_open'
                result = 'requested'
                app_id = $entry.app_id
                app_name = $entry.name
                process_id = $processId
                message = "Launch requested for $($entry.name). Refresh the App Library to verify its process status."
            }
        }
    } catch {
        Write-Warning "WCCM Start Menu launch failed: $($_.Exception.GetType().Name)"

        return @{
            status = 500
            reason = 'Internal Server Error'
            body = @{
                status = 'ONLINE'
                error = 'launcher_start_failed'
                message = 'Windows could not open the selected Start Menu shortcut.'
            }
        }
    }
}

$Listener = $null
try {
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
    $Listener.Start()
} catch {
    Write-Error "WCCM P4 agent could not bind to TCP ${Port}: $($_.Exception.Message)"
    exit 3
}

Write-Output "WCCM Windows Agent - P6.3 (P6.2 application management, restart, shutdown and Hibernate)"
Write-Output "Listening on TCP $Port; permitted IPv4 prefix: $($Config.allowed_prefix)*"
Write-Output "Allowed control scope: P4 tracked Notepad control; explicit selected-app Save & Close and force-close by verified PID/start-time/session."
Write-Output "No arbitrary commands, no firewall or scheduled-task changes. Selected-app close requires exact process identity and explicit action confirmation; Restart and Shutdown require the manual Save Review gate and use /f. Sleep uses native Windows S3 suspend and preserves wake events."
Write-Output "Keep this console open during testing. Press Ctrl+C to stop."


function Get-WccmDefenderStatusPayload {
    $Now = [DateTime]::UtcNow
    try {
        $Status = Get-MpComputerStatus -ErrorAction Stop
        $QuickAge = $null
        $FullAge = $null

        if ($null -ne $Status.QuickScanAge -and [int64]$Status.QuickScanAge -ne 4294967295) {
            $QuickAge = [int]$Status.QuickScanAge
        }
        if ($null -ne $Status.FullScanAge -and [int64]$Status.FullScanAge -ne 4294967295) {
            $FullAge = [int]$Status.FullScanAge
        }

        $SigUpdated = $null
        if ($Status.AntivirusSignatureLastUpdated -is [DateTime]) {
            $SigUpdated = $Status.AntivirusSignatureLastUpdated.ToUniversalTime().ToString("o")
        }

        return [PSCustomObject]@{
            schema_version = 1
            status = "AVAILABLE"
            source = "Microsoft Defender Get-MpComputerStatus"
            timestamp_utc = $Now.ToString("o")
            protection = [PSCustomObject]@{
                antivirus_service_enabled = [bool]$Status.AMServiceEnabled
                antivirus_enabled = [bool]$Status.AntivirusEnabled
                antispyware_enabled = [bool]$Status.AntispywareEnabled
                real_time_protection_enabled = [bool]$Status.RealTimeProtectionEnabled
                behavior_monitor_enabled = [bool]$Status.BehaviorMonitorEnabled
                ioav_protection_enabled = [bool]$Status.IoavProtectionEnabled
                network_inspection_enabled = [bool]$Status.NISEnabled
            }
            signatures = [PSCustomObject]@{
                antivirus_signature_version = [string]$Status.AntivirusSignatureVersion
                last_updated_utc = $SigUpdated
            }
            scans = [PSCustomObject]@{
                quick_scan_age_days = $QuickAge
                full_scan_age_days = $FullAge
            }
        }
    }
    catch {
        return [PSCustomObject]@{
            schema_version = 1
            status = "UNAVAILABLE"
            source = "Microsoft Defender Get-MpComputerStatus"
            timestamp_utc = $Now.ToString("o")
            message = "Defender status query could not be completed."
            error_code = "DEFENDER_QUERY_FAILED"
        }
    }
}
$GetPaths = @("/api/health", "/api/apps", "/api/p4/test-app", "/api/launcher/apps", "/api/defender/status")
$PostPaths = @("/api/p4/launch", "/api/p4/close", "/api/apps/action", "/api/apps/process-status", "/api/power/restart", "/api/power/shutdown", "/api/power/hibernate", "/api/launcher/open")
$KnownPaths = @($GetPaths + $PostPaths)

try {
    while ($true) {
        $Client = $null; $Stream = $null; $Reader = $null
        try {
            $Client = $Listener.AcceptTcpClient()
            $Client.ReceiveTimeout = 4000
            $Client.SendTimeout = 4000
            $RemoteAddress = $Client.Client.RemoteEndPoint.Address.ToString()
            $Stream = $Client.GetStream()
            $Reader = New-Object System.IO.StreamReader($Stream, [System.Text.Encoding]::ASCII, $false, 1024, $true)

            $RequestLine = $Reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($RequestLine)) { continue }
            $Parts = $RequestLine.Split(' ')
            if ($Parts.Count -lt 2) {
                Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "bad_request" }
                continue
            }
            $Method = $Parts[0].ToUpperInvariant()
            $Path = ($Parts[1] -split '\?', 2)[0]
            $Headers = @{}
            $HeaderCount = 0
            while ($true) {
                $Line = $Reader.ReadLine()
                if ($null -eq $Line -or $Line.Length -eq 0) { break }
                $HeaderCount++
                if ($HeaderCount -gt 50) { break }
                $Colon = $Line.IndexOf(':')
                if ($Colon -gt 0) {
                    $HeaderName = $Line.Substring(0, $Colon).Trim()
                    $HeaderValue = $Line.Substring($Colon + 1).Trim()
                    $Headers[$HeaderName] = $HeaderValue
                }
            }

            if (-not $RemoteAddress.StartsWith([string]$Config.allowed_prefix)) {
                Write-JsonResponse -Stream $Stream -StatusCode 403 -Reason "Forbidden" -Payload @{ error = "client_not_allowed" }
                continue
            }
            if ($Path -notin $KnownPaths) {
                Write-JsonResponse -Stream $Stream -StatusCode 404 -Reason "Not Found" -Payload @{ error = "not_found" }
                continue
            }
            if ($Method -notin @("GET", "POST")) {
                Write-JsonResponse -Stream $Stream -StatusCode 405 -Reason "Method Not Allowed" -Payload @{ error = "method_not_allowed" }
                continue
            }
            if (($Method -eq "GET" -and $Path -in $PostPaths) -or ($Method -eq "POST" -and $Path -in $GetPaths)) {
                Write-JsonResponse -Stream $Stream -StatusCode 405 -Reason "Method Not Allowed" -Payload @{ error = "method_not_allowed"; path = $Path }
                continue
            }
            if (-not (Test-ConstantTimeEquals -Left ([string]$Headers["Authorization"]) -Right ("Bearer " + [string]$Config.token))) {
                Write-JsonResponse -Stream $Stream -StatusCode 401 -Reason "Unauthorized" -Payload @{ error = "unauthorized" }
                continue
            }

            if ($Method -eq "GET" -and $Path -eq "/api/launcher/apps") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmLauncherPayload)
                continue
            }
            if ($Method -eq "GET" -and $Path -eq "/api/defender/status") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmDefenderStatusPayload)
                continue
            }            if ($Method -eq "GET" -and $Path -eq "/api/health") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmHealthPayload)
                continue
            }
            if ($Method -eq "GET" -and $Path -eq "/api/apps") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmAppsPayload)
                continue
            }
            if ($Method -eq "GET" -and $Path -eq "/api/p4/test-app") {
                Write-JsonResponse -Stream $Stream -StatusCode 200 -Reason "OK" -Payload (Get-WccmP4TestAppPayload)
                continue
            }

            if ($Method -eq "POST") {
                $ContentLength = 0
                if (-not [int]::TryParse([string]$Headers["Content-Length"], [ref]$ContentLength) -or $ContentLength -lt 0) {
                    Write-JsonResponse -Stream $Stream -StatusCode 411 -Reason "Length Required" -Payload @{ error = "content_length_required" }
                    continue
                }
                if ($ContentLength -gt 4096) {
                    Write-JsonResponse -Stream $Stream -StatusCode 413 -Reason "Payload Too Large" -Payload @{ error = "request_too_large" }
                    continue
                }
                if (-not ([string]$Headers["Content-Type"]).StartsWith("application/json", [System.StringComparison]::OrdinalIgnoreCase)) {
                    Write-JsonResponse -Stream $Stream -StatusCode 415 -Reason "Unsupported Media Type" -Payload @{ error = "application_json_required" }
                    continue
                }
                $BodyText = ""
                if ($ContentLength -gt 0) {
                    $CharBuffer = New-Object 'System.Char[]' $ContentLength
                    $ReadTotal = 0
                    while ($ReadTotal -lt $ContentLength) {
                        $ReadNow = $Reader.Read($CharBuffer, $ReadTotal, $ContentLength - $ReadTotal)
                        if ($ReadNow -le 0) { break }
                        $ReadTotal += $ReadNow
                    }
                    if ($ReadTotal -ne $ContentLength) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "incomplete_request_body" }
                        continue
                    }
                    $BodyText = -join $CharBuffer
                }
                try {
                    $Payload = $BodyText | ConvertFrom-Json -ErrorAction Stop
                } catch {
                    Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_json" }
                    continue
                }
                if ($Path -eq "/api/launcher/open") {
                    $Result = Invoke-WccmLauncherOpen -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }                if ($Path -eq "/api/p4/launch") {
                    $Result = Invoke-WccmP4Launch -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }
                if ($Path -eq "/api/p4/close") {
                    $Result = Invoke-WccmP4Close -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }
                if ($Path -eq "/api/apps/action") {
                    $ExpectedProperties = @("action", "process_id", "process_name", "process_started_utc", "confirmation")
                    $ActualProperties = @($Payload.PSObject.Properties.Name | Sort-Object)
                    if ($Payload -is [System.Collections.IDictionary]) { $ActualProperties = @($Payload.Keys | Sort-Object) }
                    if ($ActualProperties.Count -ne $ExpectedProperties.Count -or
                        (@(Compare-Object ($ExpectedProperties | Sort-Object) $ActualProperties).Count -ne 0)) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_app_action_schema" }
                        continue
                    }
                    $Result = Invoke-WccmAppAction -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }
                if ($Path -eq "/api/apps/process-status") {
                    $ExpectedProperties = @("process_id", "process_name", "process_started_utc")
                    $ActualProperties = @($Payload.PSObject.Properties.Name | Sort-Object)
                    if ($Payload -is [System.Collections.IDictionary]) { $ActualProperties = @($Payload.Keys | Sort-Object) }
                    if ($ActualProperties.Count -ne $ExpectedProperties.Count -or
                        (@(Compare-Object ($ExpectedProperties | Sort-Object) $ActualProperties).Count -ne 0)) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_process_status_schema" }
                        continue
                    }
                    $Result = Get-WccmProcessStatus -Payload $Payload
                    Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                    continue
                }
                if ($Path -eq "/api/power/restart") {
                    if (-not ($Payload -is [System.Collections.IDictionary]) -and $Payload -isnot [PSCustomObject]) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_restart_schema" }
                        continue
                    }
                    $RestartProperties = @($Payload.PSObject.Properties.Name)
                    if ($Payload -is [System.Collections.IDictionary]) { $RestartProperties = @($Payload.Keys) }
                    if ($RestartProperties.Count -ne 1 -or [string]$Payload.confirmation -ne "RESTART_WINDOWS_PC") {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "explicit_restart_confirmation_required" }
                        continue
                    }
                    try {
                        # Start a short delayed restart only after authenticated, explicit confirmation.
                        # /f force-closes remaining applications to suppress the Windows app-blocker prompt. Manual Save Review remains mandatory.
                        $RestartArgs = '/r /t 0 /f /d p:4:1 /c "WCCM authenticated Mac dashboard restart"'
                        $null = Start-Process -FilePath (Join-Path $env:WINDIR "System32\shutdown.exe") -ArgumentList $RestartArgs -WindowStyle Hidden -PassThru -ErrorAction Stop
                        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=restart Result=requested Source=authenticated_WCCM_dashboard"
                        Write-JsonResponse -Stream $Stream -StatusCode 202 -Reason "Accepted" -Payload @{ status = "ONLINE"; action = "restart"; result = "requested"; message = "Windows restart was requested by the authenticated Mac dashboard. Windows will force-close remaining applications and may disconnect during restart."; force_close = $true }
                    } catch {
                        Write-Warning "WCCM restart request failed: $($_.Exception.GetType().Name)"
                        Write-JsonResponse -Stream $Stream -StatusCode 500 -Reason "Internal Server Error" -Payload @{ error = "restart_request_failed"; message = "Windows did not accept the restart request." }
                    }
                    continue
                }
                if ($Path -eq "/api/power/hibernate") {
                    if (-not ($Payload -is [System.Collections.IDictionary]) -and $Payload -isnot [PSCustomObject]) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_hibernate_schema" }
                        continue
                    }
                    $HibernateProperties = @($Payload.PSObject.Properties.Name)
                    if ($Payload -is [System.Collections.IDictionary]) { $HibernateProperties = @($Payload.Keys) }
                    if ($HibernateProperties.Count -ne 1 -or [string]$Payload.confirmation -ne "HIBERNATE_WINDOWS_PC") {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "explicit_hibernate_confirmation_required" }
                        continue
                    }
                    Write-JsonResponse -Stream $Stream -StatusCode 202 -Reason "Accepted" -Payload @{ status = "ONLINE"; action = "hibernate"; result = "requested"; message = "Windows Hibernate (S4) requested. Applications remain open; wake events remain enabled."; force_close = $false }
                    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=hibernate Result=requested"
                    Start-Sleep -Milliseconds 900
                    try {
                        $HibernateError = 0
                        $HibernateResult = [WccmPowerHibernateNative]::EnterHibernate([ref]$HibernateError)
                        if ($HibernateResult) {
                            Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=hibernate Result=resumed"
                        } else {
                            Write-Warning "WCCM Sleep did not suspend Windows. Win32Error=$HibernateError"
                        }
                    } catch {
                        Write-Warning "WCCM Hibernate transition failed: $($_.Exception.GetType().Name)"
                    }
                    continue
                }                if ($Path -eq "/api/power/shutdown") {
                    if (-not ($Payload -is [System.Collections.IDictionary]) -and $Payload -isnot [PSCustomObject]) {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "invalid_shutdown_schema" }
                        continue
                    }
                    $ShutdownProperties = @($Payload.PSObject.Properties.Name)
                    if ($Payload -is [System.Collections.IDictionary]) { $ShutdownProperties = @($Payload.Keys) }
                    if ($ShutdownProperties.Count -ne 1 -or [string]$Payload.confirmation -ne "SHUTDOWN_WINDOWS_PC") {
                        Write-JsonResponse -Stream $Stream -StatusCode 400 -Reason "Bad Request" -Payload @{ error = "explicit_shutdown_confirmation_required" }
                        continue
                    }
                    try {
                        # Full shutdown; /f closes remaining applications after the Mac-side review gate.
                        $ShutdownArgs = '/s /t 0 /f /d p:4:1 /c "WCCM authenticated Mac dashboard shutdown"'
                        $null = Start-Process -FilePath (Join-Path $env:WINDIR "System32\shutdown.exe") -ArgumentList $ShutdownArgs -WindowStyle Hidden -PassThru -ErrorAction Stop
                        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Action=shutdown Result=requested Source=authenticated_WCCM_dashboard"
                        Write-JsonResponse -Stream $Stream -StatusCode 202 -Reason "Accepted" -Payload @{ status = "ONLINE"; action = "shutdown"; result = "requested"; message = "Windows full shutdown was requested by the authenticated Mac dashboard. The Agent will go offline until the PC is physically powered on and Windows sign-in completes."; force_close = $true }
                    } catch {
                        Write-Warning "WCCM shutdown request failed: $($_.Exception.GetType().Name)"
                        Write-JsonResponse -Stream $Stream -StatusCode 500 -Reason "Internal Server Error" -Payload @{ error = "shutdown_request_failed"; message = "Windows did not accept the shutdown request." }
                    }
                    continue
                }
            }
        } catch {
            Write-Warning "WCCM P4 request failed: $($_.Exception.GetType().Name)"
        } finally {
            if ($Reader) { $Reader.Dispose() }
            if ($Stream) { $Stream.Dispose() }
            if ($Client) { $Client.Close() }
        }
    }
} finally {
    if ($Listener) { $Listener.Stop() }
    Write-Output "WCCM P4 agent stopped."
}

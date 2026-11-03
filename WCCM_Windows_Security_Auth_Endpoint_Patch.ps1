$ErrorActionPreference = "Stop"
$AgentPath = Join-Path $env:LOCALAPPDATA "WCCM\wccm_windows_agent.ps1"
$ExpectedSha256 = "AF05DC60128656482A140ABDED388B07BAB22E3417F3DC3071700A32C1E20B50"
$TaskName = "WCCM Agent AutoStart"
if (-not (Test-Path -LiteralPath $AgentPath -PathType Leaf)) { throw "Agent source not found." }
$OriginalHash = (Get-FileHash -LiteralPath $AgentPath -Algorithm SHA256).Hash.ToUpperInvariant()
if ($OriginalHash -ne $ExpectedSha256) { throw "Source hash mismatch; no changes made. Found $OriginalHash" }
$Source = [IO.File]::ReadAllText($AgentPath)
$GetPathsOld = '$GetPaths = @("/api/health", "/api/apps", "/api/p4/test-app", "/api/launcher/apps", "/api/defender/status")'
if (-not $Source.Contains($GetPathsOld)) { throw "GET allowlist anchor missing." }
if ($Source.Contains("/api/security/auth-events")) { throw "Endpoint already exists; refusing duplicate patch." }

$FunctionBlock = @'
function Get-WccmSecurityAuthEventsPayload {
    param([string]$RequestTarget)
    try {
        $Query = @{}
        $Question = $RequestTarget.IndexOf("?")
        if ($Question -ge 0 -and $Question -lt ($RequestTarget.Length - 1)) {
            foreach ($Pair in $RequestTarget.Substring($Question + 1).Split("&")) {
                if ([string]::IsNullOrWhiteSpace($Pair)) { continue }
                $Parts = $Pair.Split("=", 2)
                $Key = [Uri]::UnescapeDataString($Parts[0].Replace("+", " "))
                $Value = if ($Parts.Count -gt 1) { [Uri]::UnescapeDataString($Parts[1].Replace("+", " ")) } else { "" }
                if ($Key -notin @("since_utc", "after_record_id") -or $Query.ContainsKey($Key)) {
                    return @{ status = 400; reason = "Bad Request"; body = @{ status = "ERROR"; error = "invalid_query_parameter" } }
                }
                $Query[$Key] = $Value
            }
        }
        $SinceUtc = [DateTime]::UtcNow.AddHours(-24)
        if ($Query.ContainsKey("since_utc")) {
            $ParsedSince = [DateTimeOffset]::MinValue
            if (-not [DateTimeOffset]::TryParse([string]$Query["since_utc"], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$ParsedSince)) {
                return @{ status = 400; reason = "Bad Request"; body = @{ status = "ERROR"; error = "invalid_since_utc" } }
            }
            $SinceUtc = $ParsedSince.UtcDateTime
        }
        $NowUtc = [DateTime]::UtcNow
        if ($SinceUtc -gt $NowUtc.AddMinutes(5) -or $SinceUtc -lt $NowUtc.AddDays(-7)) {
            return @{ status = 400; reason = "Bad Request"; body = @{ status = "ERROR"; error = "since_utc_out_of_range"; max_lookback_days = 7 } }
        }
        [long]$AfterRecordId = 0
        if ($Query.ContainsKey("after_record_id")) {
            if (-not [long]::TryParse([string]$Query["after_record_id"], [ref]$AfterRecordId) -or $AfterRecordId -lt 0) {
                return @{ status = 400; reason = "Bad Request"; body = @{ status = "ERROR"; error = "invalid_after_record_id" } }
            }
        }
        # XPath 1.0 cannot reliably compare ISO-8601 date strings; establish a RecordID boundary first.
        if ($AfterRecordId -eq 0) {
            $Prior = Get-WinEvent -FilterHashtable @{ LogName = "Security"; ID = @(4624,4625); EndTime = $SinceUtc } -MaxEvents 1 -ErrorAction SilentlyContinue
            if ($null -ne $Prior) { $AfterRecordId = [long]$Prior.RecordId }
        }
        $XPath = "*[System[(EventID=4624 or EventID=4625) and EventRecordID > $AfterRecordId]]"
        $Records = @(Get-WinEvent -LogName Security -FilterXPath $XPath -Oldest -MaxEvents 500 -ErrorAction Stop)
        $Records = @($Records | Where-Object { $_.TimeCreated.ToUniversalTime() -ge $SinceUtc })
        $Events = @()
        foreach ($Record in $Records) {
            $Xml = [xml]$Record.ToXml()
            $Data = @{}
            foreach ($Item in @($Xml.Event.EventData.Data)) {
                if ($null -ne $Item -and $Item.Name) { $Data[$Item.GetAttribute("Name")] = [string]$Item.InnerText }
            }
            $EventId = [int]$Record.Id
            $TimeUtc = ([DateTime]$Record.TimeCreated).ToUniversalTime().ToString("o")
            $Ip = [string]$Data["IpAddress"]
            $Events += [PSCustomObject]@{
                timestamp = $TimeUtc; timestamp_utc = $TimeUtc
                severity = $(if ($EventId -eq 4625) { "High" } else { "Info" })
                title = $(if ($EventId -eq 4625) { "Windows sign-in failure" } else { "Windows sign-in success" })
                category = "Authentication"; source = "Windows Event Log"
                device = [string]$Xml.Event.System.Computer
                event_id = $EventId; record_id = [long]$Record.RecordId
                channel = "Security"; provider = [string]$Xml.Event.System.Provider.Name
                source_ip = $Ip; message = [string]$Record.Message
                event_data = @{
                    LogonType = [string]$Data["LogonType"]
                    TargetUserName = [string]$Data["TargetUserName"]
                    TargetDomainName = [string]$Data["TargetDomainName"]
                    WorkstationName = [string]$Data["WorkstationName"]
                    IpAddress = $Ip
                    ProcessName = [string]$Data["ProcessName"]
                    Status = [string]$Data["Status"]
                    SubStatus = [string]$Data["SubStatus"]
                }
                provenance = "real_source"; is_synthetic = $false; includes_test_data = $false
            }
        }
        return @{ status = 200; reason = "OK"; body = @{
            status = "OK"; source = "Windows Security Event Log"; computer_name = $env:COMPUTERNAME
            channel = "Security"; event_ids = @(4624,4625); count = $Events.Count; events = @($Events)
            returned_at_utc = [DateTime]::UtcNow.ToString("o")
        } }
    } catch {
        return @{ status = 503; reason = "Service Unavailable"; body = @{ status = "UNAVAILABLE"; error = "security_event_log_unavailable"; message = "Windows Security Event Log could not be queried." } }
    }
}
'@

$Source = $Source.Replace($GetPathsOld, '$GetPaths = @("/api/health", "/api/apps", "/api/p4/test-app", "/api/launcher/apps", "/api/defender/status", "/api/security/auth-events")')
$Index = $Source.IndexOf('$GetPaths = @(', [StringComparison]::Ordinal)
if ($Index -lt 0) { throw "GET allowlist insertion point missing." }
$Source = $Source.Insert($Index, $FunctionBlock + [Environment]::NewLine)
$DispatchAnchor = '            if ($Method -eq "GET" -and $Path -eq "/api/launcher/apps") {'
$Dispatch = @'
            if ($Method -eq "GET" -and $Path -eq "/api/security/auth-events") {
                $Result = Get-WccmSecurityAuthEventsPayload -RequestTarget ([string]$Parts[1])
                Write-JsonResponse -Stream $Stream -StatusCode $Result.status -Reason $Result.reason -Payload $Result.body
                continue
            }
'@
if (-not $Source.Contains($DispatchAnchor)) { throw "GET dispatch anchor missing." }
$Source = $Source.Replace($DispatchAnchor, $Dispatch + $DispatchAnchor)
$TempPath = "$AgentPath.security-new"
$BackupPath = "$AgentPath.backup_security_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
[IO.File]::WriteAllText($TempPath, $Source, (New-Object Text.UTF8Encoding($false)))
$Tokens = $null; $ParseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile($TempPath, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { Remove-Item $TempPath -Force; throw "Parser rejected patch: $($ParseErrors[0].Message)" }
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Copy-Item -LiteralPath $AgentPath -Destination $BackupPath -ErrorAction Stop
$WasRunning = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State -eq "Running"
if ($WasRunning) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop; Start-Sleep -Seconds 2 }
try {
    Move-Item -LiteralPath $TempPath -Destination $AgentPath -Force
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Start-Sleep -Seconds 3
    $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
    $WaitUntil = (Get-Date).AddSeconds(15)
    while ($TaskState -ne "Running" -and (Get-Date) -lt $WaitUntil) {
        Start-Sleep -Milliseconds 500
        $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
    }
    if ($TaskState -ne "Running") { throw "Agent task did not return to Running state after 15 seconds: $TaskState" }
} catch {
    Copy-Item -LiteralPath $BackupPath -Destination $AgentPath -Force
    try { Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    throw
}
[pscustomobject]@{
    result = "PATCH_INSTALLED"; backup = $BackupPath
    original_sha256 = $OriginalHash
    patched_sha256 = (Get-FileHash -LiteralPath $AgentPath -Algorithm SHA256).Hash
    task_state = $TaskState
    endpoint = "GET /api/security/auth-events?since_utc=<UTC>&after_record_id=<integer>"
    scope = "Security channel, Event IDs 4624/4625, max 500, max lookback 7 days"
}

param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "stop", "restart", "status")]
    [string]$Command = "start",

    [string]$ChatId
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $PSScriptRoot "codex_lark_bridge.py"
$WorkspaceRoot = (Get-Location).Path
$StatePath = Join-Path $BridgeRoot ".lark-events\bridge-state.json"

function Get-CodexLarkPidProcesses {
    param([string]$StatePath)

    if (-not (Test-Path -LiteralPath $StatePath)) {
        return @()
    }

    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $state.processes) {
            return @()
        }

        $ids = @(
            $state.processes.lark
            $state.processes.codex
            $state.processes.bridge
        ) | Where-Object { $_ } | Select-Object -Unique

        return $ids | ForEach-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Warning "failed to read pid state: $_"
        return @()
    }
}

function Get-ProcessTree {
    param([int[]]$RootIds)

    $allProcesses = @()
    try {
        $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId, Name)
    } catch {
        $allProcesses = @()
    }

    $seen = @{}
    $ordered = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    foreach ($id in $RootIds) {
        if ($id -and -not $seen.ContainsKey($id)) {
            $seen[$id] = $true
            $ordered.Add($id)
            $queue.Enqueue($id)
        }
    }

    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in ($allProcesses | Where-Object { $_.ParentProcessId -eq $parentId })) {
            $childId = [int]$child.ProcessId
            if (-not $seen.ContainsKey($childId)) {
                $seen[$childId] = $true
                $ordered.Add($childId)
                $queue.Enqueue($childId)
            }
        }
    }

    [array]::Reverse($ordered)
    return $ordered
}

function Clear-CodexLarkPidState {
    param([string]$StatePath)

    if (-not (Test-Path -LiteralPath $StatePath)) {
        return
    }

    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $state.processes -and -not $state.workspace_root) {
            return
        }
        $state.PSObject.Properties.Remove("processes")
        $state.PSObject.Properties.Remove("workspace_root")
        $json = $state | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText($StatePath, $json, [System.Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "failed to clear pid state: $_"
    }
}

function Get-CodexLarkProcesses {
    param([string]$BridgeRoot)

    $escapedBridge = [regex]::Escape($BridgeRoot)
    try {
        return Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.CommandLine -match "$escapedBridge.*codex_lark_bridge\.py" -or
            $_.CommandLine -match 'im\.message\.receive_v1' -or
            ($_.CommandLine -match 'app-server' -and $_.CommandLine -match 'ws://127\.0\.0\.1:17345')
        }
    } catch {
        $script:CodexLarkProcessQueryFailed = $_
        return @()
    }
}

function Stop-CodexLark {
    $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $StatePath)
    if ($pidProcs) {
        $stopIds = @(Get-ProcessTree -RootIds @($pidProcs | ForEach-Object { $_.Id }))
        if (-not $stopIds) {
            $stopIds = @($pidProcs | ForEach-Object { $_.Id })
        }
        $stopIds | ForEach-Object {
            try {
                $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
                if ($proc) {
                    Stop-Process -Id $proc.Id -Force -ErrorAction Stop
                    Write-Host "stopped $($proc.Id) $($proc.ProcessName)"
                }
            } catch {
                Write-Warning "failed to stop $($_): $_"
            }
        }
        Clear-CodexLarkPidState -StatePath $StatePath
        return
    }

    $procs = Get-CodexLarkProcesses -BridgeRoot $BridgeRoot
    if ($script:CodexLarkProcessQueryFailed) {
        Write-Host "codex-lark process details are unavailable: $script:CodexLarkProcessQueryFailed"
        Write-Host "no pid state was found; start codex-lark again once to enable pid-based stop"
        return
    }
    if (-not $procs) {
        Write-Host "codex-lark is stopped"
        return
    }
    $procs | ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "stopped $($_.ProcessId) $($_.Name)"
        } catch {
            Write-Warning "failed to stop $($_.ProcessId): $_"
        }
    }
    return
}

if ($Command -eq "stop") {
    Stop-CodexLark
    return
}

if ($Command -eq "restart") {
    Stop-CodexLark
    Start-Sleep -Seconds 1
    $Command = "start"
}

if ($Command -eq "status") {
    $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $StatePath)
    if ($pidProcs) {
        Write-Host "codex-lark is running"
        $pidProcs | Select-Object Id, ProcessName, Path | Format-Table -AutoSize
        return
    }

    $procs = Get-CodexLarkProcesses -BridgeRoot $BridgeRoot
    if ($script:CodexLarkProcessQueryFailed) {
        try {
            $ready = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:17345/readyz" -TimeoutSec 2
            if ($ready.StatusCode -eq 200) {
                Write-Host "codex-lark status is partially available"
                Write-Host "codex app-server is reachable at ws://127.0.0.1:17345"
                Write-Host "process details are unavailable in this shell: $script:CodexLarkProcessQueryFailed"
                return
            }
        } catch {
        }
        Write-Host "codex-lark status is unavailable"
        Write-Host "process details are unavailable in this shell: $script:CodexLarkProcessQueryFailed"
        return
    }
    if (-not $procs) {
        Write-Host "codex-lark is stopped"
        return
    }
    Write-Host "codex-lark is running"
    $procs | Select-Object ProcessId, Name, CommandLine | Format-Table -AutoSize
    return
}

Push-Location $BridgeRoot
try {
    if ($Command -eq "init") {
        if ($ChatId) {
            python $Script init --chat-id $ChatId
        } else {
            python $Script init
        }
        return
    }

    python $Script run --workspace-root $WorkspaceRoot
}
finally {
    Pop-Location
}

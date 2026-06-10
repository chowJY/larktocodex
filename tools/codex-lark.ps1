param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "start-all", "stop", "stop-all", "restart", "status", "status-all")]
    [string]$Command = "start",

    [string]$ChatId,
    [string]$ProjectName,
    [string]$WorkspaceRoot,
    [string]$CodexWsUrl
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $PSScriptRoot "codex_lark_bridge.py"
$CurrentWorkspaceRoot = if ($WorkspaceRoot) { (Resolve-Path -LiteralPath $WorkspaceRoot).Path } else { (Get-Location).Path }
$StatePath = Join-Path $BridgeRoot ".lark-events\bridge-state.json"
$ProjectsConfigPath = Join-Path $BridgeRoot ".lark-events\projects-config.json"

function ConvertTo-CodexLarkSafeName {
    param([string]$Name)
    return (($Name.Trim()) -replace '[^A-Za-z0-9_.-]', '_')
}

function Get-CodexLarkProjects {
    if (-not (Test-Path -LiteralPath $ProjectsConfigPath)) {
        return @()
    }
    $config = Get-Content -LiteralPath $ProjectsConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $config.projects) {
        return @()
    }
    return @($config.projects)
}

function Get-CodexLarkProjectStatePath {
    param([string]$Name)
    if (-not $Name) {
        return $StatePath
    }
    $safeName = ConvertTo-CodexLarkSafeName -Name $Name
    return Join-Path $BridgeRoot ".lark-events\projects\$safeName\bridge-state.json"
}

function Test-CodexLarkAutoWsUrl {
    param([string]$Value)
    return (-not $Value) -or $Value.Trim().ToLowerInvariant() -in @("auto", "auto-port")
}

function Get-CodexLarkWsPort {
    param([string]$WsUrl)
    if (-not $WsUrl) {
        return 0
    }
    try {
        $uri = [System.Uri]$WsUrl
        return [int]$uri.Port
    } catch {
        return 0
    }
}

function Get-CodexLarkFreeWsUrl {
    param(
        [int]$StartPort = 17345,
        [int[]]$ReservedPorts = @()
    )

    $reserved = @{}
    foreach ($port in $ReservedPorts) {
        if ($port) {
            $reserved[$port] = $true
        }
    }

    for ($port = $StartPort; $port -lt 65535; $port++) {
        if ($reserved.ContainsKey($port)) {
            continue
        }
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $port)
            $listener.Start()
            return "ws://127.0.0.1:$port"
        } catch {
        } finally {
            if ($listener) {
                $listener.Stop()
            }
        }
    }
    throw "No available local port found from $StartPort"
}

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
    param(
        [string]$TargetStatePath = $StatePath,
        [switch]$AllowProcessScan
    )

    $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $TargetStatePath)
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
        Clear-CodexLarkPidState -StatePath $TargetStatePath
        return
    }

    if (-not $AllowProcessScan) {
        Write-Host "codex-lark is stopped"
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

function Start-CodexLarkProject {
    param(
        [Parameter(Mandatory = $true)]$Project,
        [string]$AssignedCodexWsUrl
    )

    $name = [string]$Project.name
    if (-not $name) {
        throw "project entry is missing name"
    }
    if (-not $Project.workspace_root) {
        throw "project '$name' is missing workspace_root"
    }
    $workspace = (Resolve-Path -LiteralPath ([string]$Project.workspace_root)).Path
    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "start",
        "-ProjectName", "`"$name`"",
        "-WorkspaceRoot", "`"$workspace`""
    )
    if ($AssignedCodexWsUrl) {
        $argList += @("-CodexWsUrl", "`"$AssignedCodexWsUrl`"")
    }
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden
    $urlText = if ($AssignedCodexWsUrl) { " codex_ws_url=$AssignedCodexWsUrl" } else { "" }
    Write-Host "started project '$name' workspace=$workspace$urlText"
}

function Show-CodexLarkStatus {
    param(
        [switch]$All,
        [string]$TargetProjectName
    )

    $rows = @()
    $projects = @(Get-CodexLarkProjects)
    if ($All -and $projects) {
        foreach ($project in $projects) {
            $name = [string]$project.name
            $targetState = Get-CodexLarkProjectStatePath -Name $name
            $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $targetState)
            $alive = $pidProcs.Count -gt 0
            $rows += [pscustomobject]@{
                Project = $name
                Running = $alive
                Workspace = [string]$project.workspace_root
                ChatIds = (@($project.chat_ids) + @($project.chat_id) | Where-Object { $_ }) -join ","
                CodexWsUrl = if ($alive) { [string]$state.codex_ws_url } else { [string]$project.codex_ws_url }
                Pids = ($pidProcs | ForEach-Object { $_.Id }) -join ","
            }
        }
        $rows | Format-Table -AutoSize
        return
    }

    $targetStatePath = if ($TargetProjectName) { Get-CodexLarkProjectStatePath -Name $TargetProjectName } else { $StatePath }
    $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $targetStatePath)
    if ($pidProcs) {
        Write-Host "codex-lark is running"
        $pidProcs | Select-Object Id, ProcessName, Path | Format-Table -AutoSize
        return
    }
    if ($TargetProjectName) {
        Write-Host "codex-lark project '$TargetProjectName' is stopped"
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
}

if ($Command -eq "stop") {
    $targetStatePath = if ($ProjectName) { Get-CodexLarkProjectStatePath -Name $ProjectName } else { $StatePath }
    Stop-CodexLark -TargetStatePath $targetStatePath -AllowProcessScan:(!$ProjectName)
    return
}

if ($Command -eq "stop-all") {
    $projects = @(Get-CodexLarkProjects)
    if (-not $projects) {
        Stop-CodexLark -AllowProcessScan
        return
    }
    foreach ($project in $projects) {
        $name = [string]$project.name
        Write-Host "stopping project '$name'"
        Stop-CodexLark -TargetStatePath (Get-CodexLarkProjectStatePath -Name $name)
    }
    return
}

if ($Command -eq "restart") {
    $targetStatePath = if ($ProjectName) { Get-CodexLarkProjectStatePath -Name $ProjectName } else { $StatePath }
    Stop-CodexLark -TargetStatePath $targetStatePath -AllowProcessScan:(!$ProjectName)
    Start-Sleep -Seconds 1
    $Command = "start"
}

if ($Command -eq "status") {
    Show-CodexLarkStatus -TargetProjectName $ProjectName
    return
}

if ($Command -eq "status-all") {
    Show-CodexLarkStatus -All
    return
}

if ($Command -eq "start-all") {
    $projects = @(Get-CodexLarkProjects)
    if (-not $projects) {
        throw "Missing projects config: $ProjectsConfigPath. Run '.\codex-lark.ps1 init' first."
    }
    $reservedPorts = @()
    foreach ($project in $projects) {
        $configuredUrl = [string]$project.codex_ws_url
        $assignedUrl = ""
        if (Test-CodexLarkAutoWsUrl -Value $configuredUrl) {
            $assignedUrl = Get-CodexLarkFreeWsUrl -ReservedPorts $reservedPorts
        } else {
            $assignedUrl = $configuredUrl
        }
        $port = Get-CodexLarkWsPort -WsUrl $assignedUrl
        if ($port) {
            $reservedPorts += $port
        }
        Start-CodexLarkProject -Project $project -AssignedCodexWsUrl $assignedUrl
    }
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

    $EffectiveCodexWsUrl = $CodexWsUrl
    if (-not $EffectiveCodexWsUrl) {
        $projects = @(Get-CodexLarkProjects)
        $selectedProject = $null
        if ($ProjectName) {
            $selectedProject = $projects | Where-Object { [string]$_.name -eq $ProjectName } | Select-Object -First 1
        }
        if ($selectedProject -and (Test-CodexLarkAutoWsUrl -Value ([string]$selectedProject.codex_ws_url))) {
            $EffectiveCodexWsUrl = Get-CodexLarkFreeWsUrl
        }
    }

    if ($ProjectName) {
        if ($EffectiveCodexWsUrl) {
            python $Script run --workspace-root $CurrentWorkspaceRoot --project-name $ProjectName --codex-ws-url $EffectiveCodexWsUrl
        } else {
            python $Script run --workspace-root $CurrentWorkspaceRoot --project-name $ProjectName
        }
    } else {
        if ($EffectiveCodexWsUrl) {
            python $Script run --workspace-root $CurrentWorkspaceRoot --codex-ws-url $EffectiveCodexWsUrl
        } else {
            python $Script run --workspace-root $CurrentWorkspaceRoot
        }
    }
}
finally {
    Pop-Location
}

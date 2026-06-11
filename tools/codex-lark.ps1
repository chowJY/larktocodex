param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "start-all", "stop", "stop-all", "restart", "status", "status-all")]
    [string]$Command = "start",

    [string]$ChatId,
    [Parameter(Position = 1)]
    [string]$ProjectName,
    [string]$WorkspaceRoot,
    [string]$CodexWsUrl
)

$ErrorActionPreference = "Stop"
$BridgeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $PSScriptRoot "codex_lark_bridge.py"
$CurrentWorkspaceRoot = if ($WorkspaceRoot) { (Resolve-Path -LiteralPath $WorkspaceRoot).Path } else { (Get-Location).Path }
$ProjectsConfigPath = Join-Path $BridgeRoot ".lark-events\projects-config.json"

function ConvertTo-CodexLarkSafeName {
    param([string]$Name)
    return (($Name.Trim()) -replace '[^A-Za-z0-9_.-]', '_')
}

function ConvertTo-CodexLarkWorkspaceKey {
    param([string]$WorkspaceRoot)

    if (-not $WorkspaceRoot) {
        return ""
    }
    try {
        return (Resolve-Path -LiteralPath $WorkspaceRoot).Path
    } catch {
        return [System.IO.Path]::GetFullPath($WorkspaceRoot)
    }
}

function Get-CodexLarkProjects {
    if (-not (Test-Path -LiteralPath $ProjectsConfigPath)) {
        return @()
    }
    $config = Get-Content -LiteralPath $ProjectsConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $config.projects) {
        return @()
    }
    if ($config.projects -is [array]) {
        return @($config.projects)
    }
    $projects = @()
    foreach ($property in $config.projects.PSObject.Properties) {
        if ($property.Value -isnot [psobject]) {
            continue
        }
        $project = $property.Value
        if (-not $project.PSObject.Properties["workspace_root"]) {
            $project | Add-Member -NotePropertyName "workspace_root" -NotePropertyValue $property.Name
        } elseif (-not [string]$project.workspace_root) {
            $project.workspace_root = $property.Name
        }
        $projects += $project
    }
    return @($projects)
}

function Find-CodexLarkProjectByName {
    param([string]$Name)

    if (-not $Name) {
        return $null
    }
    return @(Get-CodexLarkProjects) | Where-Object { [string]$_.name -eq $Name } | Select-Object -First 1
}

function Find-CodexLarkProjectByWorkspace {
    param([string]$WorkspaceRoot)

    $target = ConvertTo-CodexLarkWorkspaceKey -WorkspaceRoot $WorkspaceRoot
    if (-not $target) {
        return $null
    }
    return @(Get-CodexLarkProjects) | Where-Object {
        (ConvertTo-CodexLarkWorkspaceKey -WorkspaceRoot ([string]$_.workspace_root)) -eq $target
    } | Select-Object -First 1
}

function Invoke-CodexLarkMissingProjectHook {
    param([string]$WorkspaceRoot)

    # Reserved for future project auto-registration/initialization.
    return $null
}

function Resolve-CodexLarkStartProject {
    param(
        [string]$Name,
        [string]$WorkspaceRoot
    )

    $projects = @(Get-CodexLarkProjects)
    if (-not $projects) {
        return $null
    }
    if ($Name) {
        $project = Find-CodexLarkProjectByName -Name $Name
        if (-not $project) {
            throw ("Project not found in {0}: {1}" -f $ProjectsConfigPath, $Name)
        }
        return $project
    }
    $matched = Find-CodexLarkProjectByWorkspace -WorkspaceRoot $WorkspaceRoot
    if ($matched) {
        return $matched
    }
    $created = Invoke-CodexLarkMissingProjectHook -WorkspaceRoot $WorkspaceRoot
    if ($created) {
        return $created
    }
    throw "No project config found for workspace '$WorkspaceRoot' in $ProjectsConfigPath"
}

function Get-CodexLarkProjectStatePath {
    param([string]$Name)
    if (-not $Name) {
        throw "Project name is required for project state path"
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
    param(
        [string]$BridgeRoot,
        [string]$ProjectName,
        [string]$WorkspaceRoot
    )

    $escapedBridge = [regex]::Escape($BridgeRoot)
    $escapedProject = if ($ProjectName) { [regex]::Escape($ProjectName) } else { "" }
    $escapedWorkspace = if ($WorkspaceRoot) { [regex]::Escape($WorkspaceRoot) } else { "" }
    try {
        $matches = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.CommandLine -match "$escapedBridge.*codex_lark_bridge\.py" -or
            $_.CommandLine -match 'im\.message\.receive_v1' -or
            ($_.CommandLine -match 'app-server' -and $_.CommandLine -match 'ws://127\.0\.0\.1:17345')
        }
        if ($ProjectName -or $WorkspaceRoot) {
            $matches = $matches | Where-Object {
                ($escapedProject -and $_.CommandLine -match "--project-name\s+`"?$escapedProject`"?") -or
                ($escapedWorkspace -and $_.CommandLine -match $escapedWorkspace)
            }
        }
        return $matches
    } catch {
        $script:CodexLarkProcessQueryFailed = $_
        return @()
    }
}

function Stop-CodexLark {
    param(
        [string]$TargetStatePath,
        [switch]$AllowProcessScan,
        [string]$TargetProjectName,
        [string]$TargetWorkspaceRoot
    )

    $pidProcs = if ($TargetStatePath) { @(Get-CodexLarkPidProcesses -StatePath $TargetStatePath) } else { @() }
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

    $procs = Get-CodexLarkProcesses -BridgeRoot $BridgeRoot -ProjectName $TargetProjectName -WorkspaceRoot $TargetWorkspaceRoot
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

function Invoke-CodexLarkStopMaintenance {
    param([string]$TargetProjectName)

    $args = @($Script, "notify-stop")
    if ($TargetProjectName) {
        $args += @("--project-name", $TargetProjectName)
    }
    try {
        & python @args
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "stop maintenance exited with code $LASTEXITCODE"
        }
    } catch {
        Write-Warning "failed to send disconnect notice or clear processed messages: $_"
    }
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
        [string]$TargetProjectName,
        [string]$TargetWorkspaceRoot
    )

    $rows = @()
    $projects = @(Get-CodexLarkProjects)
    if ($All -and $projects) {
        foreach ($project in $projects) {
            $name = [string]$project.name
            $targetState = Get-CodexLarkProjectStatePath -Name $name
            $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $targetState)
            $alive = $pidProcs.Count -gt 0
            $state = $null
            if ($alive -and (Test-Path -LiteralPath $targetState)) {
                try {
                    $state = Get-Content -LiteralPath $targetState -Raw -Encoding UTF8 | ConvertFrom-Json
                } catch {
                    $state = $null
                }
            }
            $rows += [pscustomobject]@{
                Project = $name
                Running = $alive
                Workspace = [string]$project.workspace_root
                ChatIds = (@($project.chat_ids) + @($project.chat_id) | Where-Object { $_ }) -join ","
                CodexWsUrl = if ($alive -and $state) { [string]$state.codex_ws_url } else { [string]$project.codex_ws_url }
                Pids = ($pidProcs | ForEach-Object { $_.Id }) -join ","
            }
        }
        $rows | Format-Table -AutoSize
        return
    }

    $targetProject = if ($TargetProjectName) {
        Find-CodexLarkProjectByName -Name $TargetProjectName
    } else {
        Find-CodexLarkProjectByWorkspace -WorkspaceRoot $TargetWorkspaceRoot
    }
    if (-not $targetProject) {
        if ($projects) {
            $rows = foreach ($project in $projects) {
                $name = [string]$project.name
                $targetState = Get-CodexLarkProjectStatePath -Name $name
                $pidProcs = @(Get-CodexLarkPidProcesses -StatePath $targetState)
                [pscustomobject]@{
                    Project = $name
                    Running = $pidProcs.Count -gt 0
                    Workspace = [string]$project.workspace_root
                    ChatIds = (@($project.chat_ids) + @($project.chat_id) | Where-Object { $_ }) -join ","
                    CodexWsUrl = [string]$project.codex_ws_url
                    Pids = ($pidProcs | ForEach-Object { $_.Id }) -join ","
                }
            }
            $rows | Format-Table -AutoSize
            return
        }
        Write-Host "No project config found in $ProjectsConfigPath"
        return
    }

    $TargetProjectName = [string]$targetProject.name
    $targetStatePath = Get-CodexLarkProjectStatePath -Name $TargetProjectName
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
}

if ($Command -eq "stop") {
    $targetProject = if ($ProjectName) { Find-CodexLarkProjectByName -Name $ProjectName } else { Find-CodexLarkProjectByWorkspace -WorkspaceRoot $CurrentWorkspaceRoot }
    if (-not $targetProject) {
        throw "No project config found for stop target. Use -ProjectName or stop-all."
    }
    $ProjectName = [string]$targetProject.name
    $targetStatePath = Get-CodexLarkProjectStatePath -Name $ProjectName
    $targetWorkspace = [string]$targetProject.workspace_root
    Invoke-CodexLarkStopMaintenance -TargetProjectName $ProjectName
    Stop-CodexLark -TargetStatePath $targetStatePath -AllowProcessScan -TargetProjectName $ProjectName -TargetWorkspaceRoot $targetWorkspace
    return
}

if ($Command -eq "stop-all") {
    $projects = @(Get-CodexLarkProjects)
    if (-not $projects) {
        throw "Missing projects config: $ProjectsConfigPath. Run '.\codex-lark.ps1 init' first."
    }
    foreach ($project in $projects) {
        $name = [string]$project.name
        Write-Host "stopping project '$name'"
        Invoke-CodexLarkStopMaintenance -TargetProjectName $name
        Stop-CodexLark -TargetStatePath (Get-CodexLarkProjectStatePath -Name $name) -AllowProcessScan -TargetProjectName $name -TargetWorkspaceRoot ([string]$project.workspace_root)
    }
    return
}

if ($Command -eq "restart") {
    $targetProject = if ($ProjectName) { Find-CodexLarkProjectByName -Name $ProjectName } else { Find-CodexLarkProjectByWorkspace -WorkspaceRoot $CurrentWorkspaceRoot }
    if (-not $targetProject) {
        throw "No project config found for restart target. Use -ProjectName."
    }
    $ProjectName = [string]$targetProject.name
    $targetStatePath = Get-CodexLarkProjectStatePath -Name $ProjectName
    $targetWorkspace = [string]$targetProject.workspace_root
    Invoke-CodexLarkStopMaintenance -TargetProjectName $ProjectName
    Stop-CodexLark -TargetStatePath $targetStatePath -AllowProcessScan -TargetProjectName $ProjectName -TargetWorkspaceRoot $targetWorkspace
    Start-Sleep -Seconds 1
    $Command = "start"
}

if ($Command -eq "status") {
    Show-CodexLarkStatus -TargetProjectName $ProjectName -TargetWorkspaceRoot $CurrentWorkspaceRoot
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
    $SelectedProject = $null
    if ($Command -eq "start") {
        $SelectedProject = Resolve-CodexLarkStartProject -Name $ProjectName -WorkspaceRoot $CurrentWorkspaceRoot
        if ($SelectedProject) {
            $ProjectName = [string]$SelectedProject.name
            $CurrentWorkspaceRoot = ConvertTo-CodexLarkWorkspaceKey -WorkspaceRoot ([string]$SelectedProject.workspace_root)
        }
    }
    if (-not $EffectiveCodexWsUrl) {
        if ($SelectedProject -and (Test-CodexLarkAutoWsUrl -Value ([string]$SelectedProject.codex_ws_url))) {
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

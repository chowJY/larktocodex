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
$Launcher = Join-Path $PSScriptRoot "codex-lark.ps1"

$ForwardArgs = @{ Command = $Command }
if ($ChatId) { $ForwardArgs.ChatId = $ChatId }
if ($ProjectName) { $ForwardArgs.ProjectName = $ProjectName }
if ($WorkspaceRoot) { $ForwardArgs.WorkspaceRoot = $WorkspaceRoot }
if ($CodexWsUrl) { $ForwardArgs.CodexWsUrl = $CodexWsUrl }

& $Launcher @ForwardArgs

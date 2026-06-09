param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "stop", "restart", "status")]
    [string]$Command = "start",

    [string]$ChatId
)

$ErrorActionPreference = "Stop"
$Launcher = Join-Path $PSScriptRoot "codex-lark.ps1"

if ($ChatId) {
    & $Launcher $Command -ChatId $ChatId
} else {
    & $Launcher $Command
}

@echo off
setlocal
chcp 65001 >nul

set "BRIDGE_ROOT=%~dp0"
if "%BRIDGE_ROOT:~-1%"=="\" set "BRIDGE_ROOT=%BRIDGE_ROOT:~0,-1%"
set "CODEX_LARK=%BRIDGE_ROOT%\codex-lark.cmd"
set "CODEX_LARK_ROOT=%BRIDGE_ROOT%"

if not exist "%CODEX_LARK%" (
  echo codex-lark.cmd not found: "%CODEX_LARK%"
  exit /b 1
)

call "%CODEX_LARK%" init %*
if errorlevel 1 exit /b %errorlevel%

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [Environment]::GetEnvironmentVariable('CODEX_LARK_ROOT', 'Process').TrimEnd('\');" ^
  "$userPath = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
  "$parts = @($userPath -split ';' | Where-Object { $_ });" ^
  "$exists = $parts | Where-Object { [string]::Equals(([Environment]::ExpandEnvironmentVariables($_).TrimEnd('\')), $root, [StringComparison]::OrdinalIgnoreCase) };" ^
  "if (-not $exists) { [Environment]::SetEnvironmentVariable('Path', (($parts + $root) -join ';'), 'User') };" ^
  "if (($env:Path -split ';') -notcontains $root) { $env:Path = $env:Path + ';' + $root };" ^
  "Write-Host 'codex-lark command registered. Open a new terminal, then run: codex-lark status'"

exit /b %errorlevel%

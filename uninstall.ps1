# Limewheel one-line uninstaller:
#   irm https://raw.githubusercontent.com/1949-2005/limewheel/main/uninstall.ps1 | iex
# Removes the app, its shortcuts and its Apps entry. Settings in %APPDATA%\LimewheelWin are kept.

& {
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Limewheel'
    $entry = Get-ItemProperty $key -ErrorAction SilentlyContinue
    if (-not $entry) { Write-Host 'Limewheel is not installed.' -ForegroundColor Yellow; return }
    $script = Join-Path $entry.InstallLocation 'uninstall.ps1'
    if (-not (Test-Path $script)) { Write-Host "Can't find $script" -ForegroundColor Red; return }
    Start-Process powershell.exe -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$script`"" -Wait
    Write-Host 'Limewheel was removed.' -ForegroundColor Green
}

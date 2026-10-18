# Limewheel one-line installer (Windows 10/11, no admin rights needed):
#   irm https://raw.githubusercontent.com/1949-2005/limewheel/main/install.ps1 | iex
# Downloads the latest LimewheelSetup.exe from GitHub Releases, checks its SHA-256, installs and opens it.
# Running it again updates Limewheel in place.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'      # much faster downloads in Windows PowerShell 5.1
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $repo = '1949-2005/limewheel'
    $ua = @{ 'User-Agent' = 'limewheel-installer' }

    Write-Host ''
    Write-Host '  Limewheel installer' -ForegroundColor Green
    Write-Host '  Finding the latest release...'
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers $ua
    $exe = $rel.assets | Where-Object { $_.name -eq 'LimewheelSetup.exe' }
    $sum = $rel.assets | Where-Object { $_.name -eq 'LimewheelSetup.exe.sha256' }
    if (-not $exe -or -not $sum) { throw "Release $($rel.tag_name) is missing LimewheelSetup.exe or its checksum." }

    $dir = Join-Path $env:TEMP 'limewheel-setup'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $setup = Join-Path $dir 'LimewheelSetup.exe'
    $hashFile = Join-Path $dir 'LimewheelSetup.exe.sha256'
    Write-Host ("  Downloading {0} ({1:N0} MB)..." -f $rel.tag_name, ($exe.size / 1MB))
    Invoke-WebRequest $exe.browser_download_url -OutFile $setup -Headers $ua -UseBasicParsing
    Invoke-WebRequest $sum.browser_download_url -OutFile $hashFile -Headers $ua -UseBasicParsing

    $expected = ((Get-Content $hashFile -Raw).Trim() -split '\s+')[0].ToLower()
    $actual = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Remove-Item $setup -Force
        throw 'Checksum mismatch: the download is damaged or was changed. Nothing was installed.'
    }
    Write-Host '  Checksum OK. Installing...'

    $p = Start-Process $setup -ArgumentList '--silent' -PassThru
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) { throw "Setup failed (exit code $($p.ExitCode))." }
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host '  Limewheel is installed and running (look for the lime in the system tray).' -ForegroundColor Green
    Write-Host '  Drag a file, tap Shift / Ctrl / Alt, and drop it on an option.'
    Write-Host '  Uninstall: Settings > Apps > Installed apps > Limewheel'
    Write-Host ''
}

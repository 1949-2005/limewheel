<#
.SYNOPSIS
    Build an unsigned Limewheel MSIX package from a PyInstaller onedir layout.

.DESCRIPTION
    Stages the app (Limewheel.exe + _internal), the Explorer shell handler
    (LimewheelShell.dll, built if missing), generated tile/logo assets, and a
    filled AppxManifest.xml, then packs a .msix. The package is left unsigned:
    the Microsoft Store signs it on ingestion. For local testing see
    packaging\README.md.

    Packing engine: the script first tries makeappx.exe from the NuGet package
    Microsoft.Windows.SDK.BuildTools (downloaded once, cached under
    %LOCALAPPDATA%\limewheel-build). makeappx depends on a side-by-side private
    assembly that cannot be activated on machines without the Windows SDK
    installed; when makeappx cannot run, the script falls back to pack_msix.py,
    which drives the SAME packaging engine through Windows' own system COM class
    (AppxPackaging.dll IAppxFactory/IAppxPackageWriter). Either way the manifest
    is validated by the appx engine. No Visual Studio / Windows SDK / admin is
    required. signtool.exe is also cached for the optional local-signing path
    documented in the README.

.PARAMETER AppDir
    The onedir build folder containing Limewheel.exe and _internal\.

.PARAMETER Icon
    A square PNG (>= 512 px) used to generate all tile/logo assets.

.PARAMETER Version
    4-part package version (default 1.3.0.0).

.PARAMETER IdentityName
    Package Identity/Name (default "Limewheel.Dev"; use the Partner Center value
    for a Store build).

.PARAMETER Publisher
    Package Identity/Publisher (default "CN=LimewheelDev"; use the Partner Center
    value for a Store build).

.PARAMETER PublisherDisplayName
    Publisher display name (default "Limewheel").

.PARAMETER Out
    Output folder for the .msix (default packaging\out).

.PARAMETER KeepStaging
    Keep the staged layout (and print its path) instead of deleting it, e.g. to
    test with `Add-AppxPackage -Register <staging>\AppxManifest.xml`.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$Icon,
    [string]$Version = '1.3.0.0',
    [string]$IdentityName = 'Limewheel.Dev',
    [string]$Publisher = 'CN=LimewheelDev',
    [string]$PublisherDisplayName = 'Limewheel',
    [string]$Out,
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here
if (-not $Out) { $Out = Join-Path $here 'out' }

$cacheRoot = Join-Path $env:LOCALAPPDATA 'limewheel-build'
$sdkDir    = Join-Path $cacheRoot 'sdk'
$engineDir = Join-Path $cacheRoot 'makeappx-engine'

# --- resolve / validate inputs --------------------------------------------
$AppDir = (Resolve-Path $AppDir).Path
$Icon   = (Resolve-Path $Icon).Path
if (-not (Test-Path (Join-Path $AppDir 'Limewheel.exe'))) {
    throw "AppDir does not contain Limewheel.exe: $AppDir"
}
if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw "Version must be 4-part (e.g. 1.3.0.0): $Version"
}

# --- 1. Acquire SDK BuildTools (makeappx.exe + signtool.exe) --------------
# Best-effort: packing does not require these (see the COM fallback), but we
# cache them to honour the documented toolchain and the local-signing path.
function Get-SdkTools {
    $existing = Get-ChildItem -Path $sdkDir -Filter makeappx.exe -Recurse `
        -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match '\\x64\\' } |
        Select-Object -First 1
    if (-not $existing) {
        Write-Host 'Downloading Microsoft.Windows.SDK.BuildTools from nuget.org...'
        New-Item -ItemType Directory -Force -Path $sdkDir | Out-Null
        $pkg = 'microsoft.windows.sdk.buildtools'
        $index = Invoke-RestMethod "https://api.nuget.org/v3-flatcontainer/$pkg/index.json"
        $ver = $index.versions | Where-Object { $_ -notmatch 'preview|rtm' } |
            Select-Object -Last 1
        if (-not $ver) { $ver = $index.versions | Select-Object -Last 1 }
        Write-Host "  version $ver"
        $nupkg = Join-Path $sdkDir "$pkg.$ver.zip"
        Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/$pkg/$ver/$pkg.$ver.nupkg" `
            -OutFile $nupkg
        $extract = Join-Path $sdkDir $ver
        if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
        Expand-Archive -Path $nupkg -DestinationPath $extract -Force
        Remove-Item $nupkg -Force
        $existing = Get-ChildItem -Path $extract -Filter makeappx.exe -Recurse |
            Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
    }
    return $existing.FullName
}

$makeappx = $null
try {
    $makeappx = Get-SdkTools
    $toolsDir = Split-Path -Parent $makeappx
    $signtool = Join-Path $toolsDir 'signtool.exe'
    Write-Host "makeappx (cached): $makeappx"
    if (Test-Path $signtool) {
        Write-Host "signtool (cached, for optional local signing): $signtool"
    }
}
catch {
    Write-Warning "Could not fetch SDK BuildTools ($($_.Exception.Message)). Continuing with the system packaging engine."
}

# --- 2. Ensure the shell DLL exists ---------------------------------------
$dll = Join-Path $repoRoot 'shell\out\LimewheelShell.dll'
if (-not (Test-Path $dll)) {
    Write-Host 'LimewheelShell.dll missing; building it...'
    & (Join-Path $repoRoot 'shell\build_shell.ps1')
    if (-not (Test-Path $dll)) { throw 'Failed to build LimewheelShell.dll' }
}

# --- 3. Stage the package layout ------------------------------------------
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "limewheel-msix-staging-$PID"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

Write-Host "Staging into $staging"
Copy-Item -Path (Join-Path $AppDir '*') -Destination $staging -Recurse -Force
Copy-Item -Path $dll -Destination (Join-Path $staging 'LimewheelShell.dll') -Force

$assets = Join-Path $staging 'Assets'
& python -I (Join-Path $here 'make_assets.py') $Icon $assets
if ($LASTEXITCODE -ne 0) { throw 'Asset generation failed' }

# Fill the manifest (written without a BOM; makeappx/appx dislike a UTF-8 BOM).
$template = Get-Content (Join-Path $here 'AppxManifest.template.xml') -Raw
$manifestText = $template `
    -replace '\{IDENTITY_NAME\}', $IdentityName `
    -replace '\{PUBLISHER\}', $Publisher `
    -replace '\{PUBLISHER_DISPLAY_NAME\}', $PublisherDisplayName `
    -replace '\{VERSION\}', $Version
$manifestPath = Join-Path $staging 'AppxManifest.xml'
[System.IO.File]::WriteAllText($manifestPath, $manifestText,
    (New-Object System.Text.UTF8Encoding($false)))

# --- 4. Pack --------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$msix = Join-Path $Out "Limewheel_${Version}_x64.msix"
if (Test-Path $msix) { Remove-Item $msix -Force }

function Try-Makeappx {
    param([string]$MakeappxPath)
    if (-not $MakeappxPath) { return $false }
    # Give makeappx its best chance: run it from a clean folder where the
    # engine DLLs carry the exact-case names its SxS manifests reference.
    try {
        $src = Split-Path -Parent $MakeappxPath
        if (Test-Path $engineDir) { Remove-Item $engineDir -Recurse -Force }
        New-Item -ItemType Directory -Force $engineDir | Out-Null
        Copy-Item (Join-Path $src 'makeappx.exe') (Join-Path $engineDir 'makeappx.exe')
        Copy-Item (Join-Path $src 'appxpackaging.dll') (Join-Path $engineDir 'AppxPackaging.dll')
        Copy-Item (Join-Path $src 'opcservices.dll')   (Join-Path $engineDir 'OpcServices.dll')
        Copy-Item (Join-Path $src 'Microsoft.Windows.Build.Appx.AppxPackaging.dll.manifest') $engineDir
        Copy-Item (Join-Path $src 'Microsoft.Windows.Build.Appx.OpcServices.dll.manifest') $engineDir
        $log = Join-Path ([System.IO.Path]::GetTempPath()) "makeappx-$PID.log"
        & cmd /c "`"$(Join-Path $engineDir 'makeappx.exe')`" pack /d `"$staging`" /p `"$msix`" /o > `"$log`" 2>&1"
        $code = $LASTEXITCODE
        if ($code -eq 0 -and (Test-Path $msix)) {
            Write-Host 'Packed with makeappx.exe.'
            return $true
        }
        $detail = (Get-Content $log -Raw -ErrorAction SilentlyContinue)
        Write-Host "makeappx.exe could not pack (exit $code): $($detail.Trim())"
        return $false
    }
    catch {
        Write-Host "makeappx.exe could not run: $($_.Exception.Message)"
        return $false
    }
}

$packed = Try-Makeappx -MakeappxPath $makeappx
if (-not $packed) {
    Write-Host 'Falling back to the system packaging engine (pack_msix.py / AppxPackaging COM)...'
    & python -I (Join-Path $here 'pack_msix.py') $staging $manifestPath $msix
    if ($LASTEXITCODE -ne 0) { throw 'pack_msix.py failed' }
}

# --- 5. Verify (packer-independent: the .msix is an OPC zip) ---------------
& python -I -c "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); n=[x.replace('\\','/') for x in z.namelist()]; req=['Limewheel.exe','LimewheelShell.dll','AppxManifest.xml','AppxBlockMap.xml','[Content_Types].xml']; miss=[r for r in req if r not in n]; print('entries:',len(n)); print('contains required:', not miss); sys.exit(1 if miss else 0)" $msix
if ($LASTEXITCODE -ne 0) { throw 'Package verification failed (missing expected parts)' }
Write-Host 'Verified: exe, shell DLL, manifest, block map and content-types are inside.'

# --- cleanup staging (keep the .msix) -------------------------------------
if ($KeepStaging) {
    Write-Host "Staging kept: $staging"
}
else {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}

$bytes = (Get-Item $msix).Length
$sizeMB = [math]::Round($bytes / 1MB, 2)
Write-Host ''
Write-Host "MSIX: $msix"
Write-Host "Size: $bytes bytes ($sizeMB MB)"

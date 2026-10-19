<#
.SYNOPSIS
    Build LimewheelShell.dll (x64) using Zig as the C++ compiler.

.DESCRIPTION
    Produces shell\out\LimewheelShell.dll, a self-contained in-proc COM server
    (Explorer IExplorerCommand handler). The DLL statically links libc++ and
    libunwind so it depends only on system DLLs.

    No Visual Studio / Windows SDK required: Zig ships its own clang toolchain
    and MinGW-w64 headers/import libs. The only dependency is the PyPI package
    `ziglang` (installed automatically if missing).

.PARAMETER Verify
    After building, run a PE import-table check (requires `pefile`, installed
    automatically) to confirm no non-system DLL dependencies.
#>
[CmdletBinding()]
param(
    [switch]$Verify = $true
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir = Join-Path $here 'out'
$src    = Join-Path $here 'limewheel_shell.cpp'
$def    = Join-Path $here 'limewheel_shell.def'
$dll    = Join-Path $outDir 'LimewheelShell.dll'

# 1. Ensure ziglang is available.
& python -m ziglang version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing ziglang from PyPI...'
    & python -m pip install ziglang
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install ziglang' }
}
$zigVer = (& python -m ziglang version).Trim()
Write-Host "Using zig $zigVer"

# 2. Build.
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $dll) { Remove-Item $dll -Force }

Write-Host "Compiling $src -> $dll"
# Notes:
#  * Zig's x86_64-windows-gnu target statically links its bundled libc++ and
#    libunwind into the DLL by default (there is no libc++ runtime DLL to
#    ship), so the result depends only on OS DLLs. Passing a bare `-static`
#    flag here is counter-productive: it makes LLD emit a static archive
#    (!<arch>) at the -o path instead of a PE DLL, so we do NOT use it.
#  * The .def file (EXPORTS ... PRIVATE) is the authoritative export list; it
#    disables MinGW "export all symbols" so only the two undecorated COM
#    entry points are exported.
$implib = Join-Path $outDir 'LimewheelShell.lib'
$zigArgs = @(
    '-m', 'ziglang', 'c++',
    '-target', 'x86_64-windows-gnu',
    '-shared',
    '-O2',
    '-o', $dll,
    "-Wl,--out-implib=$implib",
    $src,
    $def,
    '-lole32', '-lshell32', '-lshlwapi', '-luuid', '-luser32', '-loleaut32'
)
& python @zigArgs
if ($LASTEXITCODE -ne 0) { throw "zig exited with code $LASTEXITCODE" }

# The import library is a build side-artifact, not something we ship.
if (Test-Path $implib) { Remove-Item $implib -Force -ErrorAction SilentlyContinue }

if (-not (Test-Path $dll)) { throw 'Build produced no DLL' }
$size = (Get-Item $dll).Length
Write-Host "Built: $dll ($size bytes)"

# 3. Verify imports (no non-system DLL dependencies).
if ($Verify) {
    & python -c "import pefile" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing pefile from PyPI (dev-only)...'
        & python -m pip install pefile | Out-Null
    }
    $checker = Join-Path $here 'check_imports.py'
    & python -I $checker $dll
    if ($LASTEXITCODE -ne 0) { throw 'Import verification failed' }
}

Write-Host 'build_shell.ps1: done.'

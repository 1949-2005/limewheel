# Builds dist\LimewheelSetup.exe (one file that installs Limewheel) and its .sha256 checksum.
param([switch]$SkipAppBuild)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipAppBuild) { & .\build.ps1 }

# Payload: the whole app folder, zipped
$stage = Join-Path $env:TEMP "limewheel-installer"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
$payload = Join-Path $stage "payload.zip"
python -I -c "
import sys, zipfile, pathlib
src, out = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in src.rglob('*'):
        if f.is_file(): z.write(f, f.relative_to(src).as_posix())
" (Resolve-Path "dist\Limewheel") $payload

$extra = @("--add-data", "$payload;.")
foreach ($f in "ui.py", "icons.py", "brand.py", "brand.txt", "__init__.py") { $extra += @("--add-data", "limewheel\$f;limewheel") }
$exclude = "PySide6.QtWebEngineCore","PySide6.QtWebEngineWidgets","PySide6.QtWebEngineQuick","PySide6.Qt3DCore",
           "PySide6.QtQuick","PySide6.QtQml","PySide6.QtMultimedia","PySide6.QtCharts","PySide6.QtPdf",
           "tkinter","matplotlib","numpy","cv2","fitz","pymupdf","PIL","pdf2docx","openpyxl","docx","pptx"
$exArgs = $exclude | ForEach-Object { "--exclude-module", $_ }
pyinstaller --noconfirm --clean --windowed --onefile --name LimewheelSetup --icon app.ico `
  --hidden-import PySide6.QtSvg --hidden-import win32com.client --hidden-import pythoncom `
  @extra @exArgs installer\setup_app.py
Remove-Item build, LimewheelSetup.spec -Recurse -Force -ErrorAction SilentlyContinue

$hash = (Get-FileHash "dist\LimewheelSetup.exe" -Algorithm SHA256).Hash.ToLower()
Set-Content "dist\LimewheelSetup.exe.sha256" "$hash  LimewheelSetup.exe" -NoNewline
Write-Host "`nBuilt: $PSScriptRoot\dist\LimewheelSetup.exe  (sha256 $hash)"

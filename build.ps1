# Builds dist\Limewheel\Limewheel.exe (no console window).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -r requirements.txt pyinstaller

# Exe icon from the built-in lime mark
python -c "import sys; from PySide6.QtWidgets import QApplication; a=QApplication(sys.argv); from limewheel.app import lime_pixmap; lime_pixmap(256).save('app_icon.png')"
python -c "from pathlib import Path; from PIL import Image; from limewheel.ops.images import save_ico; save_ico(Image.open('app_icon.png'), Path('app.ico'), bmp_below=64)"
Remove-Item app_icon.png

$exclude = "PySide6.QtWebEngineCore","PySide6.QtWebEngineWidgets","PySide6.QtWebEngineQuick","PySide6.Qt3DCore",
           "PySide6.QtQuick","PySide6.QtQml","PySide6.QtMultimedia","PySide6.QtCharts","PySide6.QtDataVisualization",
           "PySide6.QtPdf","PySide6.QtBluetooth","PySide6.QtNetwork","PySide6.QtOpenGL","tkinter","matplotlib","cv2","pdf2docx"
$exArgs = $exclude | ForEach-Object { "--exclude-module", $_ }

pyinstaller --noconfirm --clean --windowed --onedir --name Limewheel --icon app.ico `
  --add-data "limewheel\brand.txt;limewheel" `
  --collect-binaries imageio_ffmpeg --collect-data imageio_ffmpeg `
  --collect-submodules pillow_heif --hidden-import win32timezone `
  @exArgs main.py
Remove-Item build, Limewheel.spec -Recurse -Force -ErrorAction SilentlyContinue

# Drop Qt pieces the app never loads (software OpenGL, QML/Quick, Qt PDF, translations).
$qt = "dist\Limewheel\_internal\PySide6"
$drop = "opengl32sw.dll","Qt6Quick*.dll","Qt6Qml*.dll","Qt6Pdf*.dll","Qt6VirtualKeyboard*.dll","Qt6OpenGL.dll","QtQuick*.pyd","QtQml.pyd","QtPdf*.pyd","QtOpenGL*.pyd","QtNetwork.pyd","Qt6Network.dll"
$drop | ForEach-Object { Get-ChildItem $qt -Filter $_ -File -ErrorAction SilentlyContinue | Remove-Item -Force }
Remove-Item "$qt\translations", "$qt\qml" -Recurse -Force -ErrorAction SilentlyContinue
# python-docx only needs templates\default.docx; its unzipped copy uses names MSIX reserves ([Content_Types].xml, _rels)
Remove-Item "dist\Limewheel\_internal\docx\templates\default-docx-template" -Recurse -Force -ErrorAction SilentlyContinue

# Explorer right-click menu add-on (the DLL launches Limewheel.exe next to it)
if (-not (Test-Path shell\out\LimewheelShell.dll)) { & .\shell\build_shell.ps1 }
Copy-Item shell\out\LimewheelShell.dll dist\Limewheel\ -Force

Write-Host "`nBuilt: $PSScriptRoot\dist\Limewheel\Limewheel.exe"

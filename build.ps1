# Builds dist\Limewheel\Limewheel.exe (no console window).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -r requirements.txt pyinstaller

# Exe icon from the built-in lime mark
python -c "import sys; from PySide6.QtWidgets import QApplication; a=QApplication(sys.argv); from limewheel.app import lime_pixmap; lime_pixmap(256).save('app_icon.png')"
python -c "from PIL import Image; Image.open('app_icon.png').save('app.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
Remove-Item app_icon.png

$exclude = "PySide6.QtWebEngineCore","PySide6.QtWebEngineWidgets","PySide6.QtWebEngineQuick","PySide6.Qt3DCore",
           "PySide6.QtQuick","PySide6.QtQml","PySide6.QtMultimedia","PySide6.QtCharts","PySide6.QtDataVisualization",
           "PySide6.QtPdf","PySide6.QtBluetooth","tkinter","matplotlib"
$exArgs = $exclude | ForEach-Object { "--exclude-module", $_ }

pyinstaller --noconfirm --clean --windowed --onedir --name Limewheel --icon app.ico `
  --add-data "limewheel\brand.txt;limewheel" `
  --collect-binaries imageio_ffmpeg --collect-data imageio_ffmpeg `
  --collect-submodules pillow_heif --hidden-import win32timezone `
  @exArgs main.py
Remove-Item build, Limewheel.spec -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`nBuilt: $PSScriptRoot\dist\Limewheel\Limewheel.exe"

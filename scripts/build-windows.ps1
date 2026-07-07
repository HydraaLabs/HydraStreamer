$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

$Version = (Select-String -Path "hydra_streamer.py" -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

.\scripts\bundle-deps-windows.ps1

if (!(Test-Path "bin\windows\x64\ffmpeg.exe") -or !(Test-Path "bin\windows\x64\ffprobe.exe")) {
  throw "Windows FFmpeg bundle is missing. Expected bin\windows\x64\ffmpeg.exe and bin\windows\x64\ffprobe.exe."
}

.\.venv\Scripts\pyinstaller.exe `
  --clean `
  --onefile `
  --name HydraStreamer `
  hydra_streamer.py

$Versioned = "dist\HydraStreamer-$Version-windows-x64.exe"
Copy-Item "dist\HydraStreamer.exe" $Versioned -Force

Write-Host "Built: dist\HydraStreamer.exe"
Write-Host "Asset: $Versioned"

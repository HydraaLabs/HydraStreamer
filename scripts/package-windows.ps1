$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

$Version = (Select-String -Path "hydra_streamer.py" -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$PackageRoot = "dist\HydraStreamer-$Version-windows-x64"
$ZipPath = "dist\HydraStreamer-$Version-windows-x64.zip"

if (!(Test-Path "dist\HydraStreamer.exe")) {
  .\scripts\build-windows.ps1
}

if (Test-Path $PackageRoot) {
  Remove-Item $PackageRoot -Recurse -Force
}
if (Test-Path $ZipPath) {
  Remove-Item $ZipPath -Force
}

New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "dist") | Out-Null

Copy-Item "dist\HydraStreamer.exe" (Join-Path $PackageRoot "dist\HydraStreamer.exe") -Force
Copy-Item "scripts\install-windows.ps1" (Join-Path $PackageRoot "install-windows.ps1") -Force

$BinSource = "bin\windows\x64"
if ((Test-Path (Join-Path $BinSource "ffmpeg.exe")) -and (Test-Path (Join-Path $BinSource "ffprobe.exe"))) {
  $BinTarget = Join-Path $PackageRoot "bin\windows\x64"
  New-Item -ItemType Directory -Force -Path $BinTarget | Out-Null
  Copy-Item (Join-Path $BinSource "ffmpeg.exe") $BinTarget -Force
  Copy-Item (Join-Path $BinSource "ffprobe.exe") $BinTarget -Force
}

Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force
Write-Host "Built: $ZipPath"

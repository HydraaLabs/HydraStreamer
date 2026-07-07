$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $env:LOCALAPPDATA "HydraStreamer"
$LogDir = Join-Path $AppDir "logs"
$ExeSource = Join-Path $Root "dist\HydraStreamer.exe"
$ExeTarget = Join-Path $AppDir "HydraStreamer.exe"

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (!(Test-Path $ExeSource)) {
  throw "dist\HydraStreamer.exe not found. Build it first on Windows."
}

Copy-Item $ExeSource $ExeTarget -Force

$BinSource = Join-Path (Join-Path (Join-Path $Root "bin") "windows") "x64"
if ((Test-Path (Join-Path $BinSource "ffmpeg.exe")) -and (Test-Path (Join-Path $BinSource "ffprobe.exe"))) {
  $BinTarget = Join-Path $AppDir "bin"
  New-Item -ItemType Directory -Force -Path $BinTarget | Out-Null
  Copy-Item (Join-Path $BinSource "ffmpeg.exe") $BinTarget -Force
  Copy-Item (Join-Path $BinSource "ffprobe.exe") $BinTarget -Force
}

$AppBin = Join-Path $AppDir "bin"
$BundledFfmpeg = Join-Path $AppBin "ffmpeg.exe"
$BundledFfprobe = Join-Path $AppBin "ffprobe.exe"
if (!(Test-Path $BundledFfmpeg) -or !(Test-Path $BundledFfprobe)) {
  $ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
  $ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
  if (!$ffmpeg -or !$ffprobe) {
    Write-Host "HydraStreamer: bundled FFmpeg is missing, installing system ffmpeg fallback..."
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
      winget install --id Gyan.FFmpeg --exact --accept-source-agreements --accept-package-agreements
    } else {
      $choco = Get-Command choco.exe -ErrorAction SilentlyContinue
      if ($choco) {
        choco install ffmpeg -y
      } else {
        throw "HydraStreamer: install FFmpeg manually or provide bin\ffmpeg.exe and bin\ffprobe.exe."
      }
    }
  }
}

$LogFile = Join-Path $LogDir "hydrastreamer.log"
$Action = New-ScheduledTaskAction -Execute $ExeTarget -Argument "--log-file `"$LogFile`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
  -TaskName "HydraStreamer" `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Description "Hydracker local HLS transcoder" `
  -Force | Out-Null

Start-ScheduledTask -TaskName "HydraStreamer"

Write-Host "HydraStreamer installed and started."
Write-Host "Test: Invoke-RestMethod http://127.0.0.1:17654/health"

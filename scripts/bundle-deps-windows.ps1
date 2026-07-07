$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

function Ensure-Ffmpeg {
  $ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
  $ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
  if ($ffmpeg -and $ffprobe) {
    return
  }

  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
  if ($winget) {
    winget install --id Gyan.FFmpeg --exact --accept-source-agreements --accept-package-agreements
  } else {
    $choco = Get-Command choco.exe -ErrorAction SilentlyContinue
    if ($choco) {
      choco install ffmpeg -y
    } else {
      throw "ffmpeg/ffprobe are missing. Install FFmpeg or provide bin\ffmpeg.exe and bin\ffprobe.exe."
    }
  }
}

Ensure-Ffmpeg

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction Stop
$ffprobe = Get-Command ffprobe.exe -ErrorAction Stop
$BinDir = Join-Path (Join-Path (Join-Path (Get-Location) "bin") "windows") "x64"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item $ffmpeg.Source (Join-Path $BinDir "ffmpeg.exe") -Force
Copy-Item $ffprobe.Source (Join-Path $BinDir "ffprobe.exe") -Force

Write-Host "Bundled:"
& (Join-Path $BinDir "ffmpeg.exe") -version | Select-Object -First 1
& (Join-Path $BinDir "ffprobe.exe") -version | Select-Object -First 1

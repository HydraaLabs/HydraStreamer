# HydraStreamer

HydraStreamer runs on the user's computer at `http://127.0.0.1:17654`.
Hydracker sends it a signed 1Fichier direct URL, and HydraStreamer converts it
to local HLS with `ffmpeg`.

Endpoints:

- `GET /health`
- `GET /version`
- `GET /probe?url=...`
- `GET /stream.m3u8?url=...&audio=1`
- `POST /forward` — exécute un petit appel HTTPS API depuis l'IP du client
  (débridage 1Fichier sans proxy : l'API et le téléchargement partagent la
  même IP, donc pas de verrouillage IP). Allowlist d'hôtes
  (`HYDRASTREAMER_FORWARD_HOSTS`, défaut `api.1fichier.com`), allowlist
  d'origines navigateur (`HYDRASTREAMER_FORWARD_ORIGINS`, défaut
  `https://hydracker.com,https://www.hydracker.com`), 1 req/s/hôte. `/health`
  et `/version` exposent `capabilities.forward: true` quand l'endpoint est
  disponible.

`/version` returns the local app version, platform, architecture and the last
auto-update state. The updater checks:

```text
https://hydracker.com/hydrastreamer/releases/latest.json
```

Override it for staging:

```bash
HYDRASTREAMER_UPDATE_URL=https://example.test/latest.json HydraStreamer
```

Disable automatic update checks:

```bash
HYDRASTREAMER_AUTO_UPDATE=0 HydraStreamer
```

## Runtime requirements

For end users, no Python install is required when using the packaged app.
PyInstaller embeds the Python runtime into the executable.

HydraStreamer needs `ffmpeg` and `ffprobe`. Release builds must bundle them
inside the app folder:

- Windows: `bin/windows/x64/ffmpeg.exe` and `bin/windows/x64/ffprobe.exe`
- Linux: `bin/linux/x64/ffmpeg` or `bin/linux/arm64/ffmpeg`
- macOS: `bin/macos/x64/ffmpeg` or `bin/macos/arm64/ffmpeg`

If the bundled binaries are missing, HydraStreamer falls back to
`ffmpeg`/`ffprobe` from `PATH`. The installer scripts then try to install the
system package as a last resort:

- Linux: `apt`, `dnf`, `yum`, `pacman` or `zypper`
- macOS: Homebrew `ffmpeg`
- Windows: `winget` package `Gyan.FFmpeg`, then Chocolatey `ffmpeg`

Source/development mode requires Python 3.10+.

Packaged releases do not require Python on the user's machine. PyInstaller
embeds the Python runtime inside `HydraStreamer.exe` / `HydraStreamer`.

Download all runtime FFmpeg/FFprobe binaries for supported OS/arch targets:

```bash
./scripts/download-runtime-bins.py
```

This downloads Linux x64/arm64, macOS x64/arm64 and Windows x64 binaries into
`bin/<platform>/<arch>/`.

Bundle local binaries before packaging:

```bash
./scripts/bundle-deps.sh --install-fallback
```

Windows:

```powershell
.\scripts\bundle-deps-windows.ps1
```

## Build

Build on each target OS. PyInstaller is not a cross-compiler, so the Windows
`.exe` must be built on Windows, the macOS binary on macOS, and the Linux binary
on Linux.

```bash
./scripts/build-linux-macos.sh
```

Windows:

```powershell
.\scripts\build-windows.ps1
```

The binary is created in `dist/`. The build scripts call the dependency bundler
so releases include `ffmpeg` and `ffprobe` automatically.

## Release packages

Linux `.deb` and `.rpm`:

```bash
./scripts/package-linux.sh
```

macOS `.pkg` when `pkgbuild` is available, otherwise `.zip`:

```bash
./scripts/package-macos.sh
```

Windows ZIP installer:

```powershell
.\scripts\package-windows.ps1
```

This creates `dist\HydraStreamer-<version>-windows-x64.zip`. The user extracts
it and runs `install-windows.ps1`; the installer copies the app to
`%LOCALAPPDATA%\HydraStreamer`, registers the Scheduled Task, and starts it
immediately.

Windows Inno Setup installer:

```powershell
iscc /DAppVersion=<version> scripts\HydraStreamer.iss
```

This creates `dist\HydraStreamer-<version>-windows-x64-setup.exe`, which the
GitHub release workflow builds and verifies automatically. It installs to
`%LOCALAPPDATA%\HydraStreamer` and registers a startup shortcut instead of the
Scheduled Task. On Windows the auto-updater prefers this `-setup.exe` asset,
runs it silently (`/VERYSILENT`), and restarts the app afterwards through the
Scheduled Task or the startup shortcut, whichever the install used.

Generate the update manifest after uploading all assets to the GitHub release:

```bash
./scripts/make-manifest.py \
  --base-url https://github.com/HydraaLabs/HydraStreamer/releases/download/v0.2.0 \
  dist/HydraStreamer-0.2.0-windows-x64.exe \
  dist/HydraStreamer-0.2.0-windows-x64-setup.exe \
  dist/HydraStreamer-0.2.0-windows-x64.zip \
  dist/hydrastreamer_0.2.0_amd64.deb \
  dist/hydrastreamer-0.2.0-1.x86_64.rpm \
  dist/HydraStreamer-0.2.0-x64.pkg
```

Publish the generated `dist/latest.json` as:

```text
https://hydracker.com/hydrastreamer/releases/latest.json
```

Hydracker serves that manifest, but the asset URLs inside it point to:

```text
https://github.com/HydraaLabs/HydraStreamer/releases/download/v<VERSION>/
```

Manifest schema:

```json
{
  "app": "HydraStreamer",
  "version": "0.2.0",
  "assets": [
    {
      "platform": "linux",
      "arch": "x64",
      "format": "deb",
      "url": "https://github.com/HydraaLabs/HydraStreamer/releases/download/v0.2.0/hydrastreamer_0.2.0_amd64.deb",
      "sha256": "...",
      "size": 12345678
    }
  ]
}
```

## Install

Linux:

```bash
./scripts/install-linux.sh
```

macOS:

```bash
./scripts/install-macos.sh
```

Windows PowerShell:

```powershell
.\scripts\install-windows.ps1
```

The installers use the current user only and enable autostart at login:

- Linux: `systemd --user`
- macOS: `~/Library/LaunchAgents/com.hydracker.hydrastreamer.plist`
- Windows: Scheduled Task `HydraStreamer`

## Test

```bash
curl http://127.0.0.1:17654/health
```

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_DIR="${HOME}/Library/Application Support/HydraStreamer"
BIN_DIR="${HOME}/bin"
LOG_DIR="${HOME}/Library/Logs/HydraStreamer"
LAUNCH_AGENT_DIR="${HOME}/Library/LaunchAgents"
PLIST="${LAUNCH_AGENT_DIR}/com.hydracker.hydrastreamer.plist"

mkdir -p "$APP_DIR" "$BIN_DIR" "$LOG_DIR" "$LAUNCH_AGENT_DIR"

if [[ -x "dist/HydraStreamer" ]]; then
  install -m 0755 "dist/HydraStreamer" "$APP_DIR/HydraStreamer"
else
  install -m 0755 "hydra_streamer.py" "$APP_DIR/HydraStreamer"
fi

BUNDLE_ARCH="${HYDRASTREAMER_BUNDLE_ARCH:-$(uname -m)}"
case "$BUNDLE_ARCH" in
  x86_64|amd64) BUNDLE_ARCH="x64" ;;
  aarch64|arm64) BUNDLE_ARCH="arm64" ;;
esac
if [[ -f "bin/macos/$BUNDLE_ARCH/ffmpeg" && -f "bin/macos/$BUNDLE_ARCH/ffprobe" ]]; then
  mkdir -p "$APP_DIR/bin"
  cp -f "bin/macos/$BUNDLE_ARCH/ffmpeg" "bin/macos/$BUNDLE_ARCH/ffprobe" "$APP_DIR/bin/"
  chmod +x "$APP_DIR/bin/"* 2>/dev/null || true
fi

ensure_runtime_bins() {
  if [[ -x "$APP_DIR/bin/ffmpeg" && -x "$APP_DIR/bin/ffprobe" ]]; then
    return
  fi
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    return
  fi
  echo "HydraStreamer: bundled FFmpeg is missing, installing system ffmpeg fallback..."
  if command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "HydraStreamer: Homebrew is required to install ffmpeg fallback. Install ffmpeg manually." >&2
    exit 1
  fi
}

ensure_runtime_bins

ln -sf "$APP_DIR/HydraStreamer" "$BIN_DIR/HydraStreamer"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hydracker.hydrastreamer</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/HydraStreamer</string>
    <string>--log-file</string>
    <string>$LOG_DIR/hydrastreamer.log</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "HydraStreamer installed and started."
echo "Test: curl http://127.0.0.1:17654/health"

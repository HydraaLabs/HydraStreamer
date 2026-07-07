#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_DIR="${HOME}/.local/share/HydraStreamer"
BIN_DIR="${HOME}/.local/bin"
SERVICE_DIR="${HOME}/.config/systemd/user"
LOG_DIR="${HOME}/.local/state/HydraStreamer"

mkdir -p "$APP_DIR" "$BIN_DIR" "$SERVICE_DIR" "$LOG_DIR"

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
if [[ -f "bin/linux/$BUNDLE_ARCH/ffmpeg" && -f "bin/linux/$BUNDLE_ARCH/ffprobe" ]]; then
  mkdir -p "$APP_DIR/bin"
  cp -f "bin/linux/$BUNDLE_ARCH/ffmpeg" "bin/linux/$BUNDLE_ARCH/ffprobe" "$APP_DIR/bin/"
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
  if command -v apt-get >/dev/null 2>&1; then
    run_privileged apt-get update
    run_privileged apt-get install -y ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    run_privileged dnf install -y ffmpeg
  elif command -v yum >/dev/null 2>&1; then
    run_privileged yum install -y ffmpeg
  elif command -v pacman >/dev/null 2>&1; then
    run_privileged pacman -Sy --noconfirm ffmpeg
  elif command -v zypper >/dev/null 2>&1; then
    run_privileged zypper --non-interactive install ffmpeg
  else
    echo "HydraStreamer: could not install ffmpeg automatically. Install ffmpeg manually." >&2
    exit 1
  fi
}

run_privileged() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  elif command -v pkexec >/dev/null 2>&1; then
    pkexec "$@"
  else
    echo "HydraStreamer: sudo/pkexec is required to install ffmpeg fallback." >&2
    return 1
  fi
}

ensure_runtime_bins

ln -sf "$APP_DIR/HydraStreamer" "$BIN_DIR/HydraStreamer"

cat > "$SERVICE_DIR/hydrastreamer.service" <<EOF
[Unit]
Description=HydraStreamer local HLS transcoder
After=network-online.target

[Service]
Type=simple
ExecStart=$APP_DIR/HydraStreamer --log-file $LOG_DIR/hydrastreamer.log
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hydrastreamer.service

echo "HydraStreamer installed and started."
echo "Test: curl http://127.0.0.1:17654/health"

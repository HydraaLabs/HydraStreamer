#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="$(python3 - <<'PY'
import re
from pathlib import Path
m = re.search(r'^VERSION\s*=\s*"([^"]+)"', Path('hydra_streamer.py').read_text(), re.M)
print(m.group(1))
PY
)"
ARCH="$(uname -m)"
if [[ "$ARCH" == "x86_64" ]]; then
  ASSET_ARCH="x64"
else
  ASSET_ARCH="arm64"
fi
BUNDLE_ARCH="${HYDRASTREAMER_BUNDLE_ARCH:-$ASSET_ARCH}"

if [[ ! -x dist/HydraStreamer ]]; then
  ./scripts/build-linux-macos.sh
fi

./scripts/download-runtime-bins.py "macos-$BUNDLE_ARCH" --skip-existing

PAYLOAD="dist/pkg/macos-payload"
SCRIPTS="dist/pkg/macos-scripts"
python3 - <<'PY'
import shutil
from pathlib import Path
for path in ("dist/pkg/macos-payload", "dist/pkg/macos-scripts"):
    shutil.rmtree(Path(path), ignore_errors=True)
PY
mkdir -p "$PAYLOAD/Applications/HydraStreamer" "$SCRIPTS"
install -m 0755 dist/HydraStreamer "$PAYLOAD/Applications/HydraStreamer/HydraStreamer"
if [[ -f "bin/macos/$BUNDLE_ARCH/ffmpeg" && -f "bin/macos/$BUNDLE_ARCH/ffprobe" ]]; then
  mkdir -p "$PAYLOAD/Applications/HydraStreamer/bin"
  install -m 0755 "bin/macos/$BUNDLE_ARCH/ffmpeg" "bin/macos/$BUNDLE_ARCH/ffprobe" "$PAYLOAD/Applications/HydraStreamer/bin/"
fi

cat > "$SCRIPTS/postinstall" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

USER_NAME="${USER:-$(stat -f '%Su' /dev/console)}"
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [[ -z "$USER_HOME" || ! -d "$USER_HOME" ]]; then
  exit 0
fi
LAUNCH_AGENT_DIR="$USER_HOME/Library/LaunchAgents"
LOG_DIR="$USER_HOME/Library/Logs/HydraStreamer"
PLIST="$LAUNCH_AGENT_DIR/com.hydracker.hydrastreamer.plist"
mkdir -p "$LAUNCH_AGENT_DIR" "$LOG_DIR"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hydracker.hydrastreamer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/HydraStreamer/HydraStreamer</string>
    <string>--log-file</string>
    <string>$LOG_DIR/hydrastreamer.log</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
PLIST
chown -R "$USER_NAME" "$LAUNCH_AGENT_DIR" "$LOG_DIR"
launchctl bootout "gui/$(id -u "$USER_NAME")" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u "$USER_NAME")" "$PLIST" 2>/dev/null || true
EOF
chmod +x "$SCRIPTS/postinstall"

if command -v pkgbuild >/dev/null 2>&1; then
  pkgbuild \
    --root "$PAYLOAD" \
    --scripts "$SCRIPTS" \
    --identifier com.hydracker.hydrastreamer \
    --version "$VERSION" \
    "dist/HydraStreamer-${VERSION}-${ASSET_ARCH}.pkg"
else
  (cd "$PAYLOAD/Applications" && zip -qry "../../../HydraStreamer-${VERSION}-${ASSET_ARCH}.zip" HydraStreamer)
fi

ls -lh dist/HydraStreamer-"$VERSION"-"$ASSET_ARCH".pkg dist/HydraStreamer-"$VERSION"-"$ASSET_ARCH".zip 2>/dev/null || true

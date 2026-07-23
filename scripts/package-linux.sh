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
ARCH_DEB="${ARCH_DEB:-amd64}"
ARCH_RPM="${ARCH_RPM:-x86_64}"
BUNDLE_ARCH="${HYDRASTREAMER_BUNDLE_ARCH:-x64}"
APP="HydraStreamer"
DIST="dist"
BIN="$DIST/$APP"

if [[ ! -x "$BIN" ]]; then
  ./scripts/build-linux-macos.sh
fi

./scripts/download-runtime-bins.py "linux-$BUNDLE_ARCH" --skip-existing

python3 - <<'PY'
import shutil
from pathlib import Path
shutil.rmtree(Path("dist/pkg"), ignore_errors=True)
PY
mkdir -p "$DIST/pkg"

install_tree() {
  local root="$1"
  install -d "$root/opt/HydraStreamer" "$root/usr/bin" "$root/etc/xdg/autostart" "$root/lib/systemd/system"
  install -m 0755 "$BIN" "$root/opt/HydraStreamer/HydraStreamer"
  if [[ -f "bin/linux/$BUNDLE_ARCH/ffmpeg" && -f "bin/linux/$BUNDLE_ARCH/ffprobe" ]]; then
    install -d "$root/opt/HydraStreamer/bin"
    install -m 0755 "bin/linux/$BUNDLE_ARCH/ffmpeg" "bin/linux/$BUNDLE_ARCH/ffprobe" "$root/opt/HydraStreamer/bin/"
  fi
  ln -s /opt/HydraStreamer/HydraStreamer "$root/usr/bin/HydraStreamer"
  ln -s /opt/HydraStreamer/HydraStreamer "$root/usr/bin/hydrastreamer"
  cat > "$root/etc/xdg/autostart/hydrastreamer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=HydraStreamer
Exec=/opt/HydraStreamer/HydraStreamer --log-file %h/.local/state/HydraStreamer/hydrastreamer.log
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
  cat > "$root/lib/systemd/system/hydrastreamer.service" <<EOF
[Unit]
Description=HydraStreamer local HLS transcoder
After=network-online.target

[Service]
Type=simple
ExecStart=/opt/HydraStreamer/HydraStreamer --log-file /var/log/hydrastreamer/hydrastreamer.log
Restart=always
RestartSec=3
DynamicUser=yes
LogsDirectory=hydrastreamer
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF
}

DEB_ROOT="$DIST/pkg/deb-root"
install_tree "$DEB_ROOT"
install -d "$DEB_ROOT/DEBIAN"
cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: hydrastreamer
Version: $VERSION
Section: video
Priority: optional
Architecture: $ARCH_DEB
Maintainer: Hydracker <contact@hydracker.com>
Description: Hydracker local HLS transcoder
 HydraStreamer exposes a local HTTP server used by Hydracker to convert
 signed direct video links to browser-compatible HLS.
EOF
cat > "$DEB_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  if systemctl is-active --quiet hydrastreamer.service; then
    # Upgrade : le binaire a changé, relancer le daemon (enable --now seul
    # ne redémarre pas un service déjà actif).
    systemctl restart hydrastreamer.service || true
  else
    systemctl enable --now hydrastreamer.service || true
  fi
fi

exit 0
EOF
cat > "$DEB_ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop hydrastreamer.service || true
    systemctl disable hydrastreamer.service || true
  fi
fi

exit 0
EOF
cat > "$DEB_ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
fi

exit 0
EOF
chmod 0755 "$DEB_ROOT/DEBIAN/postinst" "$DEB_ROOT/DEBIAN/prerm" "$DEB_ROOT/DEBIAN/postrm"
dpkg-deb --build "$DEB_ROOT" "$DIST/hydrastreamer_${VERSION}_${ARCH_DEB}.deb"

RPM_TOP="$DIST/rpmbuild"
RPM_ROOT="$RPM_TOP/BUILDROOT/hydrastreamer-${VERSION}-1.${ARCH_RPM}"
install_tree "$RPM_ROOT"
mkdir -p "$RPM_TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cat > "$RPM_TOP/SPECS/hydrastreamer.spec" <<EOF
Name: hydrastreamer
Version: $VERSION
Release: 1
Summary: Hydracker local HLS transcoder
License: Proprietary
BuildArch: $ARCH_RPM

%description
HydraStreamer exposes a local HTTP server used by Hydracker to convert signed
direct video links to browser-compatible HLS.

%files
/opt/HydraStreamer
/usr/bin/HydraStreamer
/usr/bin/hydrastreamer
/etc/xdg/autostart/hydrastreamer.desktop
/lib/systemd/system/hydrastreamer.service

%post
systemctl daemon-reload >/dev/null 2>&1 || true
if systemctl is-active --quiet hydrastreamer.service; then
  systemctl restart hydrastreamer.service >/dev/null 2>&1 || true
else
  systemctl enable --now hydrastreamer.service >/dev/null 2>&1 || true
fi

%preun
if [ "\$1" = "0" ]; then
  systemctl stop hydrastreamer.service >/dev/null 2>&1 || true
  systemctl disable hydrastreamer.service >/dev/null 2>&1 || true
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || true
EOF
rpmbuild --define "_topdir $(pwd)/$RPM_TOP" --define "buildroot $(pwd)/$RPM_ROOT" -bb "$RPM_TOP/SPECS/hydrastreamer.spec"
cp "$RPM_TOP/RPMS/$ARCH_RPM"/hydrastreamer-"$VERSION"-1.*.rpm "$DIST/"

echo "Built:"
ls -lh "$DIST"/*.deb "$DIST"/*.rpm

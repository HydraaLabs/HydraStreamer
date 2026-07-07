#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

install_fallback=false
force=false
if [[ "${1:-}" == "--install-fallback" ]]; then
  install_fallback=true
fi
if [[ "${1:-}" == "--force" || "${2:-}" == "--force" ]]; then
  force=true
fi

install_system_ffmpeg() {
  if ! $install_fallback; then
    return 1
  fi
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
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    return 1
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
    return 1
  fi
}

require_binary() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    install_system_ffmpeg || {
      echo "$name is missing and could not install ffmpeg automatically" >&2
      exit 1
    }
  fi
  command -v "$name" >/dev/null 2>&1 || {
    echo "$name is still missing after dependency install" >&2
    exit 1
  }
}

require_binary ffmpeg
require_binary ffprobe

case "$(uname -s)" in
  Darwin) platform_dir="macos" ;;
  Linux) platform_dir="linux" ;;
  *) platform_dir="unix" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch_dir="x64" ;;
  arm64|aarch64) arch_dir="arm64" ;;
  *) arch_dir="$(uname -m)" ;;
esac

mkdir -p "bin/$platform_dir/$arch_dir"
if ! $force && [[ -x "bin/$platform_dir/$arch_dir/ffmpeg" && -x "bin/$platform_dir/$arch_dir/ffprobe" ]]; then
  echo "Bundled binaries already exist in bin/$platform_dir/$arch_dir. Use --force to replace them."
  exit 0
fi
cp -f "$(command -v ffmpeg)" "bin/$platform_dir/$arch_dir/ffmpeg"
cp -f "$(command -v ffprobe)" "bin/$platform_dir/$arch_dir/ffprobe"
chmod +x "bin/$platform_dir/$arch_dir/ffmpeg" "bin/$platform_dir/$arch_dir/ffprobe"

echo "Bundled:"
"bin/$platform_dir/$arch_dir/ffmpeg" -version | sed -n '1p'
"bin/$platform_dir/$arch_dir/ffprobe" -version | sed -n '1p'

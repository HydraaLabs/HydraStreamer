#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt

./scripts/download-runtime-bins.py "$(uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/macos/;s/linux/linux/')-$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/')" --skip-existing

pyinstaller \
  --clean \
  --onefile \
  --name HydraStreamer \
  hydra_streamer.py

echo "Built: dist/HydraStreamer"

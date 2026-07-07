#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
REPO_API = "https://api.github.com/repos/descriptinc/ffmpeg-ffprobe-static/releases/latest"
USER_AGENT = "HydraStreamer-build"

TARGETS = {
    "linux-x64": {
        "dir": ROOT / "bin" / "linux" / "x64",
        "assets": {"ffmpeg": "ffmpeg-linux-x64", "ffprobe": "ffprobe-linux-x64"},
    },
    "linux-arm64": {
        "dir": ROOT / "bin" / "linux" / "arm64",
        "assets": {"ffmpeg": "ffmpeg-linux-arm64", "ffprobe": "ffprobe-linux-arm64"},
    },
    "macos-x64": {
        "dir": ROOT / "bin" / "macos" / "x64",
        "assets": {"ffmpeg": "ffmpeg-darwin-x64", "ffprobe": "ffprobe-darwin-x64"},
    },
    "macos-arm64": {
        "dir": ROOT / "bin" / "macos" / "arm64",
        "assets": {"ffmpeg": "ffmpeg-darwin-arm64", "ffprobe": "ffprobe-darwin-arm64"},
    },
    "windows-x64": {
        "dir": ROOT / "bin" / "windows" / "x64",
        "assets": {"ffmpeg.exe": "ffmpeg-win32-x64", "ffprobe.exe": "ffprobe-win32-x64"},
    },
}


def fetch_json(url):
    req = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url, destination):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".download")
    with urlopen(req, timeout=180) as response, open(tmp, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    tmp.replace(destination)
    if os.name != "nt":
        destination.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description="Download FFmpeg/FFprobe runtime binaries for HydraStreamer")
    parser.add_argument(
        "targets",
        nargs="*",
        choices=sorted(TARGETS.keys()),
        help="Targets to download. Defaults to all supported targets.",
    )
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    selected = args.targets or sorted(TARGETS.keys())
    release = fetch_json(REPO_API)
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    print(f"Source: descriptinc/ffmpeg-ffprobe-static {release.get('tag_name')}")

    for target_name in selected:
        target = TARGETS[target_name]
        print(f"\n==> {target_name}")
        for binary_name, asset_name in target["assets"].items():
            asset = assets.get(asset_name)
            if not asset:
                raise SystemExit(f"Missing asset {asset_name} in latest release")
            destination = target["dir"] / binary_name
            if args.skip_existing and destination.exists():
                print(f"skip {destination}")
                continue
            print(f"download {asset_name} -> {destination}")
            download(asset["browser_download_url"], destination)


if __name__ == "__main__":
    main()

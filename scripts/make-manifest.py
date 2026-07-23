#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def version():
    source = (ROOT / "hydra_streamer.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.M)
    if not match:
        raise SystemExit("VERSION not found in hydra_streamer.py")
    return match.group(1)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def detect(path):
    name = path.name.lower()
    if name.endswith(".exe"):
        return "windows", "setup" if "setup" in name else "exe"
    if name.endswith(".deb"):
        return "linux", "deb"
    if name.endswith(".rpm"):
        return "linux", "rpm"
    if name.endswith(".pkg"):
        return "macos", "pkg"
    if name.endswith(".dmg"):
        return "macos", "dmg"
    if name.endswith(".zip") and "windows" in name:
        return "windows", "zip"
    if name.endswith(".zip"):
        return "macos", "zip"
    raise SystemExit(f"Unsupported asset type: {path}")


def arch_from_name(path):
    name = path.name.lower()
    if any(token in name for token in ("arm64", "aarch64")):
        return "arm64"
    if any(token in name for token in ("x64", "x86_64", "amd64")):
        return "x64"
    return "universal"


def main():
    parser = argparse.ArgumentParser(description="Create HydraStreamer update manifest")
    parser.add_argument("--base-url", required=True, help="Public URL prefix that hosts the assets")
    parser.add_argument("--out", default="dist/latest.json")
    parser.add_argument("assets", nargs="+")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    manifest = {
        "app": "HydraStreamer",
        "version": version(),
        "assets": [],
    }
    for raw in args.assets:
        path = Path(raw)
        platform, fmt = detect(path)
        manifest["assets"].append({
            "platform": platform,
            "arch": arch_from_name(path),
            "format": fmt,
            "url": f"{base_url}/{path.name}",
            "sha256": sha256(path),
            "size": path.stat().st_size,
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()

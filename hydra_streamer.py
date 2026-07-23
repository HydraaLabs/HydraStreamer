#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import platform
import sys
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


APP_NAME = "HydraStreamer"
HOST = "127.0.0.1"
PORT = 17654
VERSION = "0.3.1"
DEFAULT_UPDATE_MANIFEST_URL = "https://hydracker.com/hydrastreamer/releases/latest.json"
UPDATE_MANIFEST_URL = os.environ.get("HYDRASTREAMER_UPDATE_URL", DEFAULT_UPDATE_MANIFEST_URL)
AUTO_UPDATE_ENABLED = os.environ.get("HYDRASTREAMER_AUTO_UPDATE", "1").lower() not in {"0", "false", "no"}
AUTO_UPDATE_INTERVAL_SECONDS = int(os.environ.get("HYDRASTREAMER_UPDATE_INTERVAL", str(6 * 60 * 60)))
ROOT = Path(tempfile.gettempdir()) / "hydra-streamer"
JOBS = {}
LOCK = threading.Lock()
LOG_HANDLE = None
IDLE_JOB_TTL_SECONDS = 180
# Hide console windows of child processes (ffmpeg, ffprobe, ...) on Windows,
# required once the app itself is built without a console (--noconsole).
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
UPDATE_STATE = {
    "checked_at": None,
    "status": "idle",
    "current_version": VERSION,
    "latest_version": None,
    "asset": None,
    "error": None,
}

# `/forward` executes small HTTPS API calls (e.g. 1Fichier get_token) from the
# client's own IP, so the debrid API and the file download share one IP and no
# proxy lock applies. Guard rails: target host allowlist, browser Origin
# allowlist, bounded payloads, 1 req/s/host (native 1Fichier API limit).
FORWARD_ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "HYDRASTREAMER_FORWARD_HOSTS",
        "api.1fichier.com,"
        "api.alldebrid.com,"
        "api.real-debrid.com,"
        "debrid-link.com,"
        "www.premiumize.me,"
        "api.torbox.app",
    ).split(",")
    if h.strip()
}
FORWARD_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get(
        "HYDRASTREAMER_FORWARD_ORIGINS",
        "https://hydracker.com,https://www.hydracker.com,"
        "https://hydracker.local,https://app.hydracker.local",
    ).split(",")
    if o.strip()
}
FORWARD_MAX_BODY = 16 * 1024
FORWARD_MAX_RESPONSE = 2 * 1024 * 1024
FORWARD_THROTTLE = {}
# Only these request headers are forwarded to the target host.
FORWARD_HEADER_ALLOWLIST = {"authorization", "content-type", "accept", "user-agent"}


def cors(handler):
    # `/forward` carries API tokens: never answer with a wildcard origin.
    # Browsers enforce Origin on cross-origin POSTs, so echoing only the
    # allowlisted Hydracker origins blocks other sites from driving the daemon.
    if urlparse(handler.path).path == "/forward":
        origin = handler.headers.get("Origin")
        if origin and origin in FORWARD_ALLOWED_ORIGINS:
            handler.send_header("Access-Control-Allow-Origin", origin)
            handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            handler.send_header("Vary", "Origin")
        return
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Private-Network", "true")


def json_response(handler, code, payload):
    data = json.dumps(payload).encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except BrokenPipeError:
        return


def text_response(handler, code, text, content_type="text/plain; charset=utf-8"):
    data = text.encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except BrokenPipeError:
        return


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def binary_path(name):
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [
        app_dir() / "bin" / f"{name}{suffix}",
        app_dir() / f"{name}{suffix}",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"{name} is required")


def probe(url, cookies=None):
    ffprobe = binary_path("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
    ]
    if cookies:
        cmd.extend(["-cookies", cookies])
    cmd.append(url)
    proc = subprocess.run(cmd, text=True, timeout=30, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(classify_media_error(detail))
    out = proc.stdout
    data = json.loads(out)
    audio = []
    video = []
    subtitles = []
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        item = {
            "index": stream.get("index"),
            "codec": stream.get("codec_name"),
            "language": (stream.get("tags") or {}).get("language"),
            "title": (stream.get("tags") or {}).get("title"),
            "channels": stream.get("channels"),
        }
        if codec_type == "audio":
            audio.append(item)
        elif codec_type == "video":
            item["width"] = stream.get("width")
            item["height"] = stream.get("height")
            item["pix_fmt"] = stream.get("pix_fmt")
            video.append(item)
        elif codec_type == "subtitle":
            subtitles.append(item)
    return {
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
    }


def classify_media_error(detail):
    detail = (detail or "").strip()
    lower = detail.lower()
    if "moov atom not found" in lower:
        return (
            "mp4 metadata is at the end of the file and the remote server does "
            "not support byte-range reads correctly; this URL cannot be "
            "streamed without downloading/remuxing the full file first."
        )
    if "403 forbidden" in lower:
        return "remote server returned 403 Forbidden for ffmpeg/ffprobe."
    if "invalid data found when processing input" in lower:
        return "ffmpeg could not read this media stream."
    return detail[-1200:] or "ffmpeg failed"


def read_log_tail(path, limit=1200):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def runtime_info():
    return {
        "app": APP_NAME,
        "version": VERSION,
        "platform": current_platform(),
        "arch": current_arch(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "manifest_url": UPDATE_MANIFEST_URL,
        "auto_update": AUTO_UPDATE_ENABLED,
        "capabilities": {"forward": True},
    }


def current_platform():
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name.startswith("win"):
        return "windows"
    if name == "linux":
        return "linux"
    return name


def current_arch():
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine


def version_tuple(value):
    parts = []
    for part in str(value or "").strip().lstrip("v").split("."):
        number = ""
        for char in part:
            if not char.isdigit():
                break
            number += char
        parts.append(int(number or "0"))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer_version(candidate, current):
    return version_tuple(candidate) > version_tuple(current)


def update_state(**changes):
    with LOCK:
        UPDATE_STATE.update(changes)


def update_snapshot():
    with LOCK:
        return dict(UPDATE_STATE)


def fetch_update_manifest():
    request = Request(
        UPDATE_MANIFEST_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{APP_NAME}/{VERSION}",
        },
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def select_update_asset(manifest):
    assets = manifest.get("assets") or []
    system = current_platform()
    arch = current_arch()
    preferred_formats = {
        "windows": ["setup", "exe", "msi"],
        "linux": linux_package_preference(),
        "macos": ["pkg", "dmg", "zip"],
    }.get(system, [])

    candidates = [
        asset for asset in assets
        if asset.get("platform") == system
        and asset.get("arch", arch) in {arch, "universal", "all"}
    ]
    for fmt in preferred_formats:
        for asset in candidates:
            if str(asset.get("format", "")).lower() == fmt:
                return asset
    return None


def linux_package_preference():
    if shutil.which("dpkg") or shutil.which("apt"):
        return ["deb", "rpm"]
    if shutil.which("rpm"):
        return ["rpm", "deb"]
    return ["deb", "rpm"]


def download_update_asset(asset, version):
    url = asset.get("url")
    if not url or urlparse(url).scheme != "https":
        raise RuntimeError("update asset must use an https URL")
    suffix = Path(urlparse(url).path).suffix or f".{asset.get('format', 'bin')}"
    target = Path(tempfile.gettempdir()) / f"HydraStreamer-{version}-{current_platform()}-{current_arch()}{suffix}"
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
    with urlopen(request, timeout=120) as response, open(target, "wb") as output:
        shutil.copyfileobj(response, output)
    expected_hash = str(asset.get("sha256") or "").lower()
    if expected_hash:
        actual_hash = sha256_file(target)
        if actual_hash != expected_hash:
            target.unlink(missing_ok=True)
            raise RuntimeError("downloaded update hash mismatch")
    return target


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def install_update_asset(path, asset):
    fmt = str(asset.get("format") or path.suffix.lstrip(".")).lower()
    system = current_platform()
    if system == "windows":
        if fmt == "msi":
            subprocess.Popen(["msiexec", "/i", str(path), "/passive"], creationflags=CREATE_NO_WINDOW)
        elif fmt == "setup":
            install_windows_setup_update(path)
        elif fmt == "exe":
            install_windows_exe_update(path)
        else:
            raise RuntimeError(f"unsupported windows update format: {fmt}")
        return
    if system == "macos":
        if fmt == "pkg":
            subprocess.Popen(["open", str(path)])
        elif fmt == "dmg":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["open", str(path.parent)])
        return
    if system == "linux":
        if fmt == "deb":
            installer = ["dpkg", "-i", str(path)] if os.geteuid() == 0 else ["pkexec", "dpkg", "-i", str(path)]
        elif fmt == "rpm":
            installer = ["rpm", "-Uvh", str(path)] if os.geteuid() == 0 else ["pkexec", "rpm", "-Uvh", str(path)]
        else:
            installer = ["xdg-open", str(path.parent)]
        subprocess.Popen(installer)
        return
    raise RuntimeError(f"unsupported update platform: {system}")


def windows_app_paths():
    app_home = Path(os.environ.get("LOCALAPPDATA", str(app_dir()))) / "HydraStreamer"
    return app_home / "HydraStreamer.exe", app_home / "logs" / "hydrastreamer.log"


def windows_restart_lines(exe):
    # Restart through the Scheduled Task when present (install-windows.ps1),
    # otherwise start the exe directly (Inno Setup startup shortcut installs).
    _, log_file = windows_app_paths()
    return [
        "Start-ScheduledTask -TaskName 'HydraStreamer'",
        "Start-Sleep -Seconds 3",
        "if (-not (Get-Process -Name 'HydraStreamer' -ErrorAction SilentlyContinue)) {",
        f"  Start-Process -FilePath {ps_quote(str(exe))} -ArgumentList '--log-file',{ps_quote(str(log_file))}",
        "}",
    ]


def run_windows_update_script(lines):
    script = Path(tempfile.gettempdir()) / "HydraStreamer-update.ps1"
    script.write_text("\n".join(lines), encoding="utf-8")
    subprocess.Popen([
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ], creationflags=CREATE_NO_WINDOW)


def install_windows_exe_update(path):
    if current_platform() != "windows":
        raise RuntimeError("windows update called on non-windows platform")
    target, _ = windows_app_paths()
    if not target.exists():
        subprocess.Popen([str(path)], creationflags=CREATE_NO_WINDOW)
        return
    run_windows_update_script([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "Start-Sleep -Seconds 2",
        "Stop-ScheduledTask -TaskName 'HydraStreamer'",
        "Stop-Process -Name 'HydraStreamer' -Force",
        "Start-Sleep -Seconds 1",
        f"Copy-Item -LiteralPath {ps_quote(str(path))} -Destination {ps_quote(str(target))} -Force",
        *windows_restart_lines(target),
    ])


def install_windows_setup_update(path):
    if current_platform() != "windows":
        raise RuntimeError("windows update called on non-windows platform")
    exe, _ = windows_app_paths()
    run_windows_update_script([
        "$ErrorActionPreference = 'SilentlyContinue'",
        "Start-Sleep -Seconds 2",
        "Stop-ScheduledTask -TaskName 'HydraStreamer'",
        "Stop-Process -Name 'HydraStreamer' -Force",
        "Start-Sleep -Seconds 1",
        f"Start-Process -FilePath {ps_quote(str(path))} -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait",
        *windows_restart_lines(exe),
    ])


def ps_quote(value):
    return "'" + value.replace("'", "''") + "'"


def check_for_update(install=False):
    if not UPDATE_MANIFEST_URL:
        return
    update_state(status="checking", checked_at=int(time.time()), error=None)
    manifest = fetch_update_manifest()
    latest = str(manifest.get("version") or "")
    if not latest or not is_newer_version(latest, VERSION):
        update_state(status="current", latest_version=latest or VERSION, asset=None)
        return
    asset = select_update_asset(manifest)
    if not asset:
        update_state(status="available", latest_version=latest, asset=None, error="no compatible asset")
        return
    update_state(status="available", latest_version=latest, asset=safe_asset_info(asset))
    if not install:
        return
    update_state(status="downloading")
    path = download_update_asset(asset, latest)
    update_state(status="installing", asset={**safe_asset_info(asset), "downloaded_to": str(path)})
    install_update_asset(path, asset)


def safe_asset_info(asset):
    return {
        "platform": asset.get("platform"),
        "arch": asset.get("arch"),
        "format": asset.get("format"),
        "url": asset.get("url"),
        "sha256": asset.get("sha256"),
        "size": asset.get("size"),
    }


def job_key(url, audio_index, start_time, cookies=None):
    raw = f"{url}\n{audio_index}\n{int(start_time)}\n{cookies or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def start_job(url, audio_index, start_time, cookies=None):
    ffmpeg = binary_path("ffmpeg")
    key = job_key(url, audio_index, start_time, cookies)
    with LOCK:
        existing = JOBS.get(key)
        if existing and existing["process"].poll() is None:
            existing["last_access"] = time.time()
            return key, existing

        out_dir = ROOT / key
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        playlist = out_dir / "index.m3u8"
        log_file = out_dir / "ffmpeg.log"

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        if start_time > 0:
            cmd.extend(["-ss", str(int(start_time))])
        if cookies:
            cmd.extend(["-cookies", cookies])
        cmd.extend([
            "-i",
            url,
            "-map",
            "0:v:0",
            "-map",
            f"0:{audio_index}",
            "-sn",
            "-dn",
            "-fflags",
            "+genpts+discardcorrupt",
            "-avoid_negative_ts",
            "make_zero",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-g",
            "48",
            "-sc_threshold",
            "0",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments",
            "-hls_base_url",
            f"/{key}/",
            "-start_number",
            "0",
            "-hls_segment_filename",
            str(out_dir / "seg_%05d.ts"),
            str(playlist),
        ])
        log = open(log_file, "ab")
        process = subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=CREATE_NO_WINDOW)
        job = {
            "url": url,
            "audio_index": audio_index,
            "start_time": start_time,
            "dir": out_dir,
            "playlist": playlist,
            "log_file": log_file,
            "process": process,
            "last_access": time.time(),
        }
        JOBS[key] = job
        return key, job


def throttle_forward(host):
    now = time.time()
    with LOCK:
        last = FORWARD_THROTTLE.get(host, 0.0)
        if now - last < 1.0:
            return False
        FORWARD_THROTTLE[host] = now
        return True


class DummyProcess:
    def poll(self):
        return 0


class Handler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path == "/forward":
            return self.handle_forward()
        return json_response(self, 404, {"error": "not_found"})

    def handle_forward(self):
        origin = self.headers.get("Origin")
        if origin and origin not in FORWARD_ALLOWED_ORIGINS:
            return json_response(self, 403, {"error": "origin_not_allowed"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > FORWARD_MAX_BODY:
            return json_response(self, 400, {"error": "invalid_length"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return json_response(self, 400, {"error": "invalid_json"})

        url = str(payload.get("url") or "")
        method = str(payload.get("method") or "POST").upper()
        headers = payload.get("headers") or {}
        body = payload.get("body")

        target = urlparse(url)
        host = (target.hostname or "").lower()
        # https obligatoire ; http toléré uniquement vers loopback (tests/mock
        # local — ces hôtes ne sont jamais dans l'allowlist par défaut).
        scheme_ok = target.scheme == "https" or (
            target.scheme == "http" and host in {"127.0.0.1", "localhost"}
        )
        if not scheme_ok or host not in FORWARD_ALLOWED_HOSTS:
            return json_response(self, 403, {"error": "host_not_allowed"})
        if method not in {"GET", "POST"} or not isinstance(headers, dict):
            return json_response(self, 400, {"error": "method_not_allowed"})
        if not throttle_forward(host):
            return json_response(self, 429, {"error": "rate_limited"})

        data = None
        if method == "POST":
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers = {**headers, "Content-Type": "application/json"}
            elif isinstance(body, str):
                data = body.encode("utf-8")
        safe_headers = {
            k: str(v) for k, v in headers.items()
            if isinstance(k, str) and k.lower() in FORWARD_HEADER_ALLOWLIST
        }

        request = Request(url, data=data, method=method, headers=safe_headers)
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(FORWARD_MAX_RESPONSE + 1)
                status = response.status
        except HTTPError as exc:
            # 4xx/5xx still carry the API's JSON error body the caller needs.
            raw = exc.read(FORWARD_MAX_RESPONSE + 1)
            status = exc.code
        except Exception as exc:
            return json_response(self, 502, {"error": "forward_failed", "detail": str(exc)[:300]})

        return json_response(self, 200, {
            "status": status,
            "body": raw[:FORWARD_MAX_RESPONSE].decode("utf-8", errors="replace"),
            "truncated": len(raw) > FORWARD_MAX_RESPONSE,
        })

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            try:
                ffmpeg = binary_path("ffmpeg")
                ffprobe = binary_path("ffprobe")
            except Exception as exc:
                return json_response(
                    self,
                    503,
                    {"ok": False, "version": VERSION, "error": str(exc)},
                )
            return json_response(
                self,
                200,
                {
                    "ok": True,
                    "version": VERSION,
                    "ffmpeg": ffmpeg,
                    "ffprobe": ffprobe,
                    "capabilities": {"forward": True},
                },
            )

        if parsed.path == "/version":
            return json_response(
                self,
                200,
                {
                    **runtime_info(),
                    "update": update_snapshot(),
                },
            )

        if parsed.path == "/probe":
            url = first(params, "url")
            cookies = first(params, "cookies")
            if not valid_url(url):
                return json_response(self, 400, {"error": "invalid_url"})
            try:
                result = probe(url, cookies)
                print(
                    "[hydra-streamer] probe:",
                    f"duration={result.get('duration')}",
                    f"audio={len(result.get('audio') or [])}",
                )
                return json_response(self, 200, result)
            except Exception as exc:
                return json_response(self, 500, {"error": str(exc)})

        if parsed.path == "/stream.m3u8":
            url = first(params, "url")
            audio = first(params, "audio") or "1"
            start = first(params, "start") or "0"
            cookies = first(params, "cookies")
            if not valid_url(url):
                return json_response(self, 400, {"error": "invalid_url"})
            try:
                audio_index = int(audio)
            except ValueError:
                return json_response(self, 400, {"error": "invalid_audio"})
            try:
                start_time = max(0, int(float(start)))
            except ValueError:
                return json_response(self, 400, {"error": "invalid_start"})

            try:
                key, job = start_job(url, audio_index, start_time, cookies)
            except Exception as exc:
                return json_response(self, 500, {"error": str(exc)})

            playlist = job["playlist"]
            deadline = time.time() + 90
            while time.time() < deadline and not playlist.exists():
                if job["process"].poll() is not None:
                    break
                time.sleep(0.25)

            if not playlist.exists():
                if job["process"].poll() is not None:
                    detail = classify_media_error(read_log_tail(job.get("log_file")))
                    return json_response(
                        self,
                        502,
                        {
                            "error": "transcode_failed",
                            "detail": detail,
                            "job": key,
                        },
                    )
                return json_response(
                    self,
                    503,
                    {
                        "error": "playlist_not_ready",
                        "retry": f"/stream.m3u8?url={url}&audio={audio_index}&start={start_time}",
                        "job": key,
                    },
                )
            self.path = f"/{key}/index.m3u8"
            return self.serve_static()

        return self.serve_static()

    def serve_static(self):
        parsed = urlparse(self.path)
        rel = unquote(parsed.path).lstrip("/")
        key = rel.split("/", 1)[0]
        with LOCK:
            if key in JOBS:
                JOBS[key]["last_access"] = time.time()
        target = (ROOT / rel).resolve()
        if not str(target).startswith(str(ROOT.resolve())):
            return text_response(self, 403, "forbidden")
        if target.suffix == ".m3u8":
            self.extensions_map[".m3u8"] = "application/vnd.apple.mpegurl"
        elif target.suffix == ".ts":
            self.extensions_map[".ts"] = "video/mp2t"
        try:
            return SimpleHTTPRequestHandler.do_GET(self)
        except BrokenPipeError:
            return None

    def translate_path(self, path):
        parsed = urlparse(path)
        rel = unquote(parsed.path).lstrip("/")
        return str((ROOT / rel).resolve())

    def end_headers(self):
        cors(self)
        if (
            self.path.endswith(".m3u8")
            or self.path.endswith(".ts")
            or "/stream.m3u8" in self.path
        ):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        print("[hydra-streamer]", fmt % args)


def first(params, name):
    value = (params.get(name) or [""])[0]
    return value.strip()


def valid_url(url):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def stop_process(process):
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def cleanup_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with LOCK:
            for key, job in list(JOBS.items()):
                process = job.get("process")
                expired = now - float(job.get("last_access") or 0) > IDLE_JOB_TTL_SECONDS
                stopped = process is None or process.poll() is not None
                if expired or stopped:
                    stop_process(process)
                    shutil.rmtree(job.get("dir"), ignore_errors=True)
                    JOBS.pop(key, None)


def update_loop():
    if not AUTO_UPDATE_ENABLED:
        update_state(status="disabled")
        return
    # Delay the first check so startup remains instant for the local player.
    time.sleep(15)
    while True:
        try:
            check_for_update(install=True)
        except Exception as exc:
            update_state(
                status="error",
                checked_at=int(time.time()),
                error=str(exc),
            )
        time.sleep(max(300, AUTO_UPDATE_INTERVAL_SECONDS))


def main():
    parser = argparse.ArgumentParser(description="Hydra local HLS transcoder")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--log-file")
    parser.add_argument("--version", action="version", version=f"HydraStreamer {VERSION}")
    args = parser.parse_args()

    global LOG_HANDLE
    if args.log_file:
        log_path = Path(args.log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        LOG_HANDLE = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = LOG_HANDLE
        sys.stderr = LOG_HANDLE

    if args.clean and ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)

    binary_path("ffmpeg")
    binary_path("ffprobe")

    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HydraStreamer {VERSION} listening on http://{args.host}:{args.port}")
    print(f"HLS cache: {ROOT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

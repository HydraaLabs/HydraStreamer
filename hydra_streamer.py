#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import platform
import re
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
VERSION = "0.4.1"
DEFAULT_UPDATE_MANIFEST_URL = "https://hydracker.com/hydrastreamer/releases/latest.json"
UPDATE_MANIFEST_URL = os.environ.get("HYDRASTREAMER_UPDATE_URL", DEFAULT_UPDATE_MANIFEST_URL)
AUTO_UPDATE_ENABLED = os.environ.get("HYDRASTREAMER_AUTO_UPDATE", "1").lower() not in {"0", "false", "no"}
AUTO_UPDATE_INTERVAL_SECONDS = int(os.environ.get("HYDRASTREAMER_UPDATE_INTERVAL", str(6 * 60 * 60)))
ROOT = Path(tempfile.gettempdir()) / "hydra-streamer"
JOBS = {}
LOCK = threading.Lock()
LOG_HANDLE = None
IDLE_JOB_TTL_SECONDS = 900
# Résilience du transcodage : la CDN (1Fichier) coupe régulièrement la
# connexion HTTP en plein fichier ("stream ends prematurely"). Quand ffmpeg
# meurt, on le relance depuis le dernier segment produit (-ss + -start_number
# + append_list) pour continuer la MÊME playlist au lieu de tout recommencer.
# Le transcodage suit un téléchargement en cours : ffmpeg atteint l'EOF du
# fichier partiel, meurt, et le watchdog le relance quand le fichier a
# grandi — cap élevé tant que le download progresse.
MAX_RESPAWNS_PER_JOB = 60
RESPAWN_MIN_INTERVAL_SECONDS = 8

# ─── Cache de téléchargement ─────────────────────────────────────────────────
# Plutôt que de transcoder depuis l'URL CDN (coupures + expiration), on
# télécharge le fichier dans un répertoire de cache avec reprise, puis on
# transcode depuis le disque (y compris pendant que le fichier se télécharge
# encore — lecture d'un MKV qui grandit). Le cache est réutilisé entre
# sessions (re-visionnage instantané) et géré en LRU.
# /var/cache/hydrastreamer (systemd CacheDirectory) : persiste aux restarts
# et upgrades du service, contrairement à /tmp sous PrivateTmp.
CACHE_DIR = Path(os.environ.get(
    "HYDRASTREAMER_CACHE_DIR",
    "/var/cache/hydrastreamer" if os.name != "nt" else str(ROOT / "cache"),
))
CACHE_MAX_BYTES = 20 * 1024 ** 3          # plafond global du cache (20 Go)
CACHE_TTL_SECONDS = 24 * 3600             # durée de vie d'un fichier caché
DOWNLOAD_MIN_BYTES = 16 * 1024 * 1024     # minimum avant de lancer ffmpeg
DOWNLOAD_WAIT_SECONDS = 90                # attente max du minimum téléchargé
DOWNLOAD_MAX_ATTEMPTS = 20
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
# Override optionnel des origines (liste exacte CSV). Sans override, TOUTES
# les origines hydracker.* sont acceptées (com/net/site/local + sous-domaines)
# via forward_origin_allowed().
FORWARD_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get("HYDRASTREAMER_FORWARD_ORIGINS", "").split(",")
    if o.strip()
}


def forward_origin_allowed(origin):
    # Pas d'en-tête Origin (client non-navigateur) : accepté, comme avant.
    if not origin:
        return True
    if FORWARD_ALLOWED_ORIGINS:
        return origin in FORWARD_ALLOWED_ORIGINS
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    parts = host.split(".")
    # hydracker doit être le domaine enregistré (avant-dernier label) :
    # hydracker.com/net/site/local + sous-domaines (app., streaming., www.…).
    # `hydracker.evil.com` est rejeté (hydracker n'est qu'un sous-domaine).
    if len(parts) < 2 or parts[-2] != "hydracker":
        return False
    # https obligatoire, http toléré en dev (.local uniquement).
    return parsed.scheme == "https" or host.endswith(".local")
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
        if origin and forward_origin_allowed(origin):
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


def job_key(url, audio_index, start_time, cookies=None, lien_id=None):
    # La clé du job HLS identifie le CONTENU, pas l'URL CDN (qui change à
    # chaque re-résolution) : avec un lien id, une re-résolution réutilise le
    # MÊME job et la playlist continue au lieu de repartir à seg_00000.
    if lien_id:
        raw = f"lien:{lien_id}\n{audio_index}\n{int(start_time)}".encode("utf-8")
    else:
        raw = f"{url}\n{audio_index}\n{int(start_time)}\n{cookies or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


# Code hébergeur : `/?CODE` (20-24 alphanumériques). Utilisé par le frontend
# pour décider de passer `file=` (clé de cache stable) — les URLs CDN
# (`a-14.1fichier.com/p2157…`) n'ont pas de `/?` → rejetées naturellement.
SHARE_CODE_RE = re.compile(r"/\?([a-zA-Z0-9]{20,24})(?:&|$)")


def ensure_virtual_segment():
    # Segment TS valide (4s noir + silence) servi pour les entrées
    # "virtuelles" des playlists offset (région [0, T) non transcodée) —
    # hls.js les télécharge parfois (alignement, prefetch) et un 404 serait
    # fatal à la lecture.
    target = ROOT / "__virtual_skip__" / "seg.ts"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg = binary_path("ffmpeg")
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=black:s=320x240:r=25:d=4",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-f", "mpegts", str(target),
        ], timeout=60, capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    return target if target.exists() else None


def cache_key_for(url, file_hint=None, lien_id=None):
    # Nom du fichier cache : l'ID du lien quand il est fourni (identifiant
    # stable, survit aux re-résolutions et aux changements de domaine),
    # sinon md5(url)[:20]. JAMAIS l'URL CDN seule en présence d'un id/hint.
    if lien_id:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(lien_id))[:32]
        if safe:
            return f"lien-{safe}"
    basis = file_hint or url
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:20]


def total_size_for(url, cookies=None):
    # Taille totale du fichier distant : HEAD d'abord, sinon une requête
    # Range d'1 octet (certaines CDN refusent HEAD mais répondent à Range).
    def run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)

    head_cmd = ["curl", "-fsSIL", "--max-time", "15"]
    if cookies:
        head_cmd.extend(["-b", cookies])
    head_cmd.append(url)
    out = run(head_cmd).stdout or ""
    for line in out.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    range_cmd = ["curl", "-fsSL", "--max-time", "15", "-r", "0-0", "-o", os.devnull, "-w", "%{http_code} %header{content-range}"]
    if cookies:
        range_cmd.extend(["-b", cookies])
    range_cmd.append(url)
    out = run(range_cmd).stdout or ""
    match = re.search(r"/(\d+)\s*$", out)
    return int(match.group(1)) if match else 0


def download_file(url, part_path, final_path, state, cookies=None):
    # Télécharge avec reprise (Range) en boucle jusqu'au fichier complet.
    # curl -C - reprend à la taille du .part existant ; --fail stoppe net sur
    # erreur HTTP (URL expirée → inutile de réessayer).
    state["total_bytes"] = total_size_for(url, cookies)
    for attempt in range(DOWNLOAD_MAX_ATTEMPTS):
        if state.get("cancelled") or final_path.exists():
            return final_path.exists()
        cmd = [
            "curl", "-fSL", "--silent", "--show-error",
            "--retry", "3", "--retry-delay", "2", "--retry-all-errors",
            "-C", "-", "-o", str(part_path),
        ]
        if cookies:
            cmd.extend(["-b", cookies])
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, timeout=7200, creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0 and part_path.exists():
            part_path.replace(final_path)
            return True
        if proc is not None and proc.returncode == 22:
            # Erreur HTTP définitive (403/404) — l'URL est morte.
            state["error"] = "http_error"
            return False
        state["error"] = "network_error"
        time.sleep(min(30, 2 ** min(attempt, 4)))
    return False


def cache_maintenance():
    # TTL + LRU : supprime les fichiers expirés, puis les plus anciens tant
    # que le cache dépasse le plafond. Ne touche pas aux fichiers en cours de
    # téléchargement (.part récents).
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    files = []
    total = 0
    for path in CACHE_DIR.iterdir():
        if path.suffix == ".part":
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, path))
        total += st.st_size
    for mtime, size, path in files:
        if now - mtime > CACHE_TTL_SECONDS:
            path.unlink(missing_ok=True)
            total -= size
    for mtime, size, path in sorted(files):
        if total <= CACHE_MAX_BYTES:
            break
        if path.exists():
            path.unlink(missing_ok=True)
            total -= size


DOWNLOADS = {}


def start_download(url, cache_name, cookies=None):
    # Lance (ou réutilise) LE téléchargement du fichier en cache — un seul
    # downloader par fichier, jamais d'écrivains concurrents sur le .part
    # (corruption + bans CDN). Retourne (final_path, state).
    final_path = CACHE_DIR / f"{cache_name}.bin"
    part_path = CACHE_DIR / f"{cache_name}.part"
    with LOCK:
        if final_path.exists():
            return final_path, {"cancelled": False, "error": None, "done": True}
        existing = DOWNLOADS.get(cache_name)
        if existing:
            state = existing["state"]
            # URL morte (403) mais nouvelle URL fournie (re-résolution) → on
            # relance avec la nouvelle URL, en reprenant le .part existant.
            if state.get("error") == "http_error" and url != state.get("url"):
                state["error"] = None
                state["url"] = url
                state["cancelled"] = True  # stoppe l'ancienne boucle
                cookies_to_use = cookies
                DOWNLOADS.pop(cache_name, None)
            else:
                return final_path, state
        state = {"cancelled": False, "error": None, "done": False, "url": url}
        DOWNLOADS[cache_name] = {"state": state}
        cookies_to_use = cookies

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def run():
        ok = download_file(state["url"], part_path, final_path, state, cookies_to_use)
        state["done"] = ok
        with LOCK:
            if DOWNLOADS.get(cache_name, {}).get("state") is state:
                DOWNLOADS.pop(cache_name, None)

    threading.Thread(target=run, daemon=True).start()
    return final_path, state


def wait_for_data(path, state, minimum=DOWNLOAD_MIN_BYTES, timeout=DOWNLOAD_WAIT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state.get("done") or state.get("error"):
            break
        current = path if path.exists() else path.with_suffix(".part")
        try:
            if current.exists() and current.stat().st_size >= minimum:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def bytes_needed_for(total, duration, sec):
    # Octets nécessaires pour lire à la position `sec` (proportion + marge).
    if total <= 0 or duration <= 0 or sec <= 0:
        return DOWNLOAD_MIN_BYTES
    return int(total * min(0.99, sec / duration)) + 8 * 1024 * 1024


def coverage_needed(state, start_time, partial_path):
    # Octets nécessaires avant de pouvoir démarrer ffmpeg à -ss start_time :
    # la position de reprise doit être couverte par le téléchargement.
    # Estimation par proportion (durée ffprobe sur le header MKV, taille
    # totale via HEAD/Range), avec marge de 8 Mo.
    total = state.get("total_bytes") or 0
    if total <= 0 or start_time <= 0:
        return DOWNLOAD_MIN_BYTES
    if not state.get("duration"):
        try:
            info = probe(str(partial_path)) if partial_path.exists() else None
            state["duration"] = float((info or {}).get("duration") or 0)
        except Exception:
            state["duration"] = 0
    needed = bytes_needed_for(total, float(state.get("duration") or 0), start_time)
    return max(DOWNLOAD_MIN_BYTES, needed)


def build_ffmpeg_cmd(ffmpeg, url, audio_index, start_time, cookies, out_dir, playlist, key, start_number=0, append=False):
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
    ]
    # Options réseau uniquement (invalides sur une entrée fichier locale).
    if str(url).startswith(("http://", "https://")):
        cmd.extend([
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ])
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
        # append_list : le respawn continue la playlist existante au lieu de
        # l'écraser (continuité du transcodage après une coupure CDN).
        "independent_segments+append_list" if append else "independent_segments",
        "-hls_base_url",
        f"/{key}/",
        "-start_number",
        str(start_number),
        "-hls_segment_filename",
        str(out_dir / "seg_%05d.ts"),
        str(playlist),
    ])
    return cmd


def start_job(url, audio_index, start_time, cookies=None, file_hint=None, lien_id=None):
    ffmpeg = binary_path("ffmpeg")

    # Télécharge d'abord dans le cache (reprise automatique), puis transcode
    # depuis le disque — même pendant que le fichier grandit. Si la position
    # demandée (>0) n'est pas couverte par le cache, on démarre au début.
    cache_name = cache_key_for(url, file_hint, lien_id)
    cache_file, dl_state = start_download(url, cache_name, cookies)
    if not dl_state.get("done") and start_time > 0:
        partial = cache_file.with_suffix(".part")
        try:
            current = partial.stat().st_size if partial.exists() else (
                cache_file.stat().st_size if cache_file.exists() else 0
            )
        except OSError:
            current = 0
        if current < coverage_needed(dl_state, start_time, partial):
            start_time = 0

    key = job_key(url, audio_index, start_time, cookies, lien_id)
    with LOCK:
        existing = JOBS.get(key)
        if existing:
            existing["last_access"] = time.time()
            if existing["process"].poll() is None:
                return key, existing
            # Processus mort : ne JAMAIS effacer un job qui a déjà produit
            # des segments — le watchdog le relance avec append_list (sinon
            # la playlist repart à seg_00000 et le player rembobine à 0 à
            # chaque polling de playlist). Recréation à zéro seulement si
            # rien n'a été produit ET le téléchargement est mort.
            has_segments = any(existing["dir"].glob("seg_*.ts"))
            dl = existing.get("download") or {}
            if has_segments or not dl.get("error"):
                return key, existing
        # pas de job, ou job vide dont le téléchargement a échoué → recréation
        if not dl_state.get("done"):
            wait_for_data(cache_file, dl_state)
        input_path = cache_file if cache_file.exists() else cache_file.with_suffix(".part")
        if not input_path.exists():
            raise RuntimeError(f"download_failed: {dl_state.get('error') or 'no_data'}")

        out_dir = ROOT / key
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        playlist = out_dir / "index.m3u8"
        log_file = out_dir / "ffmpeg.log"

        cmd = build_ffmpeg_cmd(
            ffmpeg, str(input_path), audio_index, start_time, None, out_dir, playlist, key,
        )
        log = open(log_file, "ab")
        process = subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=CREATE_NO_WINDOW)
        job = {
            "url": url,
            "audio_index": audio_index,
            "start_time": start_time,
            "cookies": cookies,
            "input_path": str(input_path),
            "cache_file": str(cache_file),
            "download": dl_state,
            "total_bytes": int(dl_state.get("total_bytes") or 0),
            "duration": float(dl_state.get("duration") or 0),
            "dir": out_dir,
            "playlist": playlist,
            "log_file": log_file,
            "process": process,
            "last_access": time.time(),
        }
        JOBS[key] = job
        return key, job


def job_playlist_complete(job):
    try:
        return job["playlist"].exists() and "#EXT-X-ENDLIST" in job["playlist"].read_text(errors="replace")
    except OSError:
        return False


def respawn_job(key, job):
    # Reprend le transcodage au segment suivant le dernier produit, en
    # continuant la même playlist (-start_number + append_list).
    next_index = len(list(job["dir"].glob("seg_*.ts")))
    resume = int(job["start_time"]) + next_index * 4
    ffmpeg = binary_path("ffmpeg")
    cmd = build_ffmpeg_cmd(
        ffmpeg, job.get("input_path") or job["url"], job["audio_index"], resume, None,
        job["dir"], job["playlist"], key, start_number=next_index, append=True,
    )
    log = open(job["log_file"], "ab")
    with LOCK:
        job["process"] = subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=CREATE_NO_WINDOW)
        job["last_access"] = time.time()
    print(f"[hydra-streamer] respawn job {key} au segment {next_index} (-ss {resume})")


def watchdog_loop():
    while True:
        time.sleep(5)
        with LOCK:
            items = list(JOBS.items())
        for key, job in items:
            process = job.get("process")
            if process is not None and process.poll() is None:
                continue
            if job_playlist_complete(job):
                continue
            now = time.time()
            with LOCK:
                attempts = int(job.get("respawns", 0))
                if attempts >= MAX_RESPAWNS_PER_JOB:
                    continue
                if now - float(job.get("last_respawn") or 0) < RESPAWN_MIN_INTERVAL_SECONDS:
                    continue
                # Téléchargement en cours : ne respawn que si le fichier a
                # grandi depuis la dernière tentative ET couvre la position
                # de reprise (sinon ffmpeg meurt immédiatement au même EOF
                # partiel, ou ne trouve aucune frame au -ss demandé).
                dl = job.get("download") or {}
                if dl and not dl.get("done"):
                    if dl.get("error"):
                        continue
                    current_size = 0
                    try:
                        cache_file = Path(job.get("cache_file") or "")
                        partial = cache_file if cache_file.exists() else cache_file.with_suffix(".part")
                        current_size = partial.stat().st_size if partial.exists() else 0
                    except OSError:
                        current_size = 0
                    if current_size <= int(job.get("last_size", 0)) + 2 * 1024 * 1024:
                        continue
                    # Durée encore inconnue (fichier trop petit à la création
                    # du job) : on la re-sonde maintenant qu'il y a de la data.
                    if not job.get("duration") and current_size >= DOWNLOAD_MIN_BYTES:
                        try:
                            info = probe(str(partial))
                            job["duration"] = float((info or {}).get("duration") or 0)
                        except Exception:
                            pass
                    segs_done = len(list(job["dir"].glob("seg_*.ts")))
                    resume_sec = int(job["start_time"]) + segs_done * 4
                    if current_size < bytes_needed_for(
                        int(job.get("total_bytes") or 0),
                        float(job.get("duration") or 0),
                        resume_sec,
                    ):
                        continue
                    job["last_size"] = current_size
                job["respawns"] = attempts + 1
                job["last_respawn"] = now
            respawn_job(key, job)


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
        if origin and not forward_origin_allowed(origin):
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
            file_hint = first(params, "file")
            lien_id = first(params, "id")
            if not valid_url(url):
                return json_response(self, 400, {"error": "invalid_url"})
            try:
                # Résultat de probe caché par fichier (clé lien) : le contenu
                # ne change jamais → on évite de re-télécharger/re-prober.
                cache_name = cache_key_for(url, file_hint or None, lien_id or None)
                probe_cache = CACHE_DIR / f"{cache_name}.probe.json"
                if probe_cache.exists():
                    try:
                        cached_result = json.loads(probe_cache.read_text(encoding="utf-8"))
                        return json_response(self, 200, cached_result)
                    except (OSError, ValueError):
                        probe_cache.unlink(missing_ok=True)

                # Probe depuis le cache quand il est déjà là (re-lecture
                # instantanée) ; sinon démarre le téléchargement et probe le
                # fichier partiel — jamais l'URL directement (expiration,
                # rate-limit CDN → le player retombait en lecture directe).
                probe_input = url
                cached = CACHE_DIR / f"{cache_name}.bin"
                if cached.exists():
                    probe_input = str(cached)
                    cookies = None
                else:
                    cache_file, dl_state = start_download(url, cache_name, cookies)
                    wait_for_data(cache_file, dl_state, minimum=4 * 1024 * 1024, timeout=30)
                    partial = cache_file if cache_file.exists() else cache_file.with_suffix(".part")
                    if partial.exists():
                        probe_input = str(partial)
                        cookies = None
                result = probe(probe_input, cookies)
                # Header MKV pas encore téléchargé (pistes vides) : attendre
                # plus de données et re-sonder une fois avant de répondre.
                if (
                    not result.get("video")
                    and not result.get("audio")
                    and not cached.exists()
                    and Path(probe_input).suffix == ".part"
                ):
                    wait_for_data(cache_file, dl_state, minimum=32 * 1024 * 1024, timeout=30)
                    if partial.exists() and partial.stat().st_size > 4 * 1024 * 1024:
                        result = probe(str(partial), None)
                # Ne met en cache que les probes COMPLETS (pistes trouvées) —
                # un probe sur fichier partiel peut ne voir aucune piste et
                # ne doit pas être figé pour les lectures suivantes.
                if result.get("video") or result.get("audio"):
                    try:
                        CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        probe_cache.write_text(json.dumps(result), encoding="utf-8")
                    except OSError:
                        pass
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
            file_hint = first(params, "file")
            lien_id = first(params, "id")
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
                key, job = start_job(url, audio_index, start_time, cookies, file_hint=file_hint or None, lien_id=lien_id or None)
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
            if start_time > 0:
                return self.serve_offset_playlist(job)
            return self.serve_static()

        return self.serve_static()

    def serve_offset_playlist(self, job):
        # Réécrit la playlist d'un job démarré à -ss T pour qu'elle représente
        # la timeline complète de la vidéo : le lecteur affiche la position
        # absolue (ex. 20:00) au lieu de repartir à 0.
        #  - EXT-X-PLAYLIST-TYPE:EVENT → la playlist est seekable partout (pas
        #    de saut automatique vers le live edge)
        #  - segments "virtuels" pour [0, T) (jamais téléchargés : la lecture
        #    démarre à EXT-X-START:TIME-OFFSET=T et les seeks en arrière avant
        #    T relancent un nouveau job côté frontend)
        try:
            text = job["playlist"].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return text_response(self, 404, "playlist_not_found")

        start = float(job.get("start_time") or 0)
        seg_dur = 4.0  # hls_time du transcodage
        virtual_count = int(start // seg_dur)

        out = []
        for line in text.splitlines():
            if line.startswith("#EXTM3U") and "#EXT-X-PLAYLIST-TYPE" not in text:
                out.append(line)
                out.append("#EXT-X-PLAYLIST-TYPE:EVENT")
                continue
            if line.startswith("#EXTINF") and not any(
                l.startswith("#EXT-X-START") for l in out
            ):
                out.append(f"#EXT-X-START:TIME-OFFSET={start:.3f}")
                for _ in range(virtual_count):
                    out.append(f"#EXTINF:{seg_dur:.6f},")
                    out.append("/__virtual_skip__/seg.ts")
            out.append(line)
        body = "\n".join(out) + "\n"
        return text_response(self, 200, body, "application/vnd.apple.mpegurl")

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
        # Segment pas encore produit (transcode derrière le téléchargement) :
        # on attend qu'il apparaisse au lieu de renvoyer un 404 fatal au
        # player. Timeout borné — le watchdog ffmpeg fait le reste.
        if target.suffix == ".ts" and not target.exists() and key in JOBS:
            deadline = time.time() + 60
            while time.time() < deadline and not target.exists():
                with LOCK:
                    job = JOBS.get(key)
                if job is None:
                    break
                proc = job.get("process")
                dl = job.get("download") or {}
                # Stoppe l'attente si plus rien ne produira le segment.
                if (proc is None or proc.poll() is not None) and job_playlist_complete(job):
                    break
                if dl.get("error"):
                    break
                time.sleep(0.25)
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
                # Ne JAMAIS supprimer un job parce que ffmpeg s'est terminé :
                # le transcodage va ~5x plus vite que la lecture, donc ffmpeg
                # finit bien avant le spectateur — les segments sur disque
                # doivent rester servables jusqu'à la fin du TTL d'inactivité
                # (sinon la lecture repart au début en plein milieu).
                if expired:
                    stop_process(process)
                    if job.get("download"):
                        job["download"]["cancelled"] = True
                    shutil.rmtree(job.get("dir"), ignore_errors=True)
                    JOBS.pop(key, None)
        # Maintenance du cache de téléchargement (TTL 24 h + LRU 20 Go).
        try:
            cache_maintenance()
        except Exception:
            pass


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
    ensure_virtual_segment()

    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HydraStreamer {VERSION} listening on http://{args.host}:{args.port}")
    print(f"HLS cache: {ROOT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

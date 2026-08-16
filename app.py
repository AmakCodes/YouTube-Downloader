#!/usr/bin/env python3
"""
YouTube Downloader Pro — Web Edition (Flask backend)

Ports the info-fetch / download / progress logic from the original Tkinter
desktop app to a small REST API that the static HTML/CSS/JS frontend
(templates/index.html + static/js/app.js) talks to.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""

import os
import re
import time
import uuid
import json
import shutil
import platform
import subprocess
import threading
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory, abort

try:
    import yt_dlp
except ImportError:
    raise SystemExit("yt-dlp is required. Install it with: pip install yt-dlp")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = BASE_DIR / "downloads"
DOWNLOAD_ROOT.mkdir(exist_ok=True)
COOKIE_FILE = BASE_DIR / "cookies.txt"

# ---------------------------------------------------------------------------
# Cookies can also be provided via an env var instead of the upload form.
# Useful on hosts like Render where the free-tier disk isn't persistent —
# an uploaded cookies.txt disappears on the next deploy/restart, but an
# env var is set once in the dashboard and re-applied automatically every
# time the app boots. Paste the *entire contents* of a Netscape-format
# cookies.txt file (multi-line is fine) into an env var named
# YTDLP_COOKIES, and this writes it to COOKIE_FILE on startup.
# ---------------------------------------------------------------------------
_env_cookies = os.environ.get("YTDLP_COOKIES")
if _env_cookies:
    COOKIE_FILE.write_text(_env_cookies, encoding="utf-8")
    print(f"[startup] YTDLP_COOKIES found ({len(_env_cookies)} chars) -> wrote {COOKIE_FILE}", flush=True)
else:
    print("[startup] YTDLP_COOKIES env var not set or empty — cookies.txt not written from env", flush=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB, cookie file uploads only

# ---------------------------------------------------------------------------
# Deployment mode: when running on a real host (Render/Railway/etc set
# $PORT), we're "cloud" — native OS dialogs (folder picker, reveal-in-
# -explorer) are disabled since there's no desktop to show them on.
# No login is required — this app is open to anyone with the URL.
# ---------------------------------------------------------------------------
IS_CLOUD = "PORT" in os.environ

# ---------------------------------------------------------------------------
# Currently-selected download folder. Defaults to ./downloads, but the user
# can change it from the UI (native folder picker or a typed path).
# ---------------------------------------------------------------------------
_download_dir = DOWNLOAD_ROOT
_download_dir_lock = threading.Lock()


def get_download_dir() -> Path:
    with _download_dir_lock:
        return _download_dir


def set_download_dir(path: Path):
    global _download_dir
    with _download_dir_lock:
        _download_dir = path

# ---------------------------------------------------------------------------
# In-memory job registry: job_id -> state dict.
# Fine for a single-user local app; swap for Redis/DB if this ever needs to
# serve multiple simultaneous users.
# ---------------------------------------------------------------------------
jobs = {}
jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Registry of files this app has actually downloaded (persisted to disk).
# The Downloads tab used to list *everything* on disk under the current
# download folder — slow to scan if that folder has a lot in it, and it
# showed unrelated files too. Instead we record each file's absolute path
# the moment a job finishes, and the Downloads tab reads from that instead
# of walking the filesystem.
# ---------------------------------------------------------------------------
REGISTRY_FILE = BASE_DIR / "downloaded_files.json"
_registry_lock = threading.Lock()
_downloaded_files = set()


def _load_registry():
    global _downloaded_files
    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                _downloaded_files = set(json.load(f))
            return
        except Exception:
            _downloaded_files = set()
    else:
        # First run: seed with whatever's already sitting in the default
        # ./downloads folder, since that folder is dedicated to this app.
        # (A folder the user later redirects to isn't seeded — only files
        # this app downloads into it from here on are.)
        try:
            _downloaded_files = {
                str(p) for p in DOWNLOAD_ROOT.rglob("*")
                if p.is_file() and p.suffix not in SKIP_EXTENSIONS
            }
        except Exception:
            _downloaded_files = set()
        _save_registry()


def _save_registry():
    try:
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_downloaded_files), f)
    except Exception:
        pass


def register_downloaded_files(paths):
    if not paths:
        return
    with _registry_lock:
        _downloaded_files.update(str(p) for p in paths)
        _save_registry()


def _snapshot_files(folder: Path):
    """Absolute paths of every file currently under `folder` (recursive)."""
    if not folder.exists():
        return set()
    return {str(p) for p in folder.rglob("*") if p.is_file()}


QUALITY_FORMATS = {
    # Kept only as the set of valid quality keys accepted by the API
    # (used for request validation) — the actual yt-dlp format options
    # now come from build_format_opts() below, not these strings.
    "best": None,
    "1080p": None,
    "720p": None,
    "480p": None,
    "360p": None,
    "audio": None,
    "worst": None,
}

SKIP_EXTENSIONS = {".part", ".ytdl", ".json", ".jpg", ".png", ".webp", ".description"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".wav"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv"}

ANSI_RE = re.compile(r"\x1b\[[\d;]*m")


def clean_error(msg: str) -> str:
    return ANSI_RE.sub("", str(msg)).strip()


class _ErrorCollectingLogger:
    """Passed as yt-dlp's `logger` so that errors which would otherwise be
    silently swallowed by ignoreerrors=True (needed for playlists) still
    get captured and shown to the user, instead of only being visible in
    the server's own logs."""
    def __init__(self):
        self.errors = []

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(clean_error(msg))


def is_playlist_url(url: str) -> bool:
    lowered = url.lower()
    return any(p in lowered for p in ("list=", "playlist", "&list"))


def sanitize_filename(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")
    name = "".join(c for c in name if ord(c) >= 32)
    return " ".join(name.strip()[:100].split())


def format_bytes(size) -> str:
    try:
        size = float(size or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            break
        size /= 1024.0
    return f"{size:.1f} {unit}"


def format_time(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "00:00"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_ffmpeg_cache = {"value": None, "checked_at": 0.0}
_FFMPEG_CACHE_TTL = 300  # seconds; re-probe occasionally in case ffmpeg gets installed later


def check_ffmpeg() -> bool:
    """Check if FFmpeg is reachable, mirroring the desktop app's lookup order.

    This shells out via subprocess, which can take up to ~2s per candidate
    (worse on Windows, which tries several paths). Re-running it on every
    /api/status poll made the page feel slow to load, so the result is
    cached for a few minutes instead of probed on every request.
    """
    now = time.time()
    if _ffmpeg_cache["value"] is not None and (now - _ffmpeg_cache["checked_at"]) < _FFMPEG_CACHE_TTL:
        return _ffmpeg_cache["value"]

    candidates = ["ffmpeg"]
    if platform.system() == "Windows":
        candidates = [
            "ffmpeg.exe",
            "./ffmpeg.exe",
            "./assets/ffmpeg.exe",
            str(BASE_DIR / "ffmpeg.exe"),
            "ffmpeg",
        ]
    found = False
    for candidate in candidates:
        try:
            result = subprocess.run([candidate, "-version"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                found = True
                break
        except Exception:
            continue

    _ffmpeg_cache["value"] = found
    _ffmpeg_cache["checked_at"] = now
    return found


def ydl_common_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        # "android" goes first because Render's IPs (and most cloud/
        # datacenter IPs) trip YouTube's bot-check on the "web" client —
        # even with valid cookies present, "Sign in to confirm you're
        # not a bot" comes back if web is tried first from a datacenter
        # IP. android sidesteps that check. Its trade-off is a smaller/
        # different format list, which is why "web" stays second as a
        # fallback for formats android doesn't expose (e.g. certain
        # 1080p+ combos) — cookies apply to both either way.
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        # Fail fast instead of hanging: a stalled connection to YouTube
        # should surface as a clear error within seconds, not tie up
        # the request until the browser or platform proxy gives up.
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 1,
    }
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def new_job(job_type: str) -> str:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": "starting",
            "message": "Starting…",
            "percentage": 0.0,
            "speed": "0 B/s",
            "eta": "--:--",
            "cancelled": False,
            "paused": False,
            "done": False,
            "error": None,
            "result": None,
            "ydl": None,
        }
    return job_id


def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404, description="Unknown job id")
    return job


def update_job(job_id: str, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)


# ---------------------------------------------------------------------------
# Routes: pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes: status
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    return jsonify({
        "ffmpeg_available": check_ffmpeg(),
        "cookie_available": COOKIE_FILE.exists(),
        "download_folder": str(get_download_dir()),
    })


@app.route("/api/browse-folder", methods=["POST"])
def api_browse_folder():
    """Open a native OS folder picker on the machine running the server.

    Only works for a local, single-user setup where the server and the
    browser share the same desktop (e.g. running `python app.py` on your
    own Windows machine). It won't work on a remote/headless server.
    """
    if IS_CLOUD:
        return jsonify({"error": "Folder picker isn't available on a cloud deploy. Type a path instead."}), 400
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return jsonify({"error": "Folder picker isn't available on this system. Type a path instead."}), 500

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            initialdir=str(get_download_dir()),
            title="Select Download Folder",
        )
        root.destroy()
    except Exception as e:
        return jsonify({"error": f"Could not open folder picker: {clean_error(e)}"}), 500

    if not chosen:
        return jsonify({"folder": None})

    path = Path(chosen)
    path.mkdir(parents=True, exist_ok=True)
    set_download_dir(path)
    return jsonify({"folder": str(path)})


@app.route("/api/set-folder", methods=["POST"])
def api_set_folder():
    """Set the download folder from a typed path (fallback for the picker)."""
    data = request.get_json(silent=True) or {}
    folder = (data.get("folder") or "").strip()
    if not folder:
        return jsonify({"error": "Folder path is empty"}), 400
    path = Path(folder).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 400
    set_download_dir(path)
    return jsonify({"folder": str(path)})


QUALITY_HEIGHT_CAPS = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}


def build_format_opts(quality: str) -> dict:
    """yt-dlp `format` (+ optional `format_sort`) options for a quality choice.

    The previous approach used bracket filters, e.g.
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]".
    Bracket filters *exclude* any format that doesn't satisfy every
    condition — which is a problem because the "android" player client
    (used to dodge YouTube's bot-check on cloud IPs) often returns a
    format list with missing/sparse metadata (no ext tag, no height on
    some entries). That can make every option in the fallback chain
    match zero formats, surfacing as "Requested format is not available"
    even though yt-dlp technically has usable formats to work with.

    format_sort works differently: "bv*+ba/b" matches virtually any
    format combination that exists at all (it only fails if literally
    nothing downloadable was returned), and format_sort just *prefers*
    the format closest to the target height rather than excluding
    anything that doesn't match exactly. This is the combination yt-dlp's
    own docs recommend over manual bracket filtering for exactly this
    kind of cross-client reliability.
    """
    if quality == "audio":
        return {"format": "bestaudio/best"}
    if quality == "worst":
        return {"format": "wv*+wa/w", "format_sort": ["+size"]}
    opts = {"format": "bv*+ba/b"}
    cap = QUALITY_HEIGHT_CAPS.get(quality)  # None for "best" = no cap
    if cap:
        # "res:H" prefers the format closest to (at or below) H over
        # both higher and lower resolutions, without excluding anything
        # if nothing happens to fit under the cap.
        opts["format_sort"] = [f"res:{cap}"]
    return opts


def estimate_filesize(info: dict, quality: str = "best"):
    """Best-effort size estimate for the info card, matching whichever
    quality is currently selected (mirrors QUALITY_FORMATS' logic closely
    enough for an estimate — exact byte counts still depend on what yt-dlp
    picks at download time).

    yt-dlp only gives a clean top-level `filesize`/`filesize_approx` for
    single progressive formats, and even that reflects an arbitrary
    default format, not the one the user picked. So for anything but a
    trivial case we look at info['formats'] directly and add up whichever
    video-only + audio-only streams the selected quality would actually use.
    """
    def fsize(f):
        if not f:
            return 0
        return f.get("filesize") or f.get("filesize_approx") or 0

    formats = info.get("formats") or []

    if quality == "audio":
        audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                      and f.get("vcodec") in (None, "none") and fsize(f)]
        if audio_only:
            best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
            return fsize(best_audio) or None
        return info.get("filesize") or info.get("filesize_approx")

    video_only = [f for f in formats if f.get("vcodec") not in (None, "none") and fsize(f)]
    audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none") and fsize(f)]

    if quality == "worst":
        chosen_video = min(video_only, key=lambda f: f.get("height") or 9999) if video_only else None
    else:
        cap = QUALITY_HEIGHT_CAPS.get(quality)  # None for "best" = no cap
        candidates = video_only
        if cap:
            within_cap = [f for f in candidates if (f.get("height") or 0) <= cap]
            candidates = within_cap or candidates  # fall back if nothing fits the cap
        chosen_video = max(candidates, key=lambda f: f.get("height") or 0) if candidates else None

    chosen_audio = max(audio_only, key=lambda f: f.get("abr") or 0) if audio_only else None

    total = fsize(chosen_video) + fsize(chosen_audio) if (chosen_video or chosen_audio) else 0
    if not total:
        total = info.get("filesize") or info.get("filesize_approx") or 0
    return total or None


# ---------------------------------------------------------------------------
# Routes: fetch video / playlist info
# ---------------------------------------------------------------------------
@app.route("/api/fetch-info", methods=["POST"])
def api_fetch_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")
    if quality not in QUALITY_FORMATS:
        quality = "best"
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    playlist = is_playlist_url(url)
    opts = ydl_common_opts()
    if playlist:
        opts.update({"extract_flat": True, "playlistend": 10})

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 400

    if not info:
        return jsonify({"error": "Failed to fetch video information"}), 400

    if "entries" in info:
        entries = info.get("entries") or []
        count = len(entries)
        if count == 10 and info.get("playlist_count"):
            count = info["playlist_count"]

        # extract_flat playlists don't reliably have a top-level
        # "thumbnail" string — fall back to the playlist's own
        # "thumbnails" list, then to the first video's thumbnail.
        thumbnail = info.get("thumbnail")
        if not thumbnail:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                thumbnail = thumbs[-1].get("url")
        if not thumbnail and entries:
            first = entries[0] or {}
            thumbnail = first.get("thumbnail")
            if not thumbnail:
                first_thumbs = first.get("thumbnails") or []
                if first_thumbs:
                    thumbnail = first_thumbs[-1].get("url")

        payload = {
            "is_playlist": True,
            "title": info.get("title", "Unknown Playlist"),
            "uploader": info.get("uploader", "Unknown Channel"),
            "video_count": count,
            "thumbnail": thumbnail,
        }
    else:
        est_size = estimate_filesize(info, quality)
        payload = {
            "is_playlist": False,
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader", "Unknown"),
            "duration": format_time(info.get("duration", 0)) if info.get("duration") else "--:--",
            "view_count": info.get("view_count", 0),
            "thumbnail": info.get("thumbnail"),
            "filesize": format_bytes(est_size) if est_size else "—",
        }
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Routes: start / control downloads
# ---------------------------------------------------------------------------
@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if quality not in QUALITY_FORMATS:
        return jsonify({"error": "Unknown quality option"}), 400

    playlist = is_playlist_url(url)
    job_id = new_job("playlist" if playlist else "video")

    thread = threading.Thread(target=_run_download, args=(job_id, url, quality, playlist), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = get_job(job_id)
    return jsonify({
        "status": job["status"],
        "message": job["message"],
        "percentage": job["percentage"],
        "speed": job["speed"],
        "eta": job["eta"],
        "paused": job["paused"],
        "done": job["done"],
        "error": job["error"],
        "result": job["result"],
    })


@app.route("/api/pause/<job_id>", methods=["POST"])
def api_pause(job_id):
    job = get_job(job_id)
    update_job(job_id, paused=not job["paused"])
    return jsonify({"paused": jobs[job_id]["paused"]})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    get_job(job_id)
    update_job(job_id, cancelled=True, paused=False, status="cancelled", message="Download cancelled")
    return jsonify({"cancelled": True})


def _run_download(job_id, url, quality, playlist):
    download_dir = get_download_dir()
    try:
        if playlist:
            try:
                info_opts = ydl_common_opts()
                info_opts.update({"extract_flat": True, "playlistend": 1})
                with yt_dlp.YoutubeDL(info_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                title = sanitize_filename(info["title"]) if info and info.get("title") else "YouTube Playlist"
            except Exception:
                title = "YouTube Playlist"
            download_dir = download_dir / title
            download_dir.mkdir(exist_ok=True)
            update_job(job_id, message=f"Playlist: {title}")

        format_opts = build_format_opts(quality)
        outtmpl = str(download_dir / ("%(playlist_index)s - %(title)s.%(ext)s" if playlist else "%(title)s.%(ext)s"))

        ydl_opts = ydl_common_opts()
        ydl_opts.update({
            "retries": 10,
            "fragment_retries": 10,
            "skip_unavailable_fragments": True,
            "keep_fragments": True,
            # Fetch multiple fragments of the video/audio stream in parallel
            # instead of one at a time — this is what actually speeds up the
            # download itself (separate from page/UI speed).
            "concurrent_fragment_downloads": 8,
            "outtmpl": outtmpl,
            "progress_hooks": [lambda d: _progress_hook(job_id, d)],
            "writedescription": False,
            "writeinfojson": False,
            "writethumbnail": False,
            "write_all_thumbnails": False,
            "write_annotations": False,
        })
        ydl_opts.update(format_opts)
        error_logger = _ErrorCollectingLogger()
        ydl_opts["logger"] = error_logger
        if quality == "audio":
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            # Video-only + audio-only streams (needed for 1080p+, see
            # QUALITY_FORMATS above) get merged by FFmpeg into this
            # container. Requires FFmpeg to be installed and on PATH.
            ydl_opts["merge_output_format"] = "mp4"

        update_job(job_id, status="downloading",
                   message=("Starting playlist download…" if playlist else "Starting download…"))

        pre_files = _snapshot_files(download_dir)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            update_job(job_id, ydl=ydl)
            ydl.extract_info(url, download=True)

        job = get_job(job_id)
        if job["cancelled"]:
            update_job(job_id, status="cancelled", message="Download cancelled", done=True)
        else:
            post_files = _snapshot_files(download_dir)
            new_files = {
                p for p in (post_files - pre_files)
                if Path(p).suffix not in SKIP_EXTENSIONS
            }
            register_downloaded_files(new_files)

            # Relative paths (against the *root* download folder, not the
            # per-playlist subfolder) so the frontend can hit
            # /api/downloads/<relpath> to trigger a real browser download
            # for each file that just finished.
            root = get_download_dir().resolve()
            finished_files = []
            for p in sorted(new_files):
                try:
                    relpath = Path(p).resolve().relative_to(root)
                except ValueError:
                    continue
                finished_files.append({"name": Path(p).name, "relpath": str(relpath)})

            if not finished_files:
                # ignoreerrors=True (set in ydl_common_opts, needed so one
                # broken video doesn't abort an entire playlist) means
                # yt-dlp can swallow every single failure and return
                # normally with nothing actually downloaded. Silently
                # reporting "complete" in that case is misleading — treat
                # zero output files as a real failure instead, and surface
                # whatever yt-dlp actually logged via error_logger so the
                # user doesn't have to go dig through Render's logs.
                detail = "; ".join(error_logger.errors[:3]) if error_logger.errors else \
                    "every video may have been unavailable, private, or hit a format issue"
                update_job(job_id, status="error", message="Download failed",
                           error=f"No files were downloaded — {detail}", done=True)
            else:
                update_job(job_id, status="complete", message="Download complete!", percentage=100.0,
                           done=True, result={"folder": str(download_dir), "files": finished_files})
    except Exception as e:
        job = get_job(job_id)
        if not job["cancelled"]:
            update_job(job_id, status="error", message="Download failed", error=clean_error(e), done=True)
    finally:
        update_job(job_id, ydl=None)


def _progress_hook(job_id, d):
    job = get_job(job_id)
    if job["cancelled"]:
        raise Exception("Download cancelled")
    while job["paused"]:
        time.sleep(0.5)
        job = get_job(job_id)
        if job["cancelled"]:
            raise Exception("Download cancelled")

    status = d.get("status", "")
    if status == "downloading":
        downloaded = float(d.get("downloaded_bytes") or 0)
        total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        speed = float(d.get("speed") or 0)
        pct = (downloaded / total * 100) if total > 0 else 0

        if speed > 0 and total > 0 and downloaded < total:
            eta_str = format_time((total - downloaded) / speed)
        else:
            eta_str = "--:--"

        message = "Downloading…"
        info_dict = d.get("info_dict") or {}
        if "playlist_index" in info_dict and "title" in info_dict:
            title = info_dict.get("title", "Unknown")
            short = (title[:40] + "…") if len(title) > 40 else title
            message = f"Downloading video {info_dict.get('playlist_index', '?')}: {short}"

        update_job(job_id, percentage=pct, speed=format_bytes(speed), eta=eta_str, message=message)

    elif status == "finished":
        update_job(job_id, percentage=100.0, speed="0 B/s", eta="00:00", message="Finalizing…")


# ---------------------------------------------------------------------------
# Routes: downloads library
# ---------------------------------------------------------------------------
def _file_entry(item: Path, root: Path):
    if item.suffix in AUDIO_EXTENSIONS:
        ftype = "Audio"
    elif item.suffix in VIDEO_EXTENSIONS:
        ftype = "Video"
    else:
        ftype = "Other"
    stats = item.stat()
    return {
        "name": item.name,
        "relpath": str(item.relative_to(root)),
        "size": format_bytes(stats.st_size),
        "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(stats.st_mtime)),
        "type": ftype,
    }


@app.route("/api/downloads")
def api_downloads():
    """List only files this app has actually downloaded (from the registry),
    not everything sitting in the folder — and without re-scanning the
    filesystem on every request."""
    root = get_download_dir()
    root_resolved = root.resolve()
    files = []
    stale = []
    with _registry_lock:
        paths = list(_downloaded_files)
    for p in paths:
        item = Path(p)
        try:
            # Only show entries that live under the currently-selected folder.
            item.resolve().relative_to(root_resolved)
        except (ValueError, OSError):
            continue
        if not item.is_file():
            stale.append(p)  # was moved/deleted outside the app
            continue
        if item.suffix in SKIP_EXTENSIONS:
            continue
        files.append(_file_entry(item, root))
    if stale:
        with _registry_lock:
            _downloaded_files.difference_update(stale)
            _save_registry()
    files.sort(key=lambda f: f["date"], reverse=True)
    return jsonify({"files": files, "folder": str(root)})


@app.route("/api/downloads/play/<path:relpath>")
def api_play_file(relpath):
    """Stream the file inline (not as a forced download) so the browser's
    native player opens it in a new tab — works for mp4/mp3/etc."""
    return send_from_directory(get_download_dir(), relpath, as_attachment=False)


@app.route("/api/downloads/reveal/<path:relpath>", methods=["POST"])
def api_reveal_file(relpath):
    """Open the OS file manager with the file selected/highlighted.

    Only works for a local, single-user setup where the server and the
    browser share the same desktop — same constraint as /api/browse-folder.
    """
    if IS_CLOUD:
        return jsonify({"error": "Not available on a cloud deploy — use the Play/Save link instead."}), 400
    target = (get_download_dir() / relpath).resolve()
    try:
        target.relative_to(get_download_dir().resolve())
    except ValueError:
        abort(400)
    if not target.exists():
        return jsonify({"error": "File no longer exists"}), 404

    try:
        system = platform.system()
        if system == "Windows":
            subprocess.run(["explorer", "/select,", str(target)])
        elif system == "Darwin":
            subprocess.run(["open", "-R", str(target)])
        else:
            # Most Linux file managers don't support "select this file",
            # so open the containing folder instead.
            subprocess.run(["xdg-open", str(target.parent)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Could not open file manager: {clean_error(e)}"}), 500


@app.route("/api/downloads/<path:relpath>")
def api_download_file(relpath):
    return send_from_directory(get_download_dir(), relpath, as_attachment=True)


@app.route("/api/cookies", methods=["POST"])
def api_upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    f.save(str(COOKIE_FILE))
    return jsonify({"ok": True})


@app.route("/api/update-ytdlp", methods=["POST"])
def api_update_ytdlp():
    try:
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        return jsonify({"error": clean_error(result.stderr[:300] or "Unknown error")}), 500
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 500
@app.route("/api/test-cookies", methods=["POST"])
def api_test_cookies():
    """Test if uploaded cookies are working"""
    if not COOKIE_FILE.exists():
        return jsonify({"ok": False, "message": "No cookies file uploaded yet"}), 400
    
    try:
        opts = ydl_common_opts()
        opts.update({
            "quiet": True,
            "extract_flat": True,
            "playlistend": 1,
        })
        
        # Test with a public video that sometimes requires cookies
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
            
        return jsonify({
            "ok": True,
            "message": "✅ Cookies are working!",
            "video": info.get("title", "Unknown video")
        })
    except Exception as e:
        error_msg = clean_error(e)
        if "sign in" in error_msg.lower() or "confirm" in error_msg.lower():
            return jsonify({
                "ok": False, 
                "message": "❌ Cookies expired or invalid. Please re-upload fresh cookies."
            }), 400
        return jsonify({
            "ok": False, 
            "message": f"❌ Error: {error_msg[:100]}"
        }), 400
@app.route("/api/cookie-status")
def api_cookie_status():
    """Check if cookies file exists and seems valid"""
    if not COOKIE_FILE.exists():
        return jsonify({"has_cookies": False, "valid": False, "message": "No cookies uploaded"})
    
    # Check if file has content
    try:
        with open(COOKIE_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                return jsonify({"has_cookies": True, "valid": False, "message": "Cookies file is empty"})
            if "# Netscape HTTP Cookie File" in content:
                return jsonify({"has_cookies": True, "valid": True, "message": "Cookies file present"})
            return jsonify({"has_cookies": True, "valid": False, "message": "Invalid cookie format"})
    except:
        return jsonify({"has_cookies": True, "valid": False, "message": "Could not read cookies file"})


_load_registry()

if __name__ == "__main__":
    # threaded=True matters here: downloads run in a background thread, but
    # /api/browse-folder blocks on a native dialog and /api/progress is
    # polled every 800ms — without threading those can queue up behind
    # each other on Flask's dev server and make the page feel unresponsive.
    #
    # Locally: debug=True on port 5000, same as before.
    # On Railway: $PORT is set by the platform, and debug is forced off
    # since debug mode exposes a remote code-execution console — never
    # safe on a publicly reachable server.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=not IS_CLOUD, host="0.0.0.0", port=port, threaded=True)
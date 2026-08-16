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
import queue
import shutil
import zipfile
import platform
import subprocess
import threading
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory, abort, Response, stream_with_context
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

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
    "2160p": None,
    "1440p": None,
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
    """True only for URLs that represent a real playlist the user wants
    downloaded in full.

    YouTube tacks a `list=RD…&start_radio=1` (or similar) onto a normal
    watch URL whenever a video is opened via autoplay/"up next"/Mix —
    that's an algorithmic queue attached to a single video, not a
    playlist anyone asked to save. A URL with both `v=` (a specific
    video) and a Mix-style list id (`RD…`) should be treated as a single
    video; only a genuine playlist id (e.g. `PL…`, `UU…`, `LL…`, `WL`)
    — or a URL with no `v=` at all, i.e. a bare playlist link — counts
    as a playlist here.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    list_id = query.get("list", "")
    if "v" in query and list_id.lower().startswith("rd"):
        return False
    if "list" in query:
        return True
    return "playlist" in url.lower()


def strip_mix_params(url: str) -> str:
    """For a single-video URL that also carries a Mix/Radio queue
    (list=RD…, start_radio=1, index=…), drop those params before handing
    the URL to yt-dlp — otherwise yt-dlp's youtube:tab extractor still
    tries to resolve the attached mix (and can fail auth-check on it)
    even though is_playlist_url() correctly decided this is just one
    video. Leaves real playlist URLs untouched."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if "v" in query and "list" in query:
        query.pop("list", None)
        query.pop("start_radio", None)
        query.pop("index", None)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url


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
        # "web" goes first now: with fresh cookies + the Deno/EJS JS-
        # challenge solver (added to the Dockerfile) in place, "web" no
        # longer needs to hit the earlier "Sign in to confirm you're not
        # a bot" wall, and it exposes the full DASH format list (separate
        # video-only/audio-only streams) that 1080p+ needs. "android"
        # mostly only offers pre-muxed/progressive formats, which are
        # capped at a lower resolution — that's why quality selection
        # was silently falling back to a low-res single-file format
        # (bv*+ba wasn't matching anything, so our selector fell through
        # to plain /b). "android" stays second, purely as a fallback in
        # case cookies expire again and "web" starts getting bot-checked.
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android"],
            },
            # Auto-generated "Mix"/Radio playlists (IDs starting with RD)
            # trip yt-dlp's extra auth-check, which tries to confirm the
            # loaded cookies belong to the same channel before trusting a
            # playlist extraction — and fails hard if that webpage probe
            # doesn't succeed, even for a totally public mix. We're not
            # downloading anyone's private content here, so skip that
            # check rather than have every Mix URL error out.
            "youtubetab": {
                "skip": ["authcheck"],
            },
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


QUALITY_HEIGHT_CAPS = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}


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
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            return size
        # YouTube's adaptive formats very often carry no size field at
        # all (this is common enough that it used to make every quality
        # collapse to the same fallback number below). When that
        # happens, estimate from bitrate * duration instead — the same
        # approach yt-dlp itself uses to compute filesize_approx, just
        # applied here for the formats yt-dlp didn't already do it for.
        tbr = f.get("tbr")
        duration = info.get("duration")
        if tbr and duration:
            return int(tbr * 1000 / 8 * duration)
        return 0

    formats = info.get("formats") or []

    if quality == "audio":
        audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                      and f.get("vcodec") in (None, "none")]
        if audio_only:
            best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
            return fsize(best_audio) or None
        return fsize(info) or None

    video_only = [f for f in formats if f.get("vcodec") not in (None, "none")]
    audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none")]

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
        total = fsize(info)
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
    if not playlist:
        url = strip_mix_params(url)
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
    if not playlist:
        url = strip_mix_params(url)
    job_id = new_job("playlist" if playlist else "video")

    thread = threading.Thread(target=_run_download, args=(job_id, url, quality, playlist), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


def build_direct_stream_format(quality: str) -> str:
    """Format selector for direct-to-browser streaming. Same idea as
    build_format_opts() above, but prefers an mp4 video + m4a audio pair
    specifically — ffmpeg then copies both streams straight into an mp4
    container without re-encoding (fast, low CPU). Falls back to
    whatever's available (e.g. vp9/opus) if no mp4 pair exists at the
    requested quality — ffmpeg still muxes it, it's just a bit slower."""
    if quality == "audio":
        return "bestaudio/best"
    cap = QUALITY_HEIGHT_CAPS.get(quality)
    height = f"[height<={cap}]" if cap else ""
    return f"bv*{height}[ext=mp4]+ba[ext=m4a]/bv*{height}+ba/b{height}"


def _ffmpeg_input_args(fmt: dict) -> list:
    """-headers/-i pair for one ffmpeg input, forwarding whatever headers
    yt-dlp resolved for that stream (User-Agent, and Cookie if cookies.txt
    was used) so YouTube's CDN accepts the request the same way it would
    from yt-dlp itself. Also asks ffmpeg to ride out a dropped connection
    instead of failing the whole stream partway through."""
    headers = fmt.get("http_headers") or {}
    args = []
    if headers:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        args += ["-headers", header_str]
    args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", fmt["url"]]
    return args


@app.route("/api/direct-download")
def api_direct_download():
    """Stream a single video straight to the browser — no file is ever
    written to the server's disk. yt-dlp resolves the direct CDN URL(s)
    for the requested quality; ffmpeg then reads straight from those
    URLs and muxes/transcodes on the fly, writing its output to stdout,
    which we pipe straight into the HTTP response as it's produced.

    GET on purpose, not POST: this needs to be a plain link/navigation
    so the browser treats the response as a normal file download
    (via Content-Disposition: attachment) instead of something the page
    has to fetch() and turn into a Blob itself.

    Only single videos are supported here. Playlists keep using the
    regular stage-then-serve /api/download flow — kicking off several
    simultaneous browser downloads reliably is a separate problem this
    route doesn't try to solve. There's also no progress bar for this
    path: the browser's own download UI takes over once streaming starts.
    """
    url = (request.args.get("url") or "").strip()
    quality = request.args.get("quality", "best")
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if quality not in QUALITY_FORMATS:
        return jsonify({"error": "Unknown quality option"}), 400
    if is_playlist_url(url):
        return jsonify({"error": "Direct streaming only supports single videos — use the regular download for playlists."}), 400
    if not check_ffmpeg():
        return jsonify({"error": "FFmpeg isn't available on this server — direct streaming needs it."}), 400
    url = strip_mix_params(url)

    opts = ydl_common_opts()
    opts["format"] = build_direct_stream_format(quality)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 400
    if not info:
        return jsonify({"error": "Failed to fetch video information"}), 400

    title = sanitize_filename(info.get("title") or "video") or "video"

    # requested_formats = separate video-only + audio-only streams that
    # need muxing together (the normal case for 720p+). Its absence means
    # yt-dlp resolved a single pre-muxed (or audio-only) format instead.
    requested = info.get("requested_formats")
    cmd = ["ffmpeg", "-loglevel", "error", "-y"]
    if requested:
        cmd += _ffmpeg_input_args(requested[0])
        cmd += _ffmpeg_input_args(requested[1])
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += _ffmpeg_input_args(info)

    if quality == "audio":
        filename = f"{title}.mp3"
        mimetype = "audio/mpeg"
        cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "pipe:1"]
    else:
        filename = f"{title}.mp4"
        mimetype = "video/mp4"
        cmd += ["-c", "copy", "-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4", "pipe:1"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as e:
        return jsonify({"error": f"Could not start ffmpeg: {clean_error(e)}"}), 500

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(1024 * 64)
                if not chunk:
                    break
                yield chunk
        finally:
            # Covers both a finished stream and a client that disconnected
            # mid-download — either way, don't leave ffmpeg running.
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    ascii_fallback = filename.encode("ascii", "ignore").decode() or ("video.mp3" if quality == "audio" else "video.mp4")
    disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        stream_with_context(generate()),
        mimetype=mimetype,
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


PLAYLIST_ZIP_LIMIT = 100  # sane cap so one request can't try to zip a 5,000-video channel dump


class _QueueWriterStream:
    """Write-only, forward-only file-like object for zipfile.ZipFile to
    write into. Instead of buffering, every write() pushes its bytes onto
    a bounded queue that the Flask response generator drains on the other
    end — the queue's maxsize is what gives this backpressure, so a
    multi-gigabyte playlist can't balloon the server's memory just
    because the client's connection is slow."""
    def __init__(self, q: "queue.Queue", chunk_size: int = 64 * 1024):
        self._q = q
        self._pos = 0
        self._chunk_size = chunk_size

    def write(self, data):
        if not data:
            return 0
        mv = memoryview(data)
        for i in range(0, len(mv), self._chunk_size):
            self._q.put(bytes(mv[i:i + self._chunk_size]))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass


def _build_playlist_zip(q: "queue.Queue", entry_urls: list, quality: str):
    """Runs in a background thread. Resolves and muxes each playlist
    video one at a time (same ffmpeg-pipe approach as the single-video
    route) and writes each straight into its own zip entry via zipfile's
    streaming write mode — nothing is fully buffered in memory or
    written to disk at any point. Videos that fail to resolve or
    download are skipped rather than aborting the whole zip, since the
    response has already started streaming by the time this runs and
    there's no way to report a partial failure back to the browser.
    """
    try:
        zf = zipfile.ZipFile(_QueueWriterStream(q), mode="w", allowZip64=True)
        used_names = set()
        for idx, entry_url in enumerate(entry_urls, start=1):
            try:
                opts = ydl_common_opts()
                opts["format"] = build_direct_stream_format(quality)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(entry_url, download=False)
            except Exception:
                continue
            if not info:
                continue

            title = sanitize_filename(info.get("title") or f"video {idx}") or f"video {idx}"
            ext = "mp3" if quality == "audio" else "mp4"
            name = f"{idx:02d} - {title}.{ext}"
            n = 1
            while name in used_names:
                n += 1
                name = f"{idx:02d} - {title} ({n}).{ext}"
            used_names.add(name)

            requested = info.get("requested_formats")
            cmd = ["ffmpeg", "-loglevel", "error", "-y"]
            if requested:
                cmd += _ffmpeg_input_args(requested[0])
                cmd += _ffmpeg_input_args(requested[1])
                cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            else:
                cmd += _ffmpeg_input_args(info)
            if quality == "audio":
                cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "pipe:1"]
            else:
                cmd += ["-c", "copy", "-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4", "pipe:1"]

            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except Exception:
                continue

            try:
                with zf.open(name, mode="w", force_zip64=True) as zentry:
                    while True:
                        chunk = proc.stdout.read(1024 * 64)
                        if not chunk:
                            break
                        zentry.write(chunk)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stdout.close()
        zf.close()
    except Exception:
        pass
    finally:
        q.put(None)  # sentinel: tells the response generator there's no more data


@app.route("/api/direct-download-playlist")
def api_direct_download_playlist():
    """Stream an entire playlist to the browser as a single .zip — the
    multi-file counterpart to /api/direct-download. Browsers block or
    prompt-flood a page that tries to trigger several automatic
    downloads at once, so instead of one file per video, this wraps the
    whole playlist in one zip archive that's built and streamed on the
    fly (see _build_playlist_zip / _QueueWriterStream above) — still no
    intermediate file ever touches the server's disk.

    Videos are processed one at a time, not concurrently, so this is
    slower than the regular playlist download for anything but a short
    playlist. There's also no progress bar once streaming starts — same
    trade-off as the single-video direct-download route.
    """
    url = (request.args.get("url") or "").strip()
    quality = request.args.get("quality", "best")
    if not url:
        return jsonify({"error": "Please enter a YouTube URL"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if quality not in QUALITY_FORMATS:
        return jsonify({"error": "Unknown quality option"}), 400
    if not is_playlist_url(url):
        return jsonify({"error": "That's not a playlist URL — use the regular direct-download for a single video."}), 400
    if not check_ffmpeg():
        return jsonify({"error": "FFmpeg isn't available on this server — direct streaming needs it."}), 400

    try:
        info_opts = ydl_common_opts()
        info_opts.update({"extract_flat": True, "playlistend": PLAYLIST_ZIP_LIMIT})
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 400

    entries_raw = (info or {}).get("entries") or []
    entry_urls = []
    for e in entries_raw:
        if not e:
            continue
        vid_url = e.get("url") or e.get("webpage_url") or e.get("id")
        if not vid_url:
            continue
        if not vid_url.startswith(("http://", "https://")):
            vid_url = f"https://www.youtube.com/watch?v={vid_url}"
        entry_urls.append(vid_url)

    if not entry_urls:
        return jsonify({"error": "Couldn't find any videos in that playlist"}), 400

    zip_title = sanitize_filename(info.get("title") or "playlist") or "playlist"

    q = queue.Queue(maxsize=64)
    worker = threading.Thread(target=_build_playlist_zip, args=(q, entry_urls, quality), daemon=True)
    worker.start()

    def generate():
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk

    ascii_fallback = zip_title.encode("ascii", "ignore").decode() or "playlist"
    disposition = f"attachment; filename=\"{ascii_fallback}.zip\"; filename*=UTF-8''{quote(zip_title)}.zip"
    return Response(
        stream_with_context(generate()),
        mimetype="application/zip",
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


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
#!/usr/bin/env python3
"""
VibeSave — Companion Server (Termux-friendly)

Save as server_termux.py on your Termux device alongside nebula-yt-downloader.html.

Termux-specific notes:
- Run `termux-setup-storage` once to grant storage access (allows ~/storage/shared).
- Recommended installs:
    pkg update && pkg upgrade
    pkg install python ffmpeg
    pip install flask flask-cors yt-dlp
  (yt-dlp may also be available via pip or pkg depending on your Termux repo.)
- Start: python3 server_termux.py
- Then open http://<your-phone-ip>:5000 from the device/browser on same LAN.

This is a near-1:1 port of the original server.py with:
 - default downloads to shared storage (~/storage/shared/VibeSaveDownloads)
 - robust local IP detection suitable for Android/Termux
 - folder listing showing shared storage and Termux home
"""

# ─── Bootstrap: ensure dependencies / ffmpeg (Termux-aware) ──────────────────
import sys
import subprocess
import importlib
import shutil
import os

def _run_cmd(cmd, silent=False):
    try:
        if silent:
            return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        else:
            return subprocess.call(cmd) == 0
    except FileNotFoundError:
        return False

def ensure_python_packages(pkg_map):
    for pip_name, import_name in pkg_map.items():
        try:
            importlib.import_module(import_name)
            print(f"OK: Python package '{pip_name}' is installed.")
        except Exception:
            print(f"Missing Python package '{pip_name}'. Attempting to install with pip...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
                importlib.invalidate_caches()
                importlib.import_module(import_name)
                print(f"Installed '{pip_name}' successfully.")
            except Exception as e:
                print(f"Failed to install '{pip_name}' via pip: {e}")
                print(f"Please run: {sys.executable} -m pip install {pip_name}")

def ensure_ffmpeg_termux():
    # If ffmpeg in PATH, OK. Otherwise try pkg install (Termux).
    if shutil.which("ffmpeg"):
        print("OK: ffmpeg found in PATH.")
        return True

    # If pkg available, attempt to install
    if shutil.which("pkg"):
        print("ffmpeg not found. Attempting: pkg install -y ffmpeg")
        ok = _run_cmd(["pkg", "update", "-y"])  # some termux pkg implementations ignore -y on update
        ok = _run_cmd(["pkg", "install", "-y", "ffmpeg"]) or ok
        if ok and shutil.which("ffmpeg"):
            print("ffmpeg installed via pkg.")
            return True
        else:
            print("pkg attempted but ffmpeg still missing or install failed.")
    else:
        print("Termux pkg not found. Cannot auto-install ffmpeg here.")

    print("Please run in Termux:")
    print("  pkg update && pkg upgrade")
    print("  pkg install ffmpeg")
    return False

# Ensure Python packages required by this server
_python_requirements = {
    "flask": "flask",
    "flask-cors": "flask_cors",
    "yt-dlp": "yt_dlp",
}
ensure_python_packages(_python_requirements)

# Try to ensure ffmpeg exists (Termux-specific attempt)
ensure_ffmpeg_termux()

# ─── End bootstrap; import rest of modules ───────────────────────────────────

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import threading
import uuid
import tempfile
import time
import glob
from urllib.parse import urlparse
import zipfile
import socket
import shutil

app = Flask(__name__)
CORS(app, expose_headers=["Content-Disposition", "Content-Length"])

# ─── Termux / storage detection ──────────────────────────────────────────────

def detect_shared_storage():
    """
    Detect common Termux/shared-storage paths:
      - ~/storage/shared (created after termux-setup-storage)
      - /sdcard
    Return the first existing writable path or None.
    """
    candidates = [
        os.path.expanduser("~/storage/shared"),
        "/sdcard",
    ]
    for p in candidates:
        try:
            if os.path.isdir(p) and os.access(p, os.W_OK):
                return p
        except Exception:
            pass
    return None

SHARED_ROOT = detect_shared_storage()

# Default folder: prefer shared storage so files are visible to the Android UI
if SHARED_ROOT:
    DEFAULT_FOLDER = os.path.join(SHARED_ROOT, "VibeSaveDownloads")
else:
    # fallback to Termux home
    DEFAULT_FOLDER = os.path.expanduser("~/VibeSaveDownloads")

current_folder = DEFAULT_FOLDER
os.makedirs(current_folder, exist_ok=True)

jobs = {}

# ─── FFmpeg detection ────────────────────────────────────────────────────────

def detect_ffmpeg():
    # If ffmpeg is in PATH, return None (yt-dlp will use it). If not, try to locate termux pkg path.
    if shutil.which("ffmpeg"):
        return None
    # Termux puts binaries in /data/data/com.termux/files/usr/bin usually; but if ffmpeg exists next to script, use it.
    base = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(base, "ffmpeg")) or os.path.exists(os.path.join(base, "ffmpeg.exe")):
        return base
    return None

FFMPEG_LOCATION = detect_ffmpeg()

# ─── Utility: build safer headers and minor opts ─────────────────────────────

def make_base_ydl_opts(url, fmt, mp3, want_images=False):
    """
    Build common yt-dlp options with more browser-like headers and chunking.
    If want_images is True, we enable writethumbnail and skip video download.
    """
    # derive referer from URL origin (scheme://netloc)
    referer = ""
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            referer = f"{p.scheme}://{p.netloc}"
    except Exception:
        referer = ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                      " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer

    # Core options
    opts = {
        "format": fmt or "bestvideo+bestaudio/best",
        # outtmpl provided by caller
        "http_headers": headers,
        "geo_bypass": True,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
        "format_sort": ["res", "fps", "vcodec:h264"],
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios", "web_safari", "web_embedded", "android_vr"],
            }
        },
    }

    # mp3 postprocessing (use ffmpeg)
    if mp3:
        # Use bestvideo+bestaudio/best so we get audio even when it's only muxed with video
        opts.update({
            "format": "bestvideo+bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "prefer_ffmpeg": True,
        })

    if want_images:
        # Attempt to download thumbnails/images and skip the main video
        opts.update({
            "writethumbnail": True,
            "skip_download": True,  # don't download the main video when wanting images
        })

    # optional ffmpeg location
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION

    return opts

# ─── Download worker (cookies.txt support + improved anti-bot headers) ──────

def run_download(job_id, url, fmt, mp3, folder):
    job = jobs[job_id]
    job["status"] = "downloading"
    job["percent"] = 0
    job["stage"] = "Extracting information..."
    os.makedirs(folder, exist_ok=True)

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    want_images = (fmt == "png")
    outtmpl = os.path.join(folder, "%(title)s.%(ext)s")
    ydl_opts = make_base_ydl_opts(url, None if want_images else fmt, mp3, want_images=want_images)
    ydl_opts.update({
        "outtmpl": outtmpl,
        "progress_hooks": [lambda d: _progress_hook(d, job, mp3)],
        "merge_output_format": "mp4" if not want_images else None,
        "quiet": False,
    })

    if os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
        print(f"Using cookies from: {cookies_file}")
    else:
        print("No cookies.txt found. For private/age-restricted YouTube, export cookies.txt and place next to server_termux.py")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            job["title"] = info.get("title", "") if info else job.get("title", "")

            if want_images:
                files = sorted(glob.glob(os.path.join(folder, "*")), key=os.path.getmtime)
                images = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))]
                if not images:
                    job["status"] = "error"
                    job["error"] = "No images produced. The source likely has no thumbnails/slideshow images."
                    job["stage"] = "Failed"
                    return
                if len(images) == 1:
                    job["filepath"] = images[0]
                    job["filename"] = os.path.basename(images[0])
                else:
                    zip_name = f"{job_id}_images.zip"
                    zip_path = os.path.join(folder, zip_name)
                    try:
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            for img in images:
                                zf.write(img, arcname=os.path.basename(img))
                        job["filepath"] = zip_path
                        job["filename"] = zip_name
                    except Exception as ze:
                        job["status"] = "error"
                        job["error"] = f"Failed to create images ZIP: {ze}"
                        job["stage"] = "Failed"
                        return
            else:
                try:
                    files = sorted(glob.glob(os.path.join(folder, "*")), key=os.path.getmtime)
                    filepath = files[-1] if files else None
                    if filepath:
                        job["filepath"] = filepath
                        job["filename"] = os.path.basename(filepath)
                    else:
                        job["filepath"] = None
                        job["filename"] = ""
                except Exception:
                    job["filepath"] = None
                    job["filename"] = ""
            job["status"] = "done"
            job["percent"] = 100
            job["stage"] = "Complete"
    except Exception as e:
        handle_download_exception(job, e, cookies_file)

def _progress_hook(d, job, mp3):
    if d.get("status") == "downloading":
        try:
            pct_str = d.get("_percent_str", "0%").replace("%", "").strip()
            job["percent"] = float(pct_str)
            job["speed"] = d.get("_speed_str", "")
            job["eta"] = d.get("_eta_str", "")
            job["stage"] = "Downloading media..."
        except Exception:
            pass
        try:
            job["title"] = d.get("info_dict", {}).get("title", "")
        except Exception:
            pass
    elif d.get("status") == "finished":
        job["percent"] = 100
        job["stage"] = "Converting to MP3..." if mp3 else "Merging formats..."

def handle_download_exception(job, exc, cookies_file):
    err = str(exc)
    lower = err.lower()
    job["status"] = "error"
    job["error"] = err
    job["stage"] = "Failed"
    if "403" in lower or "forbidden" in lower or "unable to download video data" in lower:
        if not os.path.exists(cookies_file):
            job["error"] = (
                "403 Forbidden while downloading. If this is YouTube or similar, it may require a logged-in browser session.\n"
                "Export cookies.txt using the 'Get cookies.txt LOCALLY' browser extension (while signed in),\n"
                "place cookies.txt next to server_termux.py, then retry."
            )
        else:
            job["error"] = (
                "403 Forbidden while downloading despite cookies.txt being present. Possible causes:\n"
                "- The remote server is blocking this machine's IP (rate limit / geo-block).\n"
                "- The extractor needs a different player_client or additional headers.\n"
                "Try: re-exporting cookies, retrying, or using the 'DOWNLOAD TO DEVICE' option from the web UI (uses direct streaming)."
            )

# ─── Helper: cleanup temp dir later ──────────────────────────────────────────

def cleanup_later(path, delay=60):
    def _cleanup():
        time.sleep(delay)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_cleanup, daemon=True).start()

# ─── Endpoint: download and return file directly to client ──────────────────

@app.route("/download_file", methods=["POST"])
def download_file():
    if request.is_json:
        data = request.get_json()
        url = data.get("url", "").strip()
        fmt = data.get("format", "bestvideo+bestaudio/best")
        mp3 = bool(data.get("mp3", False))
    else:
        url = request.form.get("url", "").strip()
        fmt = request.form.get("format", "bestvideo+bestaudio/best")
        mp3 = str(request.form.get("mp3", "")).lower() in ("1", "true", "yes", "on")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    tmpdir = tempfile.mkdtemp(prefix="vibesave_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    want_images = (fmt == "png")
    ydl_opts = make_base_ydl_opts(url, None if want_images else fmt, mp3, want_images=want_images)
    ydl_opts.update({
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "merge_output_format": "mp4" if not want_images else None,
    })

    if os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
        print(f"Using cookies from: {cookies_file}")
    else:
        print("No cookies.txt found. For private/age-restricted YouTube, export cookies.txt and place next to server_termux.py")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if want_images:
                files = sorted(glob.glob(os.path.join(tmpdir, "*")), key=os.path.getmtime)
                images = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))]
                if not images:
                    shutil.rmtree(tmpdir)
                    return jsonify({"error": "No images produced from this URL"}), 404
                if len(images) == 1:
                    filepath = images[0]
                    filename = os.path.basename(filepath)
                    cleanup_later(tmpdir, delay=60)
                    try:
                        return send_file(filepath, as_attachment=True, download_name=filename)
                    except TypeError:
                        return send_file(filepath, as_attachment=True, attachment_filename=filename)
                else:
                    zip_path = os.path.join(tmpdir, "images.zip")
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for img in images:
                            zf.write(img, arcname=os.path.basename(img))
                    cleanup_later(tmpdir, delay=60)
                    try:
                        return send_file(zip_path, as_attachment=True, download_name="images.zip")
                    except TypeError:
                        return send_file(zip_path, as_attachment=True, attachment_filename="images.zip")
            else:
                files = sorted(glob.glob(os.path.join(tmpdir, "*")), key=os.path.getmtime)
                if not files:
                    raise RuntimeError("No output file produced by yt-dlp")
                filepath = files[-1]
                filename = os.path.basename(filepath)
                cleanup_later(tmpdir, delay=60)
                try:
                    return send_file(filepath, as_attachment=True, download_name=filename)
                except TypeError:
                    return send_file(filepath, as_attachment=True, attachment_filename=filename)
    except Exception as e:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        err = str(e)
        lower = err.lower()
        user_msg = err
        if "403" in lower or "forbidden" in lower or "unable to download video data" in lower:
            if not os.path.exists(cookies_file):
                user_msg = (
                    "403 Forbidden while downloading. Try exporting cookies.txt from your browser while signed-in\n"
                    "using 'Get cookies.txt LOCALLY' and place cookies.txt next to server_termux.py then try again."
                )
            else:
                user_msg = (
                    "403 Forbidden even though cookies.txt is present. Possible causes: IP block or server-side restrictions.\n"
                    "Try re-exporting cookies, testing the 'DOWNLOAD TO DEVICE' option, or try from a different network."
                )
        return jsonify({"error": user_msg}), 500

# ─── New endpoint: download the resulting file for a completed job ─────────

@app.route("/download_job/<job_id>", methods=["GET"])
def download_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job not ready"}), 400
    filepath = job.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not available"}), 404
    try:
        filename = os.path.basename(filepath)
        try:
            return send_file(filepath, as_attachment=True, download_name=filename)
        except TypeError:
            return send_file(filepath, as_attachment=True, attachment_filename=filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Routes ──────────────────────────────────────────────────────────────────

def get_local_ip():
    """
    Robust local IP detection: create a UDP socket to a public IP and read local socket name.
    Works well on Android/Termux.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip.startswith("127.") or ip == "0.0.0.0":
                ip = "127.0.0.1"
        except Exception:
            ip = "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass
    return ip

@app.route("/", methods=["GET"])
def index():
    local_ip = get_local_ip()
    server_origin = f"http://{local_ip}:5000"
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nebula-yt-downloader.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace('value="" autocomplete', f'value="{server_origin}" autocomplete')
    return html

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})

@app.route("/folders", methods=["GET"])
def get_folders():
    # Provide Shared Storage and Termux Home as options
    folders = []
    # Shared storage location (if available)
    if SHARED_ROOT:
        shared_path = os.path.join(SHARED_ROOT, "VibeSaveDownloads")
        folders.append({"id": "shared", "label": "Shared Storage (sdcard)", "path": shared_path, "icon": "shared"})
    # Termux home
    home_path = os.path.expanduser("~/VibeSaveDownloads")
    folders.append({"id": "home", "label": "Termux Home / VibeSaveDownloads", "path": home_path, "icon": "local"})

    for f in folders:
        f["active"] = (os.path.abspath(f["path"]) == os.path.abspath(current_folder))

    return jsonify({
        "folders": folders,
        "current": current_folder,
        "shared_available": SHARED_ROOT is not None,
    })

@app.route("/set-folder", methods=["POST"])
def set_folder():
    global current_folder
    data = request.get_json()
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    os.makedirs(path, exist_ok=True)
    current_folder = path
    return jsonify({"ok": True, "current": current_folder})

@app.route("/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = data.get("url", "").strip()
    fmt = data.get("format", "bestvideo+bestaudio/best")  # may be 'png' sentinel
    mp3 = data.get("mp3", False)

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued",
        "percent": 0,
        "speed": "",
        "eta": "",
        "title": "",
        "error": "",
        "folder": current_folder,
        "stage": "Queued",
        "filepath": None,
        "filename": "",
    }

    t = threading.Thread(target=run_download, args=(job_id, url, fmt, mp3, current_folder), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "folder": current_folder})

@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

# ─── Endpoint: fetch video info (thumbnail, title, author, duration) ──────────

@app.route("/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios"],
            }
        },
    }
    if os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({"error": "Could not extract info"}), 404

            thumbnail = info.get("thumbnail", "")
            thumbnails = info.get("thumbnails", [])
            if thumbnails:
                best = sorted(thumbnails, key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0), reverse=True)
                if best and best[0].get("url"):
                    thumbnail = best[0]["url"]

            return jsonify({
                "title": info.get("title", "Unknown title"),
                "author": info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown",
                "thumbnail": thumbnail,
                "duration": info.get("duration"),
                "platform": info.get("extractor_key", ""),
                "view_count": info.get("view_count"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Endpoint: trim a video/audio and return to client ───────────────────────

@app.route("/trim", methods=["POST"])
def trim_media():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    fmt = (data or {}).get("format", "bestvideo+bestaudio/best")
    mp3 = bool((data or {}).get("mp3", False))
    start_sec = float((data or {}).get("start_sec", 0))
    end_sec = float((data or {}).get("end_sec", -1))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    tmpdir = tempfile.mkdtemp(prefix="vibesave_trim_")

    try:
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        ydl_opts = make_base_ydl_opts(url, fmt, mp3)
        ydl_opts.update({
            "outtmpl": outtmpl,
            "quiet": True,
            "noprogress": True,
            "merge_output_format": "mp4" if not mp3 else None,
        })
        if os.path.exists(cookies_file):
            ydl_opts["cookiefile"] = cookies_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files = sorted(glob.glob(os.path.join(tmpdir, "*")), key=os.path.getmtime)
        if not files:
            raise RuntimeError("No file produced by yt-dlp")
        src = files[-1]
        src_name = os.path.basename(src)
        name_no_ext, src_ext = os.path.splitext(src_name)

        out_ext = ".mp3" if mp3 else ".mp4"
        trimmed_name = f"{name_no_ext}_trim{out_ext}"
        trimmed_path = os.path.join(tmpdir, trimmed_name)

        ffmpeg_bin = "ffmpeg"
        if FFMPEG_LOCATION:
            ffmpeg_bin = os.path.join(FFMPEG_LOCATION, "ffmpeg")

        duration_arg = end_sec - start_sec if end_sec > start_sec else None

        if mp3:
            cmd = [ffmpeg_bin, "-y",
                   "-i", src,
                   "-ss", str(start_sec)]
            if duration_arg:
                cmd += ["-t", str(duration_arg)]
            cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2",
                    trimmed_path]
        else:
            # Re-encode with H.264+AAC, -ss after -i for frame-accurate cuts.
            # -avoid_negative_ts make_zero fixes TikTok/fragmented MP4 timestamp issues.
            cmd = [ffmpeg_bin, "-y",
                   "-i", src,
                   "-ss", str(start_sec)]
            if duration_arg:
                cmd += ["-t", str(duration_arg)]
            cmd += [
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "22",
                "-c:a", "aac",
                "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                trimmed_path,
            ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            # Fallback to stream copy if libx264 unavailable on this device
            fallback_cmd = [ffmpeg_bin, "-y",
                            "-i", src,
                            "-ss", str(start_sec)]
            if duration_arg:
                fallback_cmd += ["-t", str(duration_arg)]
            if mp3:
                fallback_cmd += ["-vn", "-acodec", "copy"]
            else:
                fallback_cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            fallback_cmd.append(trimmed_path)
            result2 = subprocess.run(fallback_cmd, capture_output=True)
            if result2.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg trim failed.\n"
                    f"Re-encode error: {result.stderr.decode('utf-8', errors='replace')[:300]}\n"
                    f"Stream-copy error: {result2.stderr.decode('utf-8', errors='replace')[:300]}"
                )

        cleanup_later(tmpdir, delay=120)
        try:
            return send_file(trimmed_path, as_attachment=True, download_name=trimmed_name)
        except TypeError:
            return send_file(trimmed_path, as_attachment=True, attachment_filename=trimmed_name)
    except Exception as e:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    local_ip = get_local_ip()
    shared_status = f"Shared storage: {SHARED_ROOT}" if SHARED_ROOT else "Shared storage: not detected (run termux-setup-storage)"
    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    cookie_status = "cookies.txt FOUND ✓" if os.path.exists(cookies_file) else "cookies.txt not found — Some sites may require login"
    print(f"""
  🌟 VibeSave Server (Termux)
  ─────────────────────────────────────
  URL          → http://{local_ip}:5000
  Saving to    → {current_folder}
  Cookies      → {cookie_status}
  {shared_status}
  ─────────────────────────────────────
  Tips:
   - Run `termux-setup-storage` once to access shared storage.
   - Install ffmpeg (pkg) and python packages before running:
       pkg update && pkg upgrade
       pkg install python ffmpeg
       pip install flask flask-cors yt-dlp
  Press Ctrl+C to stop.
""")
    app.run(host="0.0.0.0", port=5000, debug=False)
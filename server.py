#!/usr/bin/env python3
"""
VibeSave - Companion Server (extended: platform detection + PNG/image support)

Install: pip install flask flask-cors yt-dlp
         Also install Node.js or Deno for YouTube extraction (optional but recommended)
Run:     python server.py
Then open http://<your-pc-ip>:5000

Notes:
- PNG/images option downloads thumbnails (or extracted images) via yt-dlp's "writethumbnail" behavior.
  For platforms like TikTok, some posts are slideshows; if there are no images produced, the server will return an error.
- Multiple images are packaged into a ZIP for convenient download to device.
- New: Accepts an optional 'platform' parameter in /download and /download_file:
    - platform='ios' => merge_output_format will be 'mov'
    - platform='android' (default) => 'mp4'
- Node.js or Deno is used as JavaScript runtime for YouTube extraction to avoid deprecation warnings
"""

# ─── Bootstrap: ensure dependencies / ffmpeg ───────────────────────────────────
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
    """
    pkg_map: dict of pip_name -> import_name
    """
    for pip_name, import_name in pkg_map.items():
        try:
            importlib.import_module(import_name)
            print(f"OK: Python package '{pip_name}' is installed.")
        except Exception:
            print(f"Missing Python package '{pip_name}'. Attempting to install with pip...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
                # verify
                importlib.invalidate_caches()
                importlib.import_module(import_name)
                print(f"Installed '{pip_name}' successfully.")
            except Exception as e:
                print(f"Failed to install '{pip_name}' via pip: {e}")
                print(f"Please run: {sys.executable} -m pip install {pip_name}")
                # don't exit; let the main app attempt to run (it will error later if missing)

def ensure_ffmpeg():
    # If ffmpeg in PATH, OK. Otherwise, try common package managers (apt, brew) if available.
    if shutil.which("ffmpeg"):
        print("OK: ffmpeg found in PATH.")
        return True

    print("ffmpeg not found in PATH. Attempting to install via system package manager (non-Termux).")
    # Try apt (Debian/Ubuntu)
    if shutil.which("apt"):
        print("Attempting: sudo apt update && sudo apt install -y ffmpeg")
        ok = _run_cmd(["sudo", "apt", "update"]) and _run_cmd(["sudo", "apt", "install", "-y", "ffmpeg"])
        if ok and shutil.which("ffmpeg"):
            print("ffmpeg installed via apt.")
            return True
    # Try brew (macOS/Homebrew)
    if shutil.which("brew"):
        print("Attempting: brew install ffmpeg")
        ok = _run_cmd(["brew", "install", "ffmpeg"])
        if ok and shutil.which("ffmpeg"):
            print("ffmpeg installed via brew.")
            return True

    print("Automatic ffmpeg installation failed or requires manual intervention.")
    print("Please install ffmpeg manually. Examples:")
    print("  Debian/Ubuntu: sudo apt update && sudo apt install ffmpeg")
    print("  macOS (Homebrew): brew install ffmpeg")
    print("  Windows: download from https://ffmpeg.org/ or use your package manager")
    return False

# Ensure Python packages required by this server
_python_requirements = {
    "flask": "flask",
    "flask-cors": "flask_cors",
    "yt-dlp": "yt_dlp",
}
ensure_python_packages(_python_requirements)

# Try to ensure ffmpeg exists (best-effort; may require sudo)
ensure_ffmpeg()

# ─── End bootstrap; import rest of modules ────────────────────────────────────

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
import sqlite3
import json
from datetime import datetime

app = Flask(__name__)
# Expose Content-Disposition and Content-Length so the client can read filename and size
CORS(app, expose_headers=["Content-Disposition", "Content-Length"])

# ─── Folder detection ───────────────────────────────────────────────────────

def detect_onedrive():
    candidates = [
        os.path.expanduser("~/OneDrive"),
        os.path.expanduser("~/OneDrive - Personal"),
    ]
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        candidates += [
            os.path.join(userprofile, "OneDrive"),
            os.path.join(userprofile, "OneDrive - Personal"),
        ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None


ONEDRIVE_ROOT = detect_onedrive()
DEFAULT_FOLDER = os.path.expanduser("~/Videos/VibeSaveDownloads")

current_folder = DEFAULT_FOLDER
os.makedirs(current_folder, exist_ok=True)

jobs = {}

# ─── Database for download history ───────────────────────────────────────────

def init_database():
    """Initialize SQLite database for download history"""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vibesave.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE,
            url TEXT NOT NULL,
            title TEXT,
            author TEXT,
            thumbnail TEXT,
            duration INTEGER,
            platform TEXT,
            format TEXT,
            mp3 BOOLEAN,
            file_path TEXT,
            file_name TEXT,
            file_size INTEGER,
            status TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    return db_path

DB_PATH = init_database()

def add_download_record(job_id, url, title="", author="", thumbnail="", duration=None, 
                       platform="", format="", mp3=False, file_path="", file_name="", 
                       file_size=None, status="queued", error_message=""):
    """Add or update a download record in the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO download_history 
        (job_id, url, title, author, thumbnail, duration, platform, format, mp3, 
         file_path, file_name, file_size, status, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
            (SELECT created_at FROM download_history WHERE job_id = ?), CURRENT_TIMESTAMP
        ))
    ''', (job_id, url, title, author, thumbnail, duration, platform, format, mp3,
          file_path, file_name, file_size, status, error_message, job_id))
    
    conn.commit()
    conn.close()

def update_download_status(job_id, status, file_path=None, file_name=None, 
                          file_size=None, error_message=None):
    """Update download status and completion info"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    updates = ["status = ?"]
    params = [status]
    
    if file_path:
        updates.append("file_path = ?")
        params.append(file_path)
    if file_name:
        updates.append("file_name = ?")
        params.append(file_name)
    if file_size is not None:
        updates.append("file_size = ?")
        params.append(file_size)
    if error_message:
        updates.append("error_message = ?")
        params.append(error_message)
    
    if status == "done":
        updates.append("completed_at = CURRENT_TIMESTAMP")
    
    params.append(job_id)
    
    cursor.execute(f'''
        UPDATE download_history 
        SET {', '.join(updates)}
        WHERE job_id = ?
    ''', params)
    
    conn.commit()
    conn.close()

def get_download_history(limit=50, offset=0, search=None, status_filter=None):
    """Get download history with optional search and filtering"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    query = '''
        SELECT job_id, url, title, author, thumbnail, duration, platform, 
               format, mp3, file_name, file_size, status, error_message, 
               created_at, completed_at
        FROM download_history
    '''
    params = []
    
    if search:
        query += " WHERE (title LIKE ? OR author LIKE ? OR url LIKE ?)"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])
    
    if status_filter:
        if search:
            query += " AND status = ?"
        else:
            query += " WHERE status = ?"
        params.append(status_filter)
    
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor.execute(query, params)
    columns = [description[0] for description in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    
    conn.close()
    return results

def get_download_stats():
    """Get download statistics"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM download_history")
    total_downloads = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM download_history WHERE status = 'done'")
    successful_downloads = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM download_history WHERE status = 'error'")
    failed_downloads = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT platform, COUNT(*) as count 
        FROM download_history 
        WHERE platform != '' 
        GROUP BY platform 
        ORDER BY count DESC 
        LIMIT 5
    ''')
    top_platforms = cursor.fetchall()
    
    conn.close()
    
    return {
        "total": total_downloads,
        "successful": successful_downloads,
        "failed": failed_downloads,
        "success_rate": (successful_downloads / total_downloads * 100) if total_downloads > 0 else 0,
        "top_platforms": [{"platform": p, "count": c} for p, c in top_platforms]
    }

# ─── FFmpeg ───────────────────────────────────────────────────────────────────

def detect_ffmpeg():
    if shutil.which("ffmpeg"):
        return None
    base = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(base, "ffmpeg.exe")):
        return base
    return None

FFMPEG_LOCATION = detect_ffmpeg()

def detect_nodejs():
    """Check if Node.js is available on the system"""
    return _run_cmd(["node", "--version"], silent=True)

def detect_deno():
    """Check if Deno is available on the system"""
    return _run_cmd(["deno", "--version"], silent=True)

def get_deno_path():
    """Get full path to Deno executable, checking all known install locations."""
    # shutil.which checks PATH first — works on all platforms
    path = shutil.which("deno")
    if path:
        return path

    # Common Windows install locations (Deno installer puts it here by default)
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\deno\deno.exe"),
            r"C:\Program Files\deno\deno.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

    # Common Unix/macOS install locations
    else:
        candidates = [
            os.path.expanduser("~/.deno/bin/deno"),
            "/usr/local/bin/deno",
            "/opt/homebrew/bin/deno",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

    # Last resort: ask the shell
    try:
        cmd = ["where", "deno"] if os.name == "nt" else ["which", "deno"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            line = result.stdout.strip().split("\n")[0].strip()
            if line and os.path.isfile(line):
                return line
    except Exception:
        pass

    return None

# Detect JS runtimes once at startup and cache results
_DENO_PATH = None
_HAS_DENO = False
_HAS_NODE = False

def _init_js_runtimes():
    global _DENO_PATH, _HAS_DENO, _HAS_NODE
    _DENO_PATH = get_deno_path()
    _HAS_DENO = _DENO_PATH is not None
    _HAS_NODE = detect_nodejs()
    if _HAS_DENO:
        print(f"OK: Deno found at: {_DENO_PATH}")
    elif _HAS_NODE:
        print("OK: Node.js found — will use for YouTube JS challenges.")
    else:
        print("WARNING: No JS runtime (Deno/Node.js) found.")
        print("         YouTube downloads may be missing formats or fail.")
        print("         Install Deno: irm https://deno.land/install.ps1 | iex")

_init_js_runtimes()

def is_oauth2_cookies(cookies_file):
    """Returns True if cookies.txt is a yt-dlp oauth2 token file, not browser cookies."""
    try:
        with open(cookies_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(512)
            return "yt-dlp" in content and "oauth" in content.lower()
    except Exception:
        return False

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

    # Use a robust format string that always has fallbacks
    # Prefer best quality: bestvideo*+bestaudio/bestvideo+bestaudio/best/bestvideo/bestaudio
    # This prioritizes highest resolution available (up to 4K/8K if available)
    safe_fmt = fmt or "bestvideo[height<=2160]+bestaudio/bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best/bestvideo/bestaudio"

    # Build player_client list based on available JS runtime.
    # With Deno/Node: use "web" first — it gets the best formats and Deno solves the n-challenge.
    # Without any JS runtime: skip "web" and use clients that don't require JS challenge solving.
    if _HAS_DENO or _HAS_NODE:
        player_clients = ["web", "web_creator", "mweb", "web_music", "web_embedded"]
    else:
        player_clients = ["web_creator", "mweb", "web_music", "web_embedded"]

    # Core options
    opts = {
        "format": safe_fmt,
        # outtmpl provided by caller
        "http_headers": headers,
        "geo_bypass": True,
        "http_chunk_size": 1048576,
        "retries": 20,
        "fragment_retries": 20,
        # Sort by resolution (prefer higher), then fps, then prefer h264 for compatibility
        "format_sort": ["res:1080", "res", "fps", "vcodec:h264", "acodec:aac"],
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            }
        },
    }

    # Configure JS runtime for YouTube n-challenge solving.
    # Pass as extractor_args to the jsc sub-extractor.
    if _HAS_DENO and _DENO_PATH:
        deno_arg = _DENO_PATH.replace("\\", "/")
        opts["extractor_args"]["jsc"] = {
            "js_runtimes": [f"deno:{deno_arg}"],
            "remote_components": ["ejs:github"],
        }
        # Also pass as top-level params which some yt-dlp versions prefer
        opts["extractor_args"]["youtube:jsc"] = {
            "js_runtimes": [f"deno:{deno_arg}"],
            "remote_components": ["ejs:github"],
        }
        # Add top-level remote_components for newer yt-dlp versions
        opts["remote_components"] = ["ejs:github"]
    elif _HAS_NODE:
        node_path = shutil.which("node") or "node"
        opts["extractor_args"]["jsc"] = {
            "js_runtimes": [f"node:{node_path}"],
            "remote_components": ["ejs:github"],
        }
        opts["extractor_args"]["youtube:jsc"] = {
            "js_runtimes": [f"node:{node_path}"],
            "remote_components": ["ejs:github"],
        }
        # Add top-level remote_components for newer yt-dlp versions
        opts["remote_components"] = ["ejs:github"]

    # mp3 postprocessing (use ffmpeg)
    if mp3:
        # Use bestaudio for best quality audio extraction
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",  # Highest MP3 quality
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

def run_download(job_id, url, fmt, mp3, folder, platform='android'):
    """
    Background worker for /download (server-side save). Supports:
      - fmt: yt-dlp format string OR the special value 'png' (images/thumbnails)
      - mp3: boolean (if True performs mp3 postprocessing)
      - platform: 'android' (default) or 'ios' — affects merge_output_format (mp4 vs mov)
    """
    job = jobs[job_id]
    job["status"] = "downloading"
    job["percent"] = 0
    job["stage"] = "Extracting information..."
    os.makedirs(folder, exist_ok=True)

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    want_images = (fmt == "png")
    # choose outtmpl: put files into the folder
    outtmpl = os.path.join(folder, "%(title)s.%(ext)s")
    # For images we still set an outtmpl so thumbnails are saved next to other files.
    ydl_opts = make_base_ydl_opts(url, None if want_images else fmt, mp3, want_images=want_images)
    # Decide merge output based on platform
    merge_fmt = "mov" if platform == "ios" else "mp4"
    ydl_opts.update({
        "outtmpl": outtmpl,
        "progress_hooks": [lambda d: _progress_hook(d, job, mp3)],
        "merge_output_format": None if want_images else merge_fmt,
        "quiet": False,
    })

    # Add cookiefile if present — detect whether it's browser cookies or oauth2 token
    if os.path.exists(cookies_file):
        if is_oauth2_cookies(cookies_file):
            # oauth2 token file: use username/password auth instead of cookiefile
            ydl_opts["username"] = "oauth2"
            ydl_opts["password"] = ""
            print(f"Using OAuth2 token from: {cookies_file}")
        else:
            ydl_opts["cookiefile"] = cookies_file
            print(f"Using cookies from: {cookies_file}")
    else:
        print("No cookies.txt found. For private/age-restricted YouTube, export cookies.txt and place next to server.py")

    # Extract initial info for database record
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "skip_download": True,
            "format": "bestvideo[height<=2160]+bestaudio/bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best/any",
            "format_sort": ["res:1080", "res", "fps", "vcodec:h264", "acodec:aac"],
            "extractor_args": {
                "youtube": {
                    "player_client": (["web"] + ["web_creator", "mweb", "web_music", "web_embedded"]) if (_HAS_DENO or _HAS_NODE) else ["web_creator", "mweb", "web_music", "web_embedded"],
                },
                **( {"jsc": {"js_runtimes": [f"deno:{_DENO_PATH.replace(chr(92), '/')}"], "remote_components": ["ejs:github"]}} if _HAS_DENO and _DENO_PATH
                    else {"jsc": {"js_runtimes": ["node:" + (shutil.which("node") or "node")], "remote_components": ["ejs:github"]}} if _HAS_NODE
                    else {} ),
            },
            **({"remote_components": ["ejs:github"]} if (_HAS_DENO or _HAS_NODE) else {}),
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "")
            author = info.get("uploader") or info.get("channel") or ""
            thumbnail = info.get("thumbnail", "")
            duration = info.get("duration")
            platform_name = info.get("extractor_key", "")
            
            # Add initial record to database
            add_download_record(
                job_id, url, title, author, thumbnail, duration,
                platform_name, fmt, mp3, status="downloading"
            )
            
            # Update job with extracted info
            job["title"] = title
    except Exception as e:
        print(f"Failed to extract initial info: {e}")
        # Add record with minimal info
        add_download_record(job_id, url, format=fmt, mp3=mp3, status="downloading")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not job.get("title"):
                job["title"] = info.get("title", "") if info else job.get("title", "")
    except Exception as e:
        err_str = str(e)
        print(f"Primary format failed: {err_str}")
        images_only = "only images are available" in err_str.lower()
        format_error = (not images_only and (
                        "Requested format is not available" in err_str or
                        "requested format" in err_str.lower() or
                        "n challenge" in err_str.lower() or
                        "TikTok" in err_str))
        if images_only:
            job["status"] = "error"
            job["error"] = ("This URL only contains images/photos, not a video. "
                            "Use the PNG/Images format option to download them instead.")
            job["stage"] = "Failed"
            return
        if format_error:
            fallback_formats = ["bestvideo+bestaudio/best", "bestvideo[height<=1080]+bestaudio/best", "best", "bestvideo/best"]
            success = False
            for fallback_fmt in fallback_formats:
                print(f"Trying fallback format: {fallback_fmt}")
                ydl_opts_fallback = ydl_opts.copy()
                ydl_opts_fallback["format"] = fallback_fmt
                ydl_opts_fallback["extractor_args"] = {
                    "youtube": {
                        "player_client": (["web"] + ["web_creator", "mweb", "web_music", "web_embedded"])
                                          if (_HAS_DENO or _HAS_NODE)
                                          else ["web_creator", "mweb", "web_music", "web_embedded"],
                    },
                    **( {"jsc": {"js_runtimes": [f"deno:{_DENO_PATH.replace(chr(92), '/')}"], "remote_components": ["ejs:github"]}} if _HAS_DENO and _DENO_PATH
                        else {"jsc": {"js_runtimes": ["node:" + (shutil.which("node") or "node")], "remote_components": ["ejs:github"]}} if _HAS_NODE
                        else {} ),
                }
                ydl_opts_fallback["format_sort"] = ["res:1080", "res", "fps", "vcodec:h264", "acodec:aac"]
                if _HAS_DENO or _HAS_NODE:
                    ydl_opts_fallback["remote_components"] = ["ejs:github"]
                try:
                    with yt_dlp.YoutubeDL(ydl_opts_fallback) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if not job.get("title"):
                            job["title"] = info.get("title", "") if info else job.get("title", "")
                        print(f"Fallback format successful: {fallback_fmt}")
                        success = True
                        break
                except Exception as fallback_error:
                    print(f"Fallback format failed: {fallback_error}")
                    continue
            if not success:
                raise e
        else:
            raise e

    # Determine produced file(s)
    if want_images:
        # Look for image files produced by yt-dlp in this folder
        files = sorted(glob.glob(os.path.join(folder, "*")), key=os.path.getmtime)
        images = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))]
        if not images:
            job["status"] = "error"
            job["error"] = "No images produced. The source likely has no thumbnails/slideshow images."
            job["stage"] = "Failed"
            update_download_status(job_id, "error", error_message=job["error"])
            return
        if len(images) == 1:
            job["filepath"] = images[0]
            job["filename"] = os.path.basename(images[0])
            file_size = os.path.getsize(images[0])
            update_download_status(job_id, "done", images[0], job["filename"], file_size)
        else:
            # Create a ZIP containing the images
            zip_name = f"{job_id}_images.zip"
            zip_path = os.path.join(folder, zip_name)
            try:
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for img in images:
                        zf.write(img, arcname=os.path.basename(img))
                job["filepath"] = zip_path
                job["filename"] = zip_name
                file_size = os.path.getsize(zip_path)
                update_download_status(job_id, "done", zip_path, zip_name, file_size)
                # (optional) cleanup original images later if desired
            except Exception as ze:
                job["status"] = "error"
                job["error"] = f"Failed to create images ZIP: {ze}"
                job["stage"] = "Failed"
                update_download_status(job_id, "error", error_message=job["error"])
                return
    else:
        # Video/audio path: try to find newest file produced by this run
        try:
            files = sorted(glob.glob(os.path.join(folder, "*")), key=os.path.getmtime)
            filepath = files[-1] if files else None
            if filepath:
                job["filepath"] = filepath
                job["filename"] = os.path.basename(filepath)
                file_size = os.path.getsize(filepath)
                update_download_status(job_id, "done", filepath, job["filename"], file_size)
            else:
                job["filepath"] = None
                job["filename"] = ""
                update_download_status(job_id, "error", error_message="No output file produced")
        except Exception:
            job["filepath"] = None
            job["filename"] = ""
            update_download_status(job_id, "error", error_message="Failed to locate output file")
    
    job["status"] = "done"
    job["percent"] = 100
    job["stage"] = "Complete"


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
    # Helpful guidance for 403s
    if "403" in lower or "forbidden" in lower or "unable to download video data" in lower:
        if not os.path.exists(cookies_file):
            job["error"] = (
                "403 Forbidden while downloading. If this is YouTube or similar, it may require a logged-in browser session.\n"
                "Export cookies.txt using the 'Get cookies.txt LOCALLY' browser extension (while signed in),\n"
                "place cookies.txt next to server.py, then retry."
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
    """
    Downloads media into a temporary directory, returns the resulting file as an attachment.
    Accepts JSON body or form-encoded POST:
      - url (required)
      - format (optional)  (if 'png' => image/thumbnail behavior)
      - mp3 (optional)
      - platform (optional) 'ios' or 'android' (default android) -> affects merge_output_format
    """
    if request.is_json:
        data = request.get_json()
        url = data.get("url", "").strip()
        fmt = data.get("format", "bestvideo+bestaudio/best")
        mp3 = bool(data.get("mp3", False))
        platform = data.get("platform", "android")
    else:
        url = request.form.get("url", "").strip()
        fmt = request.form.get("format", "bestvideo+bestaudio/best")
        mp3 = str(request.form.get("mp3", "")).lower() in ("1", "true", "yes", "on")
        platform = request.form.get("platform", "android")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    tmpdir = tempfile.mkdtemp(prefix="vibesave_")
    outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

    want_images = (fmt == "png")
    ydl_opts = make_base_ydl_opts(url, None if want_images else fmt, mp3, want_images=want_images)
    # Decide merge output based on platform
    merge_fmt = "mov" if platform == "ios" else "mp4"
    ydl_opts.update({
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "merge_output_format": None if want_images else merge_fmt,
    })

    if os.path.exists(cookies_file):
        if is_oauth2_cookies(cookies_file):
            ydl_opts["username"] = "oauth2"
            ydl_opts["password"] = ""
            print(f"Using OAuth2 token from: {cookies_file}")
        else:
            ydl_opts["cookiefile"] = cookies_file
            print(f"Using cookies from: {cookies_file}")
    else:
        print("No cookies.txt found. For private/age-restricted YouTube, export cookies.txt and place next to server.py")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # If images requested, collect image files and possibly zip them
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
        # cleanup immediately on error
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
                    "using 'Get cookies.txt LOCALLY' and place cookies.txt next to server.py then try again."
                )
            else:
                user_msg = (
                    "403 Forbidden even though cookies.txt is present. Possible causes: IP block or server-side restrictions.\n"
                    "Try re-exporting cookies, testing the 'DOWNLOAD TO DEVICE' option from the web UI (uses direct streaming), or try from a different network."
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

# ─── New endpoint: delete job and cleanup file after device download ────────

@app.route("/delete_job/<job_id>", methods=["POST", "DELETE"])
def delete_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    # Delete the file if it exists
    filepath = job.get("filepath")
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
            print(f"Deleted file: {filepath}")
        except Exception as e:
            print(f"Failed to delete file {filepath}: {e}")
    
    # Remove job from memory
    del jobs[job_id]
    print(f"Deleted job: {job_id}")
    
    return jsonify({"status": "deleted"})

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
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
    folders = [
        {"id": "downloads", "label": "VibeSave Downloads", "path": DEFAULT_FOLDER, "icon": "downloads"},
    ]
    if ONEDRIVE_ROOT:
        nebula_od = os.path.join(ONEDRIVE_ROOT, "VibeSave Downloads")
        folders.extend([
            {"id": "onedrive_nebula", "label": "OneDrive / VibeSave Downloads", "path": nebula_od, "icon": "onedrive"},
            {"id": "onedrive_root", "label": "OneDrive (root)", "path": ONEDRIVE_ROOT, "icon": "onedrive"},
        ])

    for f in folders:
        f["active"] = (f["path"] == current_folder)

    return jsonify({
        "folders": folders,
        "current": current_folder,
        "onedrive_available": ONEDRIVE_ROOT is not None,
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
    
    # Check if this is a batch download
    if "urls" in data:
        return start_batch_download(data)
    
    # Single download (existing logic)
    url = data.get("url", "").strip()
    fmt = data.get("format", "bestvideo+bestaudio/best")  # may be 'png' sentinel
    mp3 = data.get("mp3", False)
    platform = data.get("platform", "android")

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

    t = threading.Thread(target=run_download, args=(job_id, url, fmt, mp3, current_folder, platform), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "folder": current_folder})

def start_batch_download(data):
    """Handle batch download of multiple URLs"""
    urls = data.get("urls", [])
    fmt = data.get("format", "bestvideo+bestaudio/best")
    mp3 = data.get("mp3", False)
    platform = data.get("platform", "android")
    
    if not urls or not isinstance(urls, list):
        return jsonify({"error": "No URLs provided or invalid format"}), 400
    
    # Validate URLs
    valid_urls = []
    for url in urls:
        url = url.strip()
        if url and (url.startswith('http://') or url.startswith('https://')):
            valid_urls.append(url)
    
    if not valid_urls:
        return jsonify({"error": "No valid URLs provided"}), 400
    
    batch_id = str(uuid.uuid4())[:8]
    job_ids = []
    
    # Create jobs for each URL
    for url in valid_urls:
        job_id = str(uuid.uuid4())[:8]
        job_ids.append(job_id)
        
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
            "batch_id": batch_id,
            "batch_url": url,
        }
        
        # Start download with a small delay to avoid overwhelming the server
        t = threading.Thread(target=run_download, args=(job_id, url, fmt, mp3, current_folder, platform), daemon=True)
        t.start()
        time.sleep(0.1)  # Small delay between downloads
    
    return jsonify({
        "batch_id": batch_id,
        "job_ids": job_ids,
        "total_urls": len(valid_urls),
        "folder": current_folder
    })

@app.route("/batch_status/<batch_id>", methods=["GET"])
def get_batch_status(batch_id):
    """Get status of all jobs in a batch"""
    batch_jobs = {job_id: job for job_id, job in jobs.items() if job.get("batch_id") == batch_id}
    
    if not batch_jobs:
        return jsonify({"error": "Batch not found"}), 404
    
    # Calculate overall progress
    total_jobs = len(batch_jobs)
    completed_jobs = sum(1 for job in batch_jobs.values() if job["status"] in ["done", "error"])
    successful_jobs = sum(1 for job in batch_jobs.values() if job["status"] == "done")
    failed_jobs = sum(1 for job in batch_jobs.values() if job["status"] == "error")
    
    overall_progress = (completed_jobs / total_jobs * 100) if total_jobs > 0 else 0
    
    return jsonify({
        "batch_id": batch_id,
        "total_jobs": total_jobs,
        "completed_jobs": completed_jobs,
        "successful_jobs": successful_jobs,
        "failed_jobs": failed_jobs,
        "overall_progress": overall_progress,
        "jobs": batch_jobs
    })

@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

# ─── Download History Endpoints ───────────────────────────────────────────────

@app.route("/history", methods=["GET"])
def get_history():
    """Get download history with pagination and filtering"""
    limit = min(int(request.args.get("limit", 50)), 100)  # Cap at 100
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    
    history = get_download_history(limit, offset, search if search else None, 
                                 status_filter if status_filter else None)
    
    return jsonify({
        "history": history,
        "limit": limit,
        "offset": offset,
        "has_more": len(history) == limit
    })

@app.route("/stats", methods=["GET"])
def get_stats():
    """Get download statistics"""
    return jsonify(get_download_stats())

@app.route("/redownload/<job_id>", methods=["POST"])
def redownload(job_id):
    """Re-download a previously completed item"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT url, format, mp3 FROM download_history 
        WHERE job_id = ? AND status = 'done'
    ''', (job_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        return jsonify({"error": "Download not found or not completed"}), 404
    
    url, fmt, mp3 = result
    
    # Start new download with same parameters
    new_job_id = str(uuid.uuid4())[:8]
    jobs[new_job_id] = {
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

    t = threading.Thread(target=run_download, args=(new_job_id, url, fmt, mp3, current_folder, 'android'), daemon=True)
    t.start()

    return jsonify({"job_id": new_job_id, "folder": current_folder})

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
        # Use "any" so info extraction works even for image-only posts
        "format": "bestvideo+bestaudio/best/bestvideo/bestaudio/any",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": (["web"] + ["web_creator", "mweb", "web_music", "web_embedded"])
                                  if (_HAS_DENO or _HAS_NODE)
                                  else ["web_creator", "mweb", "web_music", "web_embedded"],

            }
        },
    }
    if os.path.exists(cookies_file):
        if is_oauth2_cookies(cookies_file):
            ydl_opts["username"] = "oauth2"
            ydl_opts["password"] = ""
        else:
            ydl_opts["cookiefile"] = cookies_file
    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION
    if _HAS_DENO and _DENO_PATH:
        deno_path_escaped = _DENO_PATH.replace("\\", "/")
        ydl_opts["extractor_args"]["jsc"] = {
            "js_runtimes": [f"deno:{deno_path_escaped}"],
            "remote_components": ["ejs:github"],
        }
        ydl_opts["remote_components"] = ["ejs:github"]
    elif _HAS_NODE:
        node_path = shutil.which("node") or "node"
        ydl_opts["extractor_args"]["jsc"] = {
            "js_runtimes": [f"node:{node_path}"],
            "remote_components": ["ejs:github"],
        }
        ydl_opts["remote_components"] = ["ejs:github"]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({"error": "Could not extract info"}), 404

            # Pick best thumbnail
            thumbnail = info.get("thumbnail", "")
            thumbnails = info.get("thumbnails", [])
            if thumbnails:
                # prefer highest resolution
                best = sorted(thumbnails, key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0), reverse=True)
                if best and best[0].get("url"):
                    thumbnail = best[0]["url"]

            return jsonify({
                "title": info.get("title", "Unknown title"),
                "author": info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown",
                "thumbnail": thumbnail,
                "duration": info.get("duration"),  # seconds (int or None)
                "platform": info.get("extractor_key", ""),
                "view_count": info.get("view_count"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Endpoint: trim a video/audio and return to client ───────────────────────

@app.route("/trim", methods=["POST"])
def trim_media():
    """
    Downloads media, trims it with ffmpeg between start_sec and end_sec,
    and streams the result back to the client.
    Body (JSON):
      - url: media URL
      - format: yt-dlp format string
      - mp3: bool
      - platform: 'android' | 'ios'
      - start_sec: float (trim start, seconds)
      - end_sec: float (trim end, seconds)
    """
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    fmt = (data or {}).get("format", "bestvideo+bestaudio/best")
    mp3 = bool((data or {}).get("mp3", False))
    platform = (data or {}).get("platform", "android")
    start_sec = float((data or {}).get("start_sec", 0))
    end_sec = float((data or {}).get("end_sec", -1))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    tmpdir = tempfile.mkdtemp(prefix="vibesave_trim_")

    try:
        # 1. Download full file first
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        merge_fmt = "mov" if platform == "ios" else "mp4"
        ydl_opts = make_base_ydl_opts(url, fmt, mp3)
        ydl_opts.update({
            "outtmpl": outtmpl,
            "quiet": True,
            "noprogress": True,
            "merge_output_format": merge_fmt if not mp3 else None,
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

        # Decide output extension
        if mp3:
            out_ext = ".mp3"
        elif platform == "ios":
            out_ext = ".mov"
        else:
            out_ext = ".mp4"

        trimmed_name = f"{name_no_ext}_trim{out_ext}"
        trimmed_path = os.path.join(tmpdir, trimmed_name)

        # 2. Trim with ffmpeg — accurate re-encode approach
        # We intentionally do NOT use -c copy because:
        #   - Stream copy only cuts at keyframes → wrong cut points on TikTok/short-form
        #   - TikTok uses fragmented/non-standard MP4 timestamps that break stream copy
        #   - -ss before -i is fast but inaccurate; -ss after -i is frame-accurate
        ffmpeg_bin = "ffmpeg"
        if FFMPEG_LOCATION:
            ffmpeg_bin = os.path.join(FFMPEG_LOCATION, "ffmpeg")

        duration_arg = end_sec - start_sec if end_sec > start_sec else None

        if mp3:
            # Audio-only trim: re-encode to mp3
            cmd = [ffmpeg_bin, "-y",
                   "-i", src,
                   "-ss", str(start_sec)]
            if duration_arg:
                cmd += ["-t", str(duration_arg)]
            cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2",
                    trimmed_path]
        else:
            # Video trim: re-encode with H.264 + AAC for maximum compatibility
            # -ss placed AFTER -i for frame-accurate seeking
            # -avoid_negative_ts make_zero fixes TikTok timestamp issues
            # -movflags +faststart makes the file streamable immediately
            cmd = [ffmpeg_bin, "-y",
                   "-i", src,
                   "-ss", str(start_sec)]
            if duration_arg:
                cmd += ["-t", str(duration_arg)]
            cmd += [
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # ensure even dimensions for h264
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
            # Fallback: try stream copy in case libx264 is unavailable
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
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    od = f"OneDrive: {ONEDRIVE_ROOT}" if ONEDRIVE_ROOT else "OneDrive: not found"
    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.exists(cookies_file):
        if is_oauth2_cookies(cookies_file):
            cookie_status = "cookies.txt FOUND (OAuth2 token)"
        else:
            cookie_status = "cookies.txt FOUND (browser cookies)"
    else:
        cookie_status = "cookies.txt not found — YouTube may require login"
    print(f"""
  * VibeSave Server
  ==================
  URL          -> http://{local_ip}:5000
  Saving to    -> {current_folder}
  Cookies      -> {cookie_status}
  {od}
  ==================
  Press Ctrl+C to stop.
""")
    # Use PORT env var for Render.com, default to 5000 for local
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
"""
Savefiy backend.

Install:  pip install -r requirements.txt
Run:      uvicorn app:app --host 0.0.0.0 --port 8000

Step 1 (preview):  POST /download { "url": "...", "mode": "info" }
                   -> { "title": "...", "thumbnail": "...", "duration": 123 }
Step 2 (download): POST /download { "url": "...", "mode": "download", "quality": "360" | "720" | "1080" | "audio" }
                   -> { "download_url": "https://<server>/file/<token>", "title": "..." }
The server downloads the video (merging streams with ffmpeg when needed), keeps it for
a few minutes and serves it from GET /file/<token> as a real file download.
"""
import glob
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---- Settings you may want to change ---------------------------------------
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://savefiy-api.onrender.com")
ALLOWED_ORIGINS = ["*"]          # set to your website's address when everything works
MAX_SECONDS = 600                # longest video allowed (10 minutes)
MAX_BYTES = 200 * 1024 * 1024    # largest file allowed (200 MB)
FILE_TTL = 15 * 60               # files are deleted after 15 minutes
MAX_PARALLEL = 2                 # downloads running at the same time
ALLOWED_DOMAINS = (
    "instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com",
    "youtube.com", "youtu.be", "pinterest.com", "pin.it", "tiktok.com",
)
# -----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savefiy")

DL_DIR = os.path.join(tempfile.gettempdir(), "savefiy")
os.makedirs(DL_DIR, exist_ok=True)
TITLES = {}
SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)


def setup_ffmpeg():
    """Use the ffmpeg binary bundled in the imageio-ffmpeg package (no system install needed)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bindir = os.path.join(tempfile.gettempdir(), "ffbin")
        os.makedirs(bindir, exist_ok=True)
        link = os.path.join(bindir, "ffmpeg")
        if not os.path.exists(link):
            os.symlink(exe, link)
        return bindir
    except Exception as e:
        log.warning("ffmpeg not available: %s", e)
        return None


FFMPEG_DIR = setup_ffmpeg()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


class DownloadRequest(BaseModel):
    url: str
    quality: str = "720"
    mode: str = "download"  # "info" or "download"


def is_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def purge():
    now = time.time()
    for path in glob.glob(os.path.join(DL_DIR, "*")):
        try:
            if now - os.path.getmtime(path) > FILE_TTL:
                os.remove(path)
        except OSError:
            pass


def find_file(token: str):
    files = [p for p in glob.glob(os.path.join(DL_DIR, token + ".*"))
             if not p.endswith((".part", ".ytdl", ".temp"))]
    return files[0] if files else None


def download_opts(token: str, quality: str) -> dict:
    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(DL_DIR, token + ".%(ext)s"),
        "max_filesize": MAX_BYTES,
        "socket_timeout": 20,
        "retries": 2,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR

    if quality == "audio":
        if FFMPEG_DIR:
            opts["format"] = "ba/b"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        else:
            opts["format"] = "ba[ext=m4a]/ba/b"
    else:
        h = int(quality) if quality.isdigit() else 720
        if FFMPEG_DIR:
            opts["format"] = f"bv*[height<=?{h}]+ba/b[height<=?{h}]/b"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = f"b[height<=?{h}][protocol^=http]/b[height<=?{h}]/b"
    return opts


@app.post("/download")
def download(req: DownloadRequest):
    if not is_allowed(req.url):
        raise HTTPException(status_code=400, detail="unsupported platform")
    purge()

    info_opts = {"quiet": True, "noplaylist": True, "skip_download": True,
                 "ignore_no_formats_error": True}
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        log.exception("yt-dlp info failed for %s", req.url)  # visible in Render > Logs
        raise HTTPException(status_code=422, detail=str(e)[:300])

    if req.mode == "info":
        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        }

    duration = info.get("duration") or 0
    if duration > MAX_SECONDS:
        raise HTTPException(status_code=413,
                            detail=f"Video is longer than {MAX_SECONDS // 60} minutes.")

    if not SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Server is busy. Please try again in a minute.")
    token = uuid.uuid4().hex
    try:
        with yt_dlp.YoutubeDL(download_opts(token, req.quality)) as ydl:
            ydl.download([req.url])
    except Exception as e:
        log.exception("yt-dlp download failed for %s", req.url)
        raise HTTPException(status_code=422, detail=str(e)[:300])
    finally:
        SLOTS.release()

    path = find_file(token)
    if not path:
        raise HTTPException(
            status_code=422,
            detail=f"Could not create the file (it may be larger than {MAX_BYTES // (1024 * 1024)} MB).",
        )
    TITLES[token] = re.sub(r"[^\w\- ]+", "", info.get("title") or "").strip()[:60] or "video"
    return {"download_url": f"{PUBLIC_URL}/file/{token}", "title": info.get("title")}


@app.get("/file/{token}")
def get_file(token: str):
    purge()
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(status_code=404, detail="not found")
    path = find_file(token)
    if not path:
        raise HTTPException(status_code=404, detail="File expired. Please create it again.")
    ext = os.path.splitext(path)[1]
    return FileResponse(
        path,
        filename=TITLES.get(token, "video") + ext,
        media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
    )


@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": bool(FFMPEG_DIR)}

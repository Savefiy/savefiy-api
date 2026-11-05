"""
Savefiy backend (minimal).

Setup:
    pip install fastapi uvicorn yt-dlp
Run locally:
    uvicorn app:app --host 0.0.0.0 --port 8000
Then in savefiy.html set:
    const CONFIG = { API_URL: "https://YOUR-SERVER/download", ... }

Step 1 (preview):  POST /download { "url": "...", "mode": "info" }
                   -> { "title": "...", "thumbnail": "...", "duration": 123 }
Step 2 (download): POST /download { "url": "...", "mode": "download", "quality": "360" | "720" | "1080" | "audio" }
                   -> { "download_url": "...", "title": "..." }
"""
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Change this to the domain where savefiy.html is hosted ("*" only while testing).
ALLOWED_ORIGINS = ["*"]

# Only these sites are accepted (also protects the server from being used on random URLs).
ALLOWED_DOMAINS = (
    "instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com",
    "youtube.com", "youtu.be", "pinterest.com", "pin.it", "tiktok.com",
)

# Single-file formats, so no ffmpeg is needed. "audio" returns M4A (true MP3 needs ffmpeg).
FORMATS = {
    "360": "best[height<=360][ext=mp4]/best[height<=360]",
    "720": "best[height<=720][ext=mp4]/best[height<=720]",
    # Note: on YouTube, real 1080p is split into separate video+audio streams and needs ffmpeg
    # to merge. Without ffmpeg this falls back to the best single-file stream (often 720p).
    "1080": "best[height<=1080][ext=mp4]/best[height<=1080]",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

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


@app.post("/download")
def download(req: DownloadRequest):
    if not is_allowed(req.url):
        raise HTTPException(status_code=400, detail="unsupported platform")

    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "format": FORMATS.get(req.quality, FORMATS["720"]),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception:
        raise HTTPException(status_code=422, detail="could not process link")

    if req.mode == "info":
        return {"title": info.get("title"), "thumbnail": info.get("thumbnail"), "duration": info.get("duration")}

    direct_url = info.get("url")
    if not direct_url:
        raise HTTPException(status_code=422, detail="no downloadable format")
    return {"download_url": direct_url, "title": info.get("title")}


@app.get("/health")
def health():
    return {"ok": True}

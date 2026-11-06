"""
Savefiy backend (minimal).

Setup:    pip install fastapi uvicorn yt-dlp
Run:      uvicorn app:app --host 0.0.0.0 --port 8000

Step 1 (preview):  POST /download { "url": "...", "mode": "info" }
                   -> { "title": "...", "thumbnail": "...", "duration": 123 }
Step 2 (download): POST /download { "url": "...", "mode": "download", "quality": "360" | "720" | "1080" | "audio" }
                   -> { "download_url": "...", "title": "..." }
"""
import logging
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Change this to the domain where the website is hosted ("*" only while testing).
ALLOWED_ORIGINS = ["*"]

ALLOWED_DOMAINS = (
    "instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com",
    "youtube.com", "youtu.be", "pinterest.com", "pin.it", "tiktok.com",
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savefiy")

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


def direct_formats(info):
    """Formats a browser can download directly (plain http/https, not HLS/DASH streams)."""
    return [
        f for f in (info.get("formats") or [])
        if f.get("url") and f.get("protocol") in ("http", "https")
    ]


def pick_file(info, quality):
    """Choose one direct file URL for the requested quality (no ffmpeg needed)."""
    fmts = direct_formats(info)

    if quality == "audio":
        audio = [f for f in fmts if f.get("vcodec") == "none"]
        audio.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        if audio:
            return audio[0]

    # Files that already contain both video and audio.
    both = [f for f in fmts if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    if both:
        try:
            limit = int(quality)
        except ValueError:
            limit = 720
        height = lambda f: f.get("height") or 0
        within = [f for f in both if height(f) <= limit]
        if within:  # best quality that is not above the requested one
            return max(within, key=lambda f: (height(f), f.get("ext") == "mp4"))
        return min(both, key=height)  # everything is higher: take the lowest

    # Last resort: a single direct URL that yt-dlp already selected.
    if info.get("url") and info.get("protocol") in ("http", "https"):
        return info
    return None


@app.post("/download")
def download(req: DownloadRequest):
    if not is_allowed(req.url):
        raise HTTPException(status_code=400, detail="unsupported platform")

    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        log.exception("yt-dlp failed for %s", req.url)  # visible in Render > Logs
        raise HTTPException(status_code=422, detail=str(e)[:300])

    if req.mode == "info":
        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        }

    chosen = pick_file(info, req.quality)
    if not chosen:
        log.warning("no direct file for %s", req.url)
        raise HTTPException(
            status_code=422,
            detail="No direct file link for this video (it is a streaming-only video).",
        )
    return {"download_url": chosen["url"], "title": info.get("title")}


@app.get("/health")
def health():
    return {"ok": True}

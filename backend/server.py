#!/usr/bin/env python3
"""MP4 Studio public HTTP service. No private videos, cookies, or DRM bypass."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

try:
    import yt_dlp
except ImportError:
    raise SystemExit("Install dependencies: pip install -r backend/requirements.txt")

BASE = Path(__file__).resolve().parent
FILES = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/mp4-studio"))
FILES.mkdir(parents=True, exist_ok=True)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "10000"))
ALLOWED_ORIGINS = {s.strip().rstrip("/") for s in os.getenv("ALLOWED_ORIGINS", "").split(",") if s.strip()}
MAX_DURATION = int(os.environ.get("MAX_DURATION_SECONDS", "1200"))
MAX_BYTES = int(os.environ.get("MAX_VIDEO_MB", "180")) * 1024 * 1024
TTL_SECONDS = 25 * 60
RATE_WINDOW = 10 * 60
RATE_COUNT = 3
MAX_QUEUE = 3
QUALITIES = {"best": None, "1080": 1080, "720": 720, "480": 480, "360": 360}
JOB_PATTERN = re.compile(r"[A-Za-z0-9_-]{32}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LOCK = threading.RLock()
DOWNLOAD_SLOT = threading.Semaphore(1)
JOBS: dict[str, dict] = {}
RATES = defaultdict(deque)


def canonical_video_url(raw: object) -> str | None:
    """Allow only valid YouTube video URLs, then normalize to trusted canonical host."""
    if not isinstance(raw, str) or not 8 < len(raw) <= 2048:
        return None
    try:
        url = urlsplit(raw.strip())
        host = (url.hostname or "").lower()
        if url.scheme not in {"https", "http"} or url.username or url.password or url.port not in (None, 80, 443):
            return None
        if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}:
            return None
        if host in {"youtu.be", "www.youtu.be"}:
            match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", url.path)
            video_id = match.group(1) if match else None
        elif url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [None])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", url.path)
            video_id = match.group(1) if match else None
        return f"https://www.youtube.com/watch?v={video_id}" if video_id and VIDEO_ID.fullmatch(video_id) else None
    except (TypeError, ValueError):
        return None


def public_job(job: dict) -> dict:
    private = {"url", "path", "ip", "created_at"}
    return {key: value for key, value in job.items() if key not in private}


def update(job_id: str, **changes) -> None:
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)


def cleanup() -> None:
    now = time.time()
    with LOCK:
        expired = [key for key, j in JOBS.items() if j["status"] in ("ready", "error") and now - j["created_at"] > TTL_SECONDS]
        for key in expired:
            JOBS.pop(key, None)
            shutil.rmtree(FILES / key, ignore_errors=True)
        for ip in list(RATES):
            q = RATES[ip]
            while q and q[0] <= now - RATE_WINDOW:
                q.popleft()
            if not q:
                RATES.pop(ip, None)


def cleanup_loop() -> None:
    while True:
        time.sleep(120)
        cleanup()


def format_choice(quality: str) -> str:
    limit = f"[height<={QUALITIES[quality]}]" if QUALITIES[quality] else ""
    return (f"bestvideo[vcodec^=avc1]{limit}+bestaudio[ext=m4a]/"
            f"bestvideo[ext=mp4]{limit}+bestaudio[ext=m4a]/"
            f"best[ext=mp4]{limit}")


def check_info(info: dict, *, incomplete: bool = False) -> str | None:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        return "Livestreams and upcoming videos are not supported."
    seconds = info.get("duration")
    if isinstance(seconds, (int, float)) and seconds > MAX_DURATION:
        return f"Video exceeds the {MAX_DURATION // 60}-minute limit for this public server."
    size = info.get("filesize") or info.get("filesize_approx")
    if isinstance(size, (int, float)) and size > MAX_BYTES:
        return f"Video exceeds the {MAX_BYTES // (1024 * 1024)} MB limit for this public server."
    return None


def run_job(job_id: str) -> None:
    dest = FILES / job_id
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with DOWNLOAD_SLOT:
            with LOCK:
                job = dict(JOBS.get(job_id) or {})
            if not job:
                return
            update(job_id, status="working", stage="Checking video…")
            def progress(meta: dict) -> None:
                if meta.get("status") == "downloading":
                    downloaded = meta.get("downloaded_bytes") or 0
                    if downloaded > MAX_BYTES:
                        raise RuntimeError("Video exceeded the public server file-size limit.")
                    total = meta.get("total_bytes") or meta.get("total_bytes_estimate") or 0
                    percent = min(98, int(downloaded * 100 / total)) if total else None
                    update(job_id, stage="Downloading video…", progress=percent,
                           downloaded_bytes=downloaded, total_bytes=total,
                           speed=meta.get("speed"), eta=meta.get("eta"))
                elif meta.get("status") == "finished":
                    update(job_id, stage="Preparing MP4…", progress=99, eta=None)
            def postprocess(meta: dict) -> None:
                if meta.get("status") == "started":
                    update(job_id, stage="Merging video and audio…", progress=99, eta=None)
            opts = {
                "format": format_choice(job["quality"]),
                "outtmpl": str(dest / "%(title).95B [%(id)s].%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "retries": 2,
                "fragment_retries": 2,
                "socket_timeout": 25,
                "progress_hooks": [progress],
                "postprocessor_hooks": [postprocess],
                "merge_output_format": "mp4",
                "restrictfilenames": True,
                "windowsfilenames": True,
                "max_filesize": MAX_BYTES,
                "js_runtimes": {"deno": {}},
                "match_filter": lambda info, *, incomplete=False: check_info(info, incomplete=incomplete),
            }
            with yt_dlp.YoutubeDL(opts) as downloader:
                result = downloader.extract_info(job["url"], download=True)
                if not result:
                    raise RuntimeError("The video could not be downloaded.")
            videos = sorted(dest.glob("*.mp4"), key=lambda f: f.stat().st_size, reverse=True)
            if not videos:
                raise RuntimeError("No compatible MP4 was produced for this video.")
            selected = videos[0]
            size = selected.stat().st_size
            if not 0 < size <= MAX_BYTES:
                raise RuntimeError("Output file is empty or exceeds the public server limit.")
            # Delete unneeded fragments; retain only downloadable MP4.
            for file in dest.iterdir():
                if file.is_file() and file != selected:
                    file.unlink(missing_ok=True)
            update(job_id, status="ready", stage="MP4 ready", progress=100,
                   title=result.get("title") or "YouTube video", path=str(selected),
                   filename=selected.name, size=size, eta=None, speed=None)
    except Exception as exc:
        message = str(exc).replace("ERROR: ", "").strip()
        update(job_id, status="error", stage="Conversion failed", error=message[:380] or "Conversion failed.")
        shutil.rmtree(dest, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "MP4StudioCloud/2.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def cors(self) -> None:
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def base_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.cors()

    def json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.base_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        if self.headers.get("Origin", "").rstrip("/") not in ALLOWED_ORIGINS:
            return self.json(403, {"error": "Origin is not approved for this backend."})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.base_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/api/health"):
            return self.json(200, {"ok": True, "app": "MP4 Studio API", "version": "2.0"})
        if path == "/api/info":
            return self.json(200, {"ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                   "js_runtime": "deno" if shutil.which("deno") else None,
                                   "max_video_mb": MAX_BYTES // (1024 * 1024),
                                   "max_minutes": MAX_DURATION // 60, "version": "2.0"})
        parts = path.strip("/").split("/")
        if len(parts) not in (3, 4) or parts[:2] != ["api", "jobs"] or not JOB_PATTERN.fullmatch(parts[2]):
            return self.json(404, {"error": "Not found."})
        key = parts[2]
        with LOCK:
            rec = JOBS.get(key)
            job = dict(rec) if rec else None
        if not job:
            return self.json(404, {"error": "Job expired or the server restarted."})
        if len(parts) == 3:
            return self.json(200, public_job(job))
        if parts[3] != "download":
            return self.json(404, {"error": "Not found."})
        if job["status"] != "ready":
            return self.json(409, {"error": "Video is not ready."})
        file = Path(job["path"])
        if file.parent != FILES / key or not file.is_file():
            return self.json(404, {"error": "Video expired."})
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(file.name)}")
        self.send_header("Content-Length", str(file.stat().st_size))
        self.base_headers()
        self.end_headers()
        try:
            with file.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 128 * 1024)
        except (ConnectionError, BrokenPipeError):
            pass

    def user_ip(self) -> str:
        # Render forwards the real remote address in the last X-Forwarded-For hop.
        # This is a best-effort quota, not a substitute for a real edge rate limiter.
        forwarded = self.headers.get("X-Forwarded-For", "")
        return (forwarded.split(",")[-1].strip() if forwarded else self.client_address[0])[:128]

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/jobs":
            return self.json(404, {"error": "Not found."})
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
            return self.json(403, {"error": "This website has not been approved in ALLOWED_ORIGINS."})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.json(415, {"error": "JSON is required."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                return self.json(413, {"error": "Request body too large."})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                return self.json(400, {"error": "Invalid request."})
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.json(400, {"error": "Invalid JSON."})
        url = canonical_video_url(payload.get("url"))
        quality = payload.get("quality", "best")
        if not url:
            return self.json(400, {"error": "Enter a valid YouTube video or Shorts URL."})
        if quality not in QUALITIES:
            return self.json(400, {"error": "Unsupported quality."})
        ip = self.user_ip()
        cleanup()
        with LOCK:
            requests = RATES[ip]
            if len(requests) >= RATE_COUNT:
                return self.json(429, {"error": "Limit reached: 3 videos per 10 minutes. Try again later."})
            active = sum(job["status"] in {"queued", "working"} for job in JOBS.values())
            if active >= MAX_QUEUE:
                return self.json(429, {"error": "The server is busy. Try again shortly."})
            requests.append(time.time())
            job_id = secrets.token_urlsafe(24)
            JOBS[job_id] = {"id": job_id, "url": url, "ip": ip, "quality": quality,
                            "created_at": time.time(), "status": "queued", "stage": "In queue…",
                            "progress": 0, "speed": None, "eta": None, "downloaded_bytes": 0,
                            "total_bytes": 0, "error": None, "title": None,
                            "filename": None, "size": None, "path": None}
            output = public_job(JOBS[job_id])
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        self.json(202, output)


def main() -> None:
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"MP4 Studio backend on {HOST}:{PORT}; CORS origins: {sorted(ALLOWED_ORIGINS)}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

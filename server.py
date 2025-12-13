import os
import re
import json
import signal
import shutil
import string
import random
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ================= ENV / TIMEZONE =================
os.environ["TZ"] = "Asia/Ho_Chi_Minh"
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

YT_COOKIES = os.getenv("YT_COOKIES")
YTDLP_BIN = os.getenv("YTDLP_BIN", "yt-dlp")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

def yt_common_args():
    args = [
        "--geo-bypass",
        "--geo-bypass-country", "VN",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "--extractor-retries", "3",
        "--retry-sleep", "2",
        "--no-check-certificate",
    ]
    if YT_COOKIES and os.path.exists(YT_COOKIES):
        args += ["--cookies", YT_COOKIES]
    return args

# ================= PATH =================
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
TMP_DIR = DATA_DIR / "tmp"
FILES_DIR = DATA_DIR / "files"
TMP_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")

# ================= APP =================
app = FastAPI(title="ytmp3-backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= TASK =================
class Task:
    def __init__(self, url: str):
        self.url = url
        self.title = ""
        self.duration = 0
        self.output_path = None
        self.status = "processing"
        self.error = None
        self.proc = None

_tasks = {}
_lock = threading.Lock()

SAFE_CHARS = f"-_.() {string.ascii_letters}{string.digits}"

def safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    cleaned = "".join(c for c in name if c in SAFE_CHARS).strip()
    return re.sub(r"\s+", " ", cleaned)[:120] or f"audio_{random.randint(1000,9999)}"

def parse_upload_date(upload_date: str | None):
    if not upload_date:
        return None
    try:
        dt = datetime.strptime(upload_date, "%Y%m%d")
        return dt.replace(tzinfo=VN_TZ).isoformat()
    except Exception:
        return None

# ================= SEARCH =================
@app.post("/search")
async def search(req: Request):
    try:
        body = await req.json()
        q = body.get("q")
    except Exception:
        q = None

    if not q:
        return JSONResponse([])

    cmd = [
        YTDLP_BIN,
        f"ytsearch10:{q}",
        "--dump-json",
        "--skip-download",
        "--flat-playlist",
        "--no-warnings",
    ]

    if YT_COOKIES and os.path.exists(YT_COOKIES):
        cmd.insert(1, "--cookies")
        cmd.insert(2, YT_COOKIES)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception:
        return JSONResponse([])

    results = []

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            it = json.loads(line)
        except Exception:
            continue

        vid = it.get("id")
        if not vid:
            continue

        results.append({
            "id": vid,
            "title": it.get("title") or "Video",
            "duration": it.get("duration") or 0,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "channelTitle": it.get("uploader") or "",
            "publishedAt": datetime.now(VN_TZ).isoformat(),
            "isLive": bool(it.get("is_live")),
        })

    return JSONResponse(results[:10])

# ================= DOWNLOAD =================
@app.post("/download")
async def download(req: Request):
    body = await req.json()
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")

    with _lock:
        task = _tasks.get(url)
        if not task:
            task = Task(url)
            _tasks[url] = task
            threading.Thread(target=_worker_download, args=(task,), daemon=True).start()

    resp = {
        "status": task.status,
        "title": task.title or None,
        "duration": task.duration or None,
    }

    if task.status == "done" and task.output_path:
        resp["downloadLink"] = f"/files/{task.output_path.name}"
    if task.status == "error":
        resp["error"] = task.error

    return JSONResponse(resp)

def _worker_download(task: Task):
    try:
        info_cmd = [
            YTDLP_BIN,
            *yt_common_args(),
            task.url,
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
        ]
        p = subprocess.run(info_cmd, capture_output=True, text=True)
        if p.returncode != 0:
            task.status = "error"
            task.error = "metadata failed"
            return

        info = json.loads(p.stdout)
        task.title = info.get("title") or task.url
        task.duration = int(info.get("duration") or 0)

        safe = safe_filename(task.title)
        out_tpl = str(TMP_DIR / f"{safe}.%(ext)s")

        cmd = [
            YTDLP_BIN,
            *yt_common_args(),
            task.url,
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "--ffmpeg-location", FFMPEG_BIN,
            "-o", out_tpl,
            "--no-playlist",
            "--no-warnings",
        ]

        task.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        task.proc.communicate()

        if task.proc.returncode != 0:
            task.status = "error"
            task.error = "download failed"
            return

        mp3 = next(TMP_DIR.glob(f"{safe}.mp3"), None)
        if not mp3:
            task.status = "error"
            task.error = "mp3 not found"
            return

        final_path = FILES_DIR / mp3.name
        shutil.move(mp3, final_path)

        task.output_path = final_path
        task.status = "done"

    except Exception as e:
        task.status = "error"
        task.error = str(e)

# ================= CANCEL =================
@app.post("/cancel")
async def cancel(req: Request):
    body = await req.json()
    url = body.get("url")
    if not url:
        return {"ok": True}

    with _lock:
        task = _tasks.pop(url, None)
        if task and task.proc:
            try:
                task.proc.terminate()
            except Exception:
                pass
    return {"ok": True}

# ================= FILE =================
@app.get("/files/{name}")
async def get_file(name: str):
    path = FILES_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg", filename=name)

@app.get("/")
async def root():
    return {
        "ok": True,
        "name": "ytmp3-backend",
        "files_base": f"{PUBLIC_BASE_URL}/files/",
        "timezone": "Asia/Ho_Chi_Minh"
    }

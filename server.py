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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import os

YT_COOKIES = os.getenv("YT_COOKIES")

def yt_cookies_args():
    if YT_COOKIES and os.path.exists(YT_COOKIES):
        return ["--cookies", YT_COOKIES]
    return []

# ====== Config ======
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
TMP_DIR = DATA_DIR / "tmp"
FILES_DIR = DATA_DIR / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")
# ví dụ khi deploy: PUBLIC_BASE_URL=https://api.tendomain.dev

# ====== App ======
app = FastAPI(title="ytmp3-backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # có thể siết domain nếu cần
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====== Task state ======
class Task:
    def __init__(self, url: str):
        self.url = url
        self.title = ""
        self.duration = 0
        self.output_path: Path | None = None
        self.status = "processing"  # processing | done | error | canceled
        self.error = None
        self.proc: subprocess.Popen | None = None

_tasks: dict[str, Task] = {}
_lock = threading.Lock()

YTDLP_BIN = os.getenv("YTDLP_BIN", "yt-dlp")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

SAFE_CHARS = f"-_.() {string.ascii_letters}{string.digits}"

def safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    cleaned = ''.join(c for c in name if c in SAFE_CHARS).strip()
    return re.sub(r"\s+", " ", cleaned)[:120] or f"audio_{random.randint(1000,9999)}"

# ====== Helpers ======

def parse_upload_date(upload_date: str | None) -> str | None:
    # yt-dlp trả về 'YYYYMMDD'
    if not upload_date:
        return None
    try:
        dt = datetime.strptime(upload_date, "%Y%m%d")
        return dt.isoformat()
    except Exception:
        return None

# ----- SEARCH (không cần API key YouTube) -----
@app.post("/search")
async def search(req: Request):
    # lấy query từ body hoặc querystring để khớp frontend
    q = None
    try:
        body = await req.json()
        q = body.get("q") if isinstance(body, dict) else None
    except Exception:
        pass
    if not q:
        q = req.query_params.get("q")
    if not q:
        raise HTTPException(status_code=400, detail="Missing q")

    # dùng yt-dlp để search: lấy JSON
    # limit 15 rồi trả về 10 item đầu cho khớp UI
    cmd = [
        YTDLP_BIN,
        *yt_cookies_args(),
        f"ytsearch15:{q}",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--flat-playlist",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        results = []
        for it in lines:
            # Khi --flat-playlist, duration có thể không có -> cần re-extract từng id khi tải
            vid = it.get("id")
            title = it.get("title") or "Video"
            thumb = None
            thumbnails = it.get("thumbnails") or []
            if isinstance(thumbnails, list) and thumbnails:
                thumb = thumbnails[-1].get("url")
            channel = it.get("uploader") or it.get("channel") or ""
            is_live = bool(it.get("is_live"))
            published_iso = parse_upload_date(it.get("upload_date")) or datetime.utcnow().isoformat()
            duration = it.get("duration") or 0
            results.append({
                "id": vid,
                "title": title,
                "duration": duration,
                "thumbnail": thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "channelTitle": channel,
                "publishedAt": published_iso,
                "isLive": is_live,
            })
        return JSONResponse(results[:10])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"search error: {e}")

# ----- DOWNLOAD -----
@app.post("/download")
async def download(req: Request):
    body = await req.json()
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")

    key = url  # dùng url làm khóa task
    with _lock:
        task = _tasks.get(key)
        if not task:
            task = Task(url)
            _tasks[key] = task
            threading.Thread(target=_worker_download, args=(task,), daemon=True).start()

    # trả trạng thái hiện tại
    resp = {
        "status": task.status,
        "title": task.title or None,
        "duration": task.duration or None,
    }
    if task.status == "done" and task.output_path:
        rel = task.output_path.relative_to(BASE_DIR)
        # frontend ghép PUBLIC_BASE_URL + downloadLink
        resp["downloadLink"] = f"/files/{task.output_path.name}"
    if task.status == "error":
        resp["error"] = task.error or "unknown"
    return JSONResponse(resp)


def _worker_download(task: Task):
    try:
        # B1: lấy metadata chi tiết để biết title, duration
        info_cmd = [
            YTDLP_BIN, task.url,
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
        ]
        p = subprocess.run(info_cmd, capture_output=True, text=True, check=False)
        if p.returncode != 0:
            task.status = "error"
            task.error = p.stderr.strip() or "metadata failed"
            return
        info = json.loads(p.stdout)
        task.title = info.get("title") or task.url
        task.duration = int(info.get("duration") or 0)

        # B2: tải & convert mp3
        safe = safe_filename(task.title)
        out_tpl = str((TMP_DIR / f"{safe}.%(ext)s").as_posix())
        cmd = [
            YTDLP_BIN,
            *yt_cookies_args(),
            task.url,
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "--ffmpeg-location", FFMPEG_BIN,
            "-o", out_tpl,
            "--no-playlist",
            "--no-warnings",
        ]

        task.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = task.proc.communicate()
        if task.proc.returncode != 0:
            task.status = "error"
            task.error = (stderr or stdout or "download failed")[-800:]
            return

        # tìm file mp3 vừa tạo ở TMP_DIR
        created = sorted(TMP_DIR.glob(f"{safe}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        mp3 = next((p for p in created if p.suffix.lower() == ".mp3"), None)
        if not mp3:
            task.status = "error"
            task.error = "mp3 not found"
            return

        # move sang FILES_DIR
        final_name = f"{safe}.mp3"
        final_path = FILES_DIR / final_name
        # tránh trùng tên: thêm 4 ký tự nếu đã tồn tại
        if final_path.exists():
            rand = ''.join(random.choices(string.ascii_lowercase+string.digits, k=4))
            final_path = FILES_DIR / f"{safe}-{rand}.mp3"
        shutil.move(str(mp3), final_path)
        task.output_path = final_path
        task.status = "done"
    except Exception as e:
        task.status = "error"
        task.error = str(e)
    finally:
        # dọn rác tệp tạm còn sót
        for p in TMP_DIR.glob("*.*"):
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
            except Exception:
                pass

# ----- CANCEL -----
@app.post("/cancel")
async def cancel(req: Request):
    body = await req.json()
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    with _lock:
        task = _tasks.get(url)
        if not task:
            return {"ok": True, "message": "no task"}
        if task.proc and task.status == "processing":
            try:
                # gửi tín hiệu hạ gục cả group process
                os.killpg(os.getpgid(task.proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    task.proc.terminate()
                except Exception:
                    pass
        task.status = "canceled"
        _tasks.pop(url, None)
    return {"ok": True}

# ----- Serve files (download link) -----
@app.get("/files/{name}")
async def get_file(name: str):
    path = FILES_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=name, media_type="audio/mpeg")

# healthcheck
@app.get("/")
async def root():
    return {"ok": True, "name": "ytmp3-backend", "files_base": f"{PUBLIC_BASE_URL}/files/"}

FROM python:3.11-slim

# ffmpeg & deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
RUN mkdir -p /app/data/tmp /app/data/files

ENV PUBLIC_BASE_URL="http://localhost:3000"
ENV YTDLP_BIN="yt-dlp"
ENV FFMPEG_BIN="ffmpeg"

EXPOSE 3000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3000"]

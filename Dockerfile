# Backend: FFmpeg for merging tracks, Deno for yt-dlp YouTube JS challenges.
FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.12-slim
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/server.py ./server.py
RUN mkdir -p /tmp/mp4-studio && chmod 1777 /tmp/mp4-studio
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=10000 HOST=0.0.0.0
EXPOSE 10000
CMD ["python", "server.py"]

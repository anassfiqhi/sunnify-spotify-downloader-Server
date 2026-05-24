FROM python:3.12-slim

# ffmpeg is required by yt-dlp for audio conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp separately so it can be updated independently
RUN pip install --no-cache-dir yt-dlp

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY spotifydown_api.py .
COPY server.py .

ENV PORT=8001
ENV CACHE_DIR=/tmp/sunnify

EXPOSE ${PORT}

# Shell form so ${PORT} is expanded at runtime from Railway's injected env var
CMD gunicorn server:app --bind 0.0.0.0:${PORT} --workers 4 --timeout 300 --keep-alive 5

FROM python:3.12-slim

# yt-dlp needs ffmpeg on PATH for audio extraction and 1080p+ merges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Most PaaS hosts (Render, Railway, etc.) inject $PORT at runtime;
# app.py reads it via os.environ. Defaults to 5000 if unset.
# --workers 1 is required: job progress/state lives in an in-memory dict
# (see `jobs` in app.py), so multiple worker processes would each have
# their own copy and progress polling would randomly 404. --threads gives
# concurrency instead, which is enough for one user's browser tabs.
CMD gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 8 --timeout 0 app:app

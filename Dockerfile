FROM python:3.12-slim

# yt-dlp needs:
#  - ffmpeg on PATH for audio extraction and 1080p+ merges
#  - a JS runtime on PATH (Deno here) to solve YouTube's signature/"n"
#    challenge. As of 2026, YouTube forces "SABR streaming" and strips
#    the real format URLs from every client's response unless yt-dlp can
#    run its JS challenge-solver — without a JS runtime present, this
#    surfaces as "Requested format is not available" even though
#    extraction and cookies are working fine. Deno is used because it's
#    a single static binary — no separate npm/node_modules step needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && chmod +x /usr/local/bin/deno

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
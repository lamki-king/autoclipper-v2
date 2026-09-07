FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu curl ca-certificates unzip git nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Install Deno for yt-dlp's EJS challenge solver.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y && \
    /usr/local/bin/deno --version

# Install the matching bgutil PO-token generation server for yt-dlp.
# Use the supported Node server path; it is simpler and more reliable than
# starting the server from inside node_modules with Deno.
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider && \
    cd /opt/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
ENV OPENAI_TRANSCRIBE_MODEL=whisper-1
ENV MAX_UPLOAD_MB=500
ENV PATH="/usr/local/bin:${PATH}"

EXPOSE 8000

# Run the PO-token provider beside FastAPI.
CMD ["sh", "-c", "cd /opt/bgutil-ytdlp-pot-provider/server && node build/main.js --host 127.0.0.1 --port 4416 & exec uvicorn app.entry:app --host 0.0.0.0 --port 8000"]

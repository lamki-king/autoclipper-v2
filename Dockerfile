FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu curl ca-certificates unzip git && \
    rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y && \
    /usr/local/bin/deno --version

# Install the matching bgutil PO-token generation server for yt-dlp.
RUN git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider && \
    cd /opt/bgutil-ytdlp-pot-provider/server && \
    deno install --allow-scripts=npm:canvas --frozen

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
ENV OPENAI_TRANSCRIBE_MODEL=whisper-1
ENV MAX_UPLOAD_MB=500
ENV PATH="/usr/local/bin:${PATH}"

EXPOSE 8000

# Start the bgutil HTTP PO-token server beside FastAPI.
CMD ["sh", "-c", "cd /opt/bgutil-ytdlp-pot-provider/server/node_modules && deno run --no-prompt --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts --port 4416 & exec uvicorn app.entry:app --host 0.0.0.0 --port 8000"]

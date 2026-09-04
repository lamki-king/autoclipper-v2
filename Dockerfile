FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu curl ca-certificates unzip && \
    rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y && \
    /usr/local/bin/deno --version

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
ENV OPENAI_TRANSCRIBE_MODEL=whisper-1
ENV MAX_UPLOAD_MB=500
ENV PATH="/usr/local/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]

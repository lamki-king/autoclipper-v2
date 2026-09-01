FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
ENV OPENAI_TRANSCRIBE_MODEL=whisper-1
ENV MAX_UPLOAD_MB=500
EXPOSE 8000
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]

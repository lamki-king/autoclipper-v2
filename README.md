# AutoClipper V2

AI-powered automatic video clipping backend for long-form videos.

## What it does

1. Accepts a video upload through FastAPI.
2. Extracts audio with FFmpeg.
3. Transcribes speech through the OpenAI Audio API.
4. Uses an OpenAI text model to select strong 15–45 second moments.
5. Crops clips to 9:16, scales them to 1080x1920, and burns captions.
6. Optionally overlays a watermark.
7. Exposes job status and clip download endpoints.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=your_key_here
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check: `GET /health`

## Docker

```bash
docker build -t autoclipper-v2 .
docker run --rm -p 8000:8000 -e OPENAI_API_KEY=your_key_here autoclipper-v2
```

## Render

The repository includes `render.yaml`. Render should use the Dockerfile and the `OPENAI_API_KEY` secret.

**Do not commit an API key.** Add the key in Render's Environment settings. The previously exported Render configuration used the secret value as the environment-variable name; that was incorrect. The variable name must be exactly `OPENAI_API_KEY`.

## API

- `GET /` — service status.
- `GET /health` — health/configuration status.
- `POST /process` — upload a video and optionally a watermark. Returns a job ID.
- `GET /status/{job_id}` — processing status and completed clip metadata.
- `GET /download/{job_id}/{clip_index}` — download a rendered clip.

## Environment variables

- `OPENAI_API_KEY` — required; keep it secret.
- `OPENAI_TEXT_MODEL` — defaults to `gpt-5.6-luna`.
- `OPENAI_TRANSCRIBE_MODEL` — defaults to `whisper-1`.
- `MAX_UPLOAD_MB` — defaults to 500.

## Security

Never put API keys in Git, source files, README files, or Render YAML values. Rotate any key that has already been exposed.

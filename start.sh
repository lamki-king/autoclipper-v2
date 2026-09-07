#!/bin/sh
set -e
exec uvicorn app.entry:app --host 0.0.0.0 --port "${PORT:-10000}"

#!/bin/sh
set -eu

BGUTIL_DIR=/opt/bgutil-ytdlp-pot-provider/server
BGUTIL_LOG=/tmp/bgutil.log

if [ -f "$BGUTIL_DIR/build/main.js" ]; then
  cd "$BGUTIL_DIR"
  node build/main.js --port 4416 >"$BGUTIL_LOG" 2>&1 &
  BGUTIL_PID=$!
else
  echo "bgutil provider build not found" >&2
  exit 1
fi

READY=0
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:4416/health >/dev/null 2>&1 || curl -fsS http://127.0.0.1:4416/ >/dev/null 2>&1; then
    READY=1
    break
  fi
  if ! kill -0 "$BGUTIL_PID" 2>/dev/null; then
    cat "$BGUTIL_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "bgutil provider did not become ready" >&2
  cat "$BGUTIL_LOG" >&2 || true
  kill "$BGUTIL_PID" 2>/dev/null || true
  exit 1
fi

trap 'kill "$BGUTIL_PID" 2>/dev/null || true' EXIT
exec uvicorn app.entry:app --host 0.0.0.0 --port "${PORT:-10000}"

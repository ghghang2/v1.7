#!/usr/bin/env bash
# ============================================================================
# start-endpoint.sh — expose the local inference server (port 8080) to the
# internet via a Cloudflare quick tunnel, and print the public URL.
#
# The RunPod SSH gateway (ssh.runpod.io) cannot carry SSH port forwards, so
# a tunnel that dials OUT from the pod is the only reliable way to reach the
# endpoint from a laptop.
#
# Usage:
#   bash start-endpoint.sh          # foreground; Ctrl-C stops the tunnel
#   bash start-endpoint.sh -d       # detached; URL printed at the end
#
# Notes:
#   - The URL is TEMPORARY: a new random name is generated on every start,
#     and it dies when this process or the pod stops.
#   - The tunnel is UNAUTHENTICATED: anyone with the URL can use the model.
#   - Re-running the script kills any previous quick tunnel first.
# ============================================================================
set -euo pipefail

MODEL_PORT="${MODEL_PORT:-8080}"   # local inference port
CF_BIN="${CF_BIN:-/usr/local/bin/cloudflared}"
LOG=/tmp/cloudflared.log

# ---------------------------------------------------------------------------
# 1. Make sure cloudflared exists (pod images can be fresh).
# ---------------------------------------------------------------------------
if [ ! -x "$CF_BIN" ]; then
    echo "cloudflared not found - downloading..."
    (command -v curl >/dev/null && curl -fsSL -o "$CF_BIN" \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64) \
        || python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64', '$CF_BIN')"
    chmod +x "$CF_BIN"
fi

# ---------------------------------------------------------------------------
# 2. Wait for the inference server to answer /health (up to ~120 s).
# ---------------------------------------------------------------------------
echo "Waiting for inference server on 127.0.0.1:$MODEL_PORT ..."
for i in $(seq 1 120); do
    if python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$MODEL_PORT/health', timeout=2)" 2>/dev/null; then
        echo "Server is up."
        break
    fi
    [ "$i" -eq 120 ] && { echo "ERROR: server on port $MODEL_PORT never came up." >&2; exit 1; }
    sleep 1
done

# ---------------------------------------------------------------------------
# 3. Kill any stale quick tunnel so we start clean.
# ---------------------------------------------------------------------------
pkill -f "cloudflared tunnel --url http://127.0.0.1:$MODEL_PORT" 2>/dev/null || true
sleep 1

# ---------------------------------------------------------------------------
# 4. Launch the tunnel.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "-d" ]; then
    nohup "$CF_BIN" tunnel --url "http://127.0.0.1:$MODEL_PORT" --no-autoupdate >"$LOG" 2>&1 &
    TPID=$!
    echo "Tunnel running in background (pid $TPID), logging to $LOG"
    URL=""
    for i in $(seq 1 30); do
        URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1) || true
        [ -n "$URL" ] && break
        sleep 1
    done
    if [ -z "$URL" ]; then
        echo "ERROR: no URL appeared in $LOG - check the log." >&2; exit 1
    fi
    echo
    echo "Your endpoint (OpenAI-compatible, no API key needed):"
    echo "  $URL"
else
    exec "$CF_BIN" tunnel --url "http://127.0.0.1:$MODEL_PORT" --no-autoupdate
fi

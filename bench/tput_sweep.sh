#!/usr/bin/env bash
# bench/tput_sweep.sh — throughput sweep for Qwen3.8-27B UD-Q4_K_XL on RTX 5090.
#
# Workload mirrors the agent: 32k prompt / 1500 gen, c1.
#
# Trimmed to 6 runs (~7 min total) — only knobs that correlate with max
# throughput. Dropped vs the original plan: fa-off sanity, -t 8 (kept only
# -t 32), and the 8k/16k/65k context-scaling runs (degradation curve, not a
# config we'd ship).
#
# Self-contained by design. This session runs THROUGH the prod llama-server
# (port 8889), and a second copy of the 17.2 GiB model does not fit beside it
# in 32 GiB VRAM. So this script:
#   1. snapshots the live server command line (to restart it identically),
#   2. stops the prod server via run.py (this session dies mid-turn; expected),
#   3. runs the 6-run sweep,
#   4. restarts llama-server from the snapshot,
#   5. relaunches the nbchat TUI so the client is usable again.
#
# Usage:  nohup bash bench/tput_sweep.sh > /tmp/tput_sweep.out 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."

# Refuse to run twice (lock file; pgrep -f would match its own wrapper).
LOCK=/tmp/tput_sweep.lock
if [ -e "$LOCK" ]; then
  echo "tput_sweep already running (lock: $LOCK) — exiting."
  exit 1
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

MODEL=(-hf unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL)
WL=(-p 32768 -n 1500)                       # agent prompts run 16k-33k in, ~1.5k out
COMMON=(-ngl 999 -fa on -lm mmap -t 16 -r 2 --delay 5 -o csv)
OUT=bench/results/tput_sweep.csv
mkdir -p bench/results

# 1) Snapshot the live server command line for a faithful restart later.
LIVE_CMD=$(tr '\0' ' ' < /proc/$(ss -ltnp 2>/dev/null | grep 8889 | grep -oP 'pid=\K[0-9]+' | head -1)/cmdline)
echo "[$(date)] captured: $LIVE_CMD"

echo "[$(date)] stopping prod server..."
SERVER_PID=$(ss -ltnp 2>/dev/null | grep 8889 | grep -oP 'pid=\K[0-9]+' | head -1)
if [ -n "$SERVER_PID" ]; then
  kill "$SERVER_PID" 2>/dev/null
  for i in $(seq 1 15); do
    ss -ltn 2>/dev/null | grep -q 8889 || break
    sleep 1
  done
  ss -ltn 2>/dev/null | grep -q 8889 && kill -9 "$SERVER_PID" 2>/dev/null && sleep 2
fi
if ss -ltn 2>/dev/null | grep -q 8889; then
  echo "ERROR: could not stop server on port 8889 - aborting to avoid VRAM conflict."
  exit 1
fi
echo "[$(date)] server stopped, port 8889 free."

# 1. baseline = current production llama-server settings (f16 KV, 4096/2048)
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 4096 -ub 2048 "${COMMON[@]}" >> "$OUT"

# 2. KV cache quant: f16 -> q8_0 (halves KV read bytes per decode step).
#    27B Q4 on 5090 decode is bandwidth-bound; smaller KV reads faster.
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 4096 -ub 2048 -ctk q8_0 -ctv q8_0 "${COMMON[@]}" >> "$OUT"

# 3. KV cache quant: q4_0 (quarters KV). Free throughput if quality holds.
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 4096 -ub 2048 -ctk q4_0 -ctv q4_0 "${COMMON[@]}" >> "$OUT"

# 4. Prefill chunk: 4096/2048 -> 8192/4096. 32k prefill dominates wall-time;
#    bigger chunks usually win on the 5090.
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 8192 -ub 4096 "${COMMON[@]}" >> "$OUT"

# 5. Load mode: mlock keeps the 17 GiB weights pinned in RAM (no page faults
#    on mmap reads). Silently skipped if ulimit -l blocks it.
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 4096 -ub 2048 -lm mlock "${COMMON[@]}" >> "$OUT" \
  || echo "[skip] mlock run failed (RLIMIT_MEMLOCK?)" >> "$OUT"

# 6. Host threads: prod uses -t 16; try 32 on the 256-core box.
./llama-bench "${MODEL[@]}" "${WL[@]}" -b 4096 -ub 2048 -t 32 "${COMMON[@]}" >> "$OUT"

# 7. Restart the prod server from the snapshot (same binary, same args).
echo "[$(date)] restarting prod server..."
if ! (exec 3<>/dev/tcp/127.0.0.1/8889) 2>/dev/null; then
  # shellcheck disable=SC2086
  setsid nohup $LIVE_CMD >> ./llama_server.log 2>&1 &
  disown 2>/dev/null || true
fi

# This sweep killed the TUI's server mid-session; relaunch the TUI so the
# client is usable again once llama-server is back up.
pkill -f "nbchat.tui" 2>/dev/null || true
sleep 2
setsid nohup python -m nbchat.tui >/var/log/nbchat_tui.log 2>&1 &
disown 2>/dev/null || true

echo
echo "Done. Results: $OUT"

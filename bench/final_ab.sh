#!/usr/bin/env bash
# bench/final_ab.sh -- production-condition A/B: default ubatch (512) vs ubatch 2048,
# plus a parallel-2 candidate. Runs all on port 8889; restarts production at the end.
# Output: bench/results/final_ab.log, probes_A*.jsonl / probes_B*.jsonl / probes_C*.jsonl
set -u
cd /v1.6
LLAMA=./llama-server
PY=python3
RES=bench/results
LOG=$RES/final_ab.log
exec >>"$LOG" 2>&1
echo "=== FINAL A/B $(date -u +%FT%TZ) ==="

HFMODEL="unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"
BUDGET_MSG='... I am thinking for too long -- let me gather more info about the task.'

PROD_CFG=(-hf "$HFMODEL" --port 8889 --parallel 1 --ctx-size 262144 --n-gpu-layers 999
  --flash-attn 1 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --reasoning on
  --chat-template-kwargs '{"reasoning_effort": "xhigh"}'
  --spec-default --spec-type draft-mtp --reasoning-preserve --fit off --agent
  --reasoning-budget 4096 --reasoning-budget-message "$BUDGET_MSG"
  --no-mmproj --repeat-penalty 1.0 --load-mode mlock --metrics)

stop_cur() {
  local pid
  pid=$($PY -c 'import json;print(json.load(open("service_info.json"))["pids"]["llama"])' 2>/dev/null)
  if [ -n "${pid:-}" ]; then kill -TERM "$pid" 2>/dev/null; fi
  for _ in $(seq 1 60); do pgrep -x llama-server >/dev/null || break; sleep 1; done
  pkill -KILL -x llama-server 2>/dev/null
  sleep 2
}

wait_health() {
  local port=$1
  for _ in $(seq 1 300); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

start_on() { # $1=log $2=pidfile, rest = server args
  local logf=$1 pidf=$2; shift 2
  nohup setsid "$LLAMA" "$@" >>"$logf" 2>&1 &
  echo $! >"$pidf"
}

pp_probes() { # $1=label $2=port
  $PY bench/probe.py "$2" "${1}_pp12k" '{"filler_chars": 48000, "max_tokens": 8, "stream": true}' \
    >> "$RES/probes_${1}.jsonl"
  $PY bench/probe.py "$2" "${1}_pp24k" '{"filler_chars": 96000, "max_tokens": 8, "stream": true}' \
    >> "$RES/probes_${1}.jsonl"
}

grab_timing() { # $1=label $2=logpath
  grep -E "prompt eval time|draft acceptance|eval time" "$2" 2>/dev/null | tail -6 \
    | sed "s/^/  [timing $1] /"
  nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader \
    | sed "s/^/  [gpu $1] /"
}

# ---------------- A: current production (ubatch 512 default) ----------------
echo "### A: current production $(date +%H:%M:%S)"
pp_probes A_current 8889
grab_timing A_current "$PWD/llama_server.log"

# ---------------- B: ubatch 2048 / batch 4096, rest identical ----------------
echo "### B: stop prod, start B (ub 2048) $(date +%H:%M:%S)"
stop_cur
start_on "$RES/srv_B.log" "$RES/srv_B.pid" \
  -hf "$HFMODEL" --port 8889 --parallel 1 --ctx-size 262144 --n-gpu-layers 999
  --flash-attn 1 -b 4096 -ub 2048 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0
  --reasoning on --chat-template-kwargs '{"reasoning_effort": "xhigh"}'
  --spec-default --spec-type draft-mtp --reasoning-preserve --fit off --agent
  --reasoning-budget 4096 --reasoning-budget-message "$BUDGET_MSG"
  --no-mmproj --repeat-penalty 1.0 --load-mode auto --metrics
if ! wait_health 8889; then echo "B FAILED TO START"; tail -20 "$RES/srv_B.log"; fi
pp_probes B_ub2048 8889
grab_timing B_ub2048 "$RES/srv_B.log"

# ---------------- C: parallel 2, ctx 262144 total, ub 2048 ----------------
echo "### C: stop B, start C (parallel 2) $(date +%H:%M:%S)"
stop_cur
start_on "$RES/srv_C.log" "$RES/srv_C.pid" \
  -hf "$HFMODEL" --port 8889 --parallel 2 --ctx-size 262144 --n-gpu-layers 999
  --flash-attn 1 -b 4096 -ub 2048 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0
  --reasoning on --chat-template-kwargs '{"reasoning_effort": "xhigh"}'
  --spec-default --spec-type draft-mtp --reasoning-preserve --fit off --agent
  --reasoning-budget 4096 --reasoning-budget-message "$BUDGET_MSG"
  --no-mmproj --repeat-penalty 1.0 --load-mode auto --metrics
if ! wait_health 8889; then echo "C FAILED TO START"; tail -20 "$RES/srv_C.log"; fi
$PY bench/probe.py 8889 C_par2_pp6k '{"filler_chars": 24000, "max_tokens": 8, "stream": true, "n": 2}' \
  >> "$RES/probes_C.jsonl"
$PY bench/probe.py 8889 C_par2_tg '{"filler_chars": 24000, "max_tokens": 256, "stream": true, "n": 2}' \
  >> "$RES/probes_C.jsonl"
grab_timing C_par2 "$RES/srv_C.log"

# ---------------- restart production ----------------
echo "### restart production $(date +%H:%M:%S)"
stop_cur
start_on "$PWD/llama_server.log" bench/prod.pid "${PROD_CFG[@]}"
if wait_health 8889; then
  $PY -c 'import json,time,pid; json.dump({"pids":{"llama":int(open("bench/prod.pid").read().strip())},"started_at":time.strftime("%Y-%m-%d %H:%M:%S")}, open("service_info.json","w"), indent=2)' 2>/dev/null || \
  $PY - "$PWD/bench/prod.pid" <<'EOF'
import json, sys, time
json.dump({"pids": {"llama": int(open(sys.argv[1]).read().strip())},
           "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
          open("service_info.json", "w"), indent=2)
EOF
  echo "PROD OK $(date +%H:%M:%S)"
else
  echo "PROD FAILED TO RESTART -- NEEDS ATTENTION"
fi
echo "=== FINAL A/B COMPLETE $(date -u +%FT%TZ) ==="

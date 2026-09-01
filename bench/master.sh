#!/usr/bin/env bash
# bench/master.sh -- full L40S optimization campaign (runs detached).
#
#   1. Stop production llama-server (the one serving the agent session).
#   2. llama-bench sweeps (pp: FA x KV-quant x ubatch; tg: FA x KV-quant x ctx-depth).
#   3. Temp llama-server validation of candidate configs (incl. MTP spec, parallelism)
#      with real API probes measuring pp/tg/TTFT/aggregate throughput.
#   4. Restart production with the ORIGINAL (bug-fixed) config.
#
# Safety: EXIT trap restarts production if it is not running at exit (any reason).
# All output: bench/results/master.log   Probe data: bench/results/cfg_*.json
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
cd "$ROOT"

MODEL=/root/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/f1bfb127c64f7072bdd2cad55f258b9c8b2910fe/Qwen3.8-27B-UD-Q4_K_XL.gguf
LLAMA=./llama-server
BENCH=./llama-bench
PY=python3
RES=bench/results
LOG=$RES/master.log
mkdir -p "$RES"
exec >>"$LOG" 2>&1
echo "=== master.sh started $(date -u +%FT%TZ) ==="

# cleanup stale artifacts (NEVER touch master.log -- this script's own log)
rm -f bench/results/phase1.log bench/results/gpu_log.csv bench/results/*.csv \
      bench/results/*.json* bench/results/probes_*.jsonl bench/results/srv_*.log \
      bench/done.txt bench/prod.pid 2>/dev/null

HFMODEL="unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"

# Production config: EXACTLY run.py's current T4 config, with the argument
# concatenation bug fixed (original had: --reasoning-budget-message ...--no-mmproj
# as ONE arg, so mmproj was loaded and --no-mmproj never applied).
PROD_CFG=(-hf "$HFMODEL" --port 8889 --parallel 1 --ctx-size 262144 --n-gpu-layers 999
  --flash-attn 1 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --reasoning on
  --chat-template-kwargs '{"reasoning_effort": "xhigh"}'
  --spec-default --spec-type draft-mtp --reasoning-preserve --fit off --agent
  --reasoning-budget 4096
  --reasoning-budget-message '... I am thinking for too long -- let me gather more info about the task.'
  --no-mmproj --repeat-penalty 1.0 --load-mode mlock --metrics)

# --------------------------------------------------------------------------
prod_pid() {
  $PY - <<'EOF' 2>/dev/null
import json
try:
    print(json.load(open("service_info.json"))["pids"]["llama"])
except Exception:
    pass
EOF
}

stop_prod() {
  local pid; pid=$(prod_pid)
  if [ -n "${pid:-}" ]; then
    echo "[prod] stopping PID $pid"
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null
  fi
  # belt & braces: only exact llama-server binaries
  pkill -TERM -x llama-server 2>/dev/null
  for _ in $(seq 1 30); do pgrep -x llama-server >/dev/null || break; sleep 1; done
  pkill -KILL -x llama-server 2>/dev/null
  sleep 2
}

start_prod() {
  echo "[prod] starting production server (ulimit -l unlimited)"
  ( ulimit -l unlimited; cd "$ROOT"; nohup setsid "$LLAMA" "${PROD_CFG[@]}" >> "$ROOT/llama_server.log" 2>&1 &
    echo $! > bench/prod.pid )
  local pid; pid=$(cat bench/prod.pid 2>/dev/null)
  for _ in $(seq 1 300); do
    curl -sf http://127.0.0.1:8889/health >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf http://127.0.0.1:8889/health >/dev/null 2>&1 || { echo "[prod] FAILED to come up"; return 1; }
  echo "[prod] healthy (PID $pid)"
  $PY - "$pid" <<'EOF'
import json, sys, time
json.dump({"pids": {"llama": int(sys.argv[1])}, "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
          open("service_info.json", "w"), indent=2)
EOF
  return 0
}

prod_running() { curl -sf http://127.0.0.1:8889/health >/dev/null 2>&1; }

# --------------------------------------------------------------------------
# GPU sampler for whole campaign
( while true; do sleep 15
    nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used,power.draw,clocks.sm --format=csv,noheader >> "$RES/gpu_log.csv" 2>/dev/null
  done ) &
GPU_PID=$!

cleanup() {
  kill "$GPU_PID" 2>/dev/null
  echo "=== master.sh finishing $(date -u +%FT%TZ) ==="
  if ! prod_running; then
    echo "[trap] production not running -- starting it"
    start_prod
  else
    echo "[trap] production already running -- OK"
  fi
}
trap cleanup EXIT

# ==========================================================================
echo "### STEP 1: stop production"
stop_prod
sleep 5

# ==========================================================================
echo "### STEP 2: llama-bench sweeps"
B="-r 3 --offline -ngl 999 -o csv"

# PP sweep: FA x KV-quant x ubatch, prompts 512/2048/8192
for FA in on off; do
  for CT in f16 q8_0; do
    echo "[bench] pp FA=$FA CT=$CT $(date +%H:%M:%S)"
    $BENCH -m "$MODEL" -p 512,2048,8192 -n 0 -b 2048 -ub 128,256,512,1024,2048 \
      -fa "$FA" -ctk "$CT" -ctv "$CT" $B -o csv > "$RES/pp_fa_${FA}_ct_${CT}.csv" 2>&1
    echo "[bench] pp FA=$FA CT=$CT rc=$? $(date +%H:%M:%S)"
  done
done

# TG sweep: FA x KV-quant x depth, 256 tokens gen, default batch/ubatch
for FA in on off; do
  for CT in f16 q8_0; do
    echo "[bench] tg FA=$FA CT=$CT $(date +%H:%M:%S)"
    $BENCH -m "$MODEL" -p 0 -n 256 -b 2048 -ub 512 -d 0,4096,16384 \
      -fa "$FA" -ctk "$CT" -ctv "$CT" $B > "$RES/tg_fa_${FA}_ct_${CT}.csv" 2>&1
    echo "[bench] tg FA=$FA CT=$CT rc=$? $(date +%H:%M:%S)"
  done
done

# ==========================================================================
echo "### STEP 3: server config validation (port 8901)"
PORT=8901

wait_health() {
  local port=$1
  for _ in $(seq 1 300); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

kill_bench_server() {
  pkill -TERM -f "llama-server .*--port $PORT " 2>/dev/null
  for _ in $(seq 1 30); do pgrep -f "llama-server .*--port $PORT " >/dev/null || break; sleep 1; done
  pkill -KILL -f "llama-server .*--port $PORT " 2>/dev/null
  sleep 3
}

# run_cfg LABEL CTX_PARALLEL_ARGS... : starts server, runs probes, records JSON, stops server
run_cfg() {
  local label=$1; shift
  local args=("$@")
  echo "[cfg] ===== $label : ${args[*]} $(date +%H:%M:%S)"
  kill_bench_server
  ( ulimit -l unlimited; nohup setsid "$LLAMA" -hf "$HFMODEL" --port $PORT "${args[@]}" \
      --no-mmproj --metrics --log-verbosity 1 > "$RES/srv_${label}.log" 2>&1 &
    echo $! > "$RES/srv_${label}.pid" )
  if ! wait_health $PORT; then
    echo "[cfg] $label FAILED to start; see $RES/srv_${label}.log"
    echo "{\"label\": \"$label\", \"error\": \"server failed to start\"}" > "$RES/cfg_${label}.json"
    kill_bench_server
    return 1
  fi
  local h=$(curl -s "http://127.0.0.1:$PORT/health")
  echo "[cfg] $label healthy: $h"

  # probe: tg short context
  $PY bench/probe.py $PORT "${label}_tg_short" '{"filler_chars": 0, "max_tokens": 512, "stream": true}' \
      >> "$RES/probes_${label}.jsonl"
  # probe: tg long context (~24k tokens)
  $PY bench/probe.py $PORT "${label}_tg_long" '{"filler_chars": 96000, "max_tokens": 512, "stream": true}' \
      >> "$RES/probes_${label}.jsonl"
  # probe: pp 2k (~2k tokens)
  $PY bench/probe.py $PORT "${label}_pp_2k" '{"filler_chars": 8000, "max_tokens": 8, "stream": true}' \
      >> "$RES/probes_${label}.jsonl"
  # probe: pp 8k (~8k tokens)
  $PY bench/probe.py $PORT "${label}_pp_8k" '{"filler_chars": 32000, "max_tokens": 8, "stream": true}' \
      >> "$RES/probes_${label}.jsonl"

  # MTP acceptance counters (cumulative since server start) from Prometheus metrics
  $PY - "$PORT" "$label" <<'EOF' >> "$RES/probes_${label}.jsonl"
import json, sys, urllib.request
port, label = sys.argv[1], sys.argv[2]
try:
    txt = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10).read().decode()
    vals = {}
    for line in txt.splitlines():
        if line.startswith("llamacpp:"):
            parts = line.rsplit(" ", 1)
            vals[parts[0]] = parts[1]
    d = {k: v for k, v in vals.items() if "spec_decode" in k}
    out = {"label": f"{label}_mtp", "metrics": d}
    if d.get("llamacpp:spec_decode_num_draft_tokens_total"):
        tot = lambda k: float(d.get(k, 0.0))
        drafts = tot("llamacpp:spec_decode_num_draft_tokens_total")
        acc = tot("llamacpp:spec_decode_num_accepted_tokens_total")
        out["acceptance_rate"] = round(acc / drafts, 4) if drafts else None
        out["mean_accept_len"] = round(1 + acc / max(1e-9, tot("llamacpp:spec_decode_num_drafts_total")), 3)
    print(json.dumps(out))
except Exception as e:
    print(json.dumps({"label": f"{label}_mtp", "error": str(e)}))
EOF

  kill_bench_server
  echo "[cfg] ===== $label done $(date +%H:%M:%S)"
}

BASE=(--ctx-size 32768 --n-gpu-layers 999 --parallel 1 --load-mode auto)

run_cfg c0_base    "${BASE[@]}" -b 2048 -ub 512 -fa on  -ctk f16  -ctv f16
run_cfg c1_spec    "${BASE[@]}" -b 2048 -ub 512 -fa on  -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp
run_cfg c2_ub1024  "${BASE[@]}" -b 2048 -ub 1024 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp
run_cfg c3_ub2048  "${BASE[@]}" -b 8192 -ub 2048 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp
run_cfg c4_nofa    "${BASE[@]}" -b 2048 -ub 512 -fa off -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp
run_cfg c5_q8kv    "${BASE[@]}" -b 2048 -ub 512 -fa on  -ctk q8_0 -ctv q8_0 --spec-default --spec-type draft-mtp
run_cfg c6_ub256   "${BASE[@]}" -b 2048 -ub 256 -fa on  -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp
run_cfg c7_b4k     "${BASE[@]}" -b 4096 -ub 1024 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp

# ==========================================================================
echo "### STEP 4: parallelism (uses best single-slot tg config from probes)"
BEST=$($PY - <<'EOF'
import json, glob
best, best_tg = None, -1.0
for f in glob.glob("bench/results/probes_c*.jsonl"):
    try:
        lab = f.split("probes_")[1].split(".")[0]
        tg = None
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            j = json.loads(line)
            if j["label"].endswith("_tg_short"):
                tg = j.get("mean_tg_tok_s") or 0
        if tg and tg > best_tg:
            best, best_tg = lab, tg
    except Exception:
        pass
print(best or "c2_ub1024")
EOF
)
echo "[par] best single-slot config for parallelism test: $BEST"
# reuse the same server args as $BEST
case "$BEST" in
  c0_base)   PAR=(-b 2048 -ub 512 -fa on  -ctk f16  -ctv f16) ;;
  c1_spec)   PAR=(-b 2048 -ub 512 -fa on  -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  c2_ub1024) PAR=(-b 2048 -ub 1024 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  c3_ub2048) PAR=(-b 8192 -ub 2048 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  c4_nofa)   PAR=(-b 2048 -ub 512 -fa off -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  c5_q8kv)   PAR=(-b 2048 -ub 512 -fa on  -ctk q8_0 -ctv q8_0 --spec-default --spec-type draft-mtp) ;;
  c6_ub256)  PAR=(-b 2048 -ub 256 -fa on  -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  c7_b4k)    PAR=(-b 4096 -ub 1024 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
  *)         PAR=(-b 2048 -ub 1024 -fa on -ctk f16  -ctv f16 --spec-default --spec-type draft-mtp) ;;
esac

run_par() {
  local label=$1 pctx=$2
  local pchars=$3 np=$4
  echo "[par] ===== $label (ctx/slot $pctx) $(date +%H:%M:%S)"
  kill_bench_server
  ( ulimit -l unlimited; nohup setsid "$LLAMA" -hf "$HFMODEL" --port $PORT \
      --ctx-size "$pctx" --n-gpu-layers 999 --parallel "$np" "${PAR[@]}" \
      --no-mmproj --metrics --log-verbosity 1 > "$RES/srv_${label}.log" 2>&1 &
    echo $! > "$RES/srv_${label}.pid" )
  if ! wait_health $PORT; then
    echo "[par] $label FAILED to start"
    kill_bench_server
    return 1
  fi
  $PY bench/probe.py $PORT "${label}_par${np}" \
    "{\"filler_chars\": $pchars, \"max_tokens\": 256, \"stream\": true, \"n\": $np}" \
    >> "$RES/probes_${label}.jsonl"
  kill_bench_server
  echo "[par] ===== $label done $(date +%H:%M:%S)"
}

# --parallel is set per config; run_cfg hardcodes --parallel 1 in BASE,
# so parallel runs use a dedicated function above.
# NOTE: --ctx-size is the TOTAL context shared across slots (per-slot = total/parallel)
run_par p2 131072 48000 2
run_par p4 65536 12000 4
run_par p8 32768 12000 8

# ==========================================================================
echo "### STEP 5: restart production (original bug-fixed config)"
kill_bench_server
start_prod

echo "=== master.sh COMPLETE $(date -u +%FT%TZ) ==="

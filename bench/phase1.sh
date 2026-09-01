#!/usr/bin/env bash
# Phase 1: llama-bench sweeps, running CONCURRENTLY with the production llama-server.
# Zero downtime. Results appended to bench/results/phase1.csv (one CSV block per group).
# Resume-safe: completed groups recorded in bench/done.txt.
set -u
cd "$(dirname "$0")/.."
BENCH=./llama-bench
MODEL=/root/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/f1bfb127c64f7072bdd2cad55f258b9c8b2910fe/Qwen3.8-27B-UD-Q4_K_XL.gguf
RES=bench/results
mkdir -p "$RES"
LOG=$RES/phase1.log
DONE=bench/done.txt
touch "$DONE"

is_done() { grep -qxF "$1" "$DONE"; }
mark_done() { echo "$1" >> "$DONE"; }

# GPU sampler: contention evidence (production server + bench sharing the L40S)
( while true; do
    sleep 15
    nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used,power.draw,clocks.sm --format=csv,noheader >> "$RES/gpu_log.csv" 2>/dev/null
  done ) &
GPU_PID=$!
trap 'kill $GPU_PID 2>/dev/null' EXIT

# ---------------------------------------------------------------- PP sweep
# FA x KV-quant x ubatch, prompts 512/2048/8192, no generation
for FA in on off; do
  for CT in f16 q8_0; do
    tag="pp_fa_${FA}_ct_${CT}"
    if is_done "$tag"; then echo "[skip] $tag (already done)" >> "$LOG"; continue; fi
    echo "[run] $tag $(date +%H:%M:%S)" >> "$LOG"
    $BENCH -m "$MODEL" \
      -p 512,2048,8192 -n 0 \
      -b 2048 -ub 128,256,512,1024,2048 \
      -fa "$FA" -ctk "$CT" -ctv "$CT" \
      -ngl 999 -r 3 --offline -o csv \
      >> "$RES/${tag}.csv" 2>> "$LOG"
    rc=$?
    echo "[done] $tag rc=$rc $(date +%H:%M:%S)" >> "$LOG"
    mark_done "$tag"
  done
done

# ---------------------------------------------------------------- TG sweep
# FA x KV-quant x depth, 256 generated tokens, default batch/ubatch (2048/512)
for FA in on off; do
  for CT in f16 q8_0; do
    tag="tg_fa_${FA}_ct_${CT}"
    if is_done "$tag"; then echo "[skip] $tag (already done)" >> "$LOG"; continue; fi
    echo "[run] $tag $(date +%H:%M:%S)" >> "$LOG"
    $BENCH -m "$MODEL" \
      -p 0 -n 256 \
      -b 2048 -ub 512 \
      -d 0,4096,16384 \
      -fa "$FA" -ctk "$CT" -ctv "$CT" \
      -ngl 999 -r 3 --offline -o csv \
      >> "$RES/${tag}.csv" 2>> "$LOG"
    rc=$?
    echo "[done] $tag rc=$rc $(date +%H:%M:%S)" >> "$LOG"
    mark_done "$tag"
  done
done

echo "[phase1] ALL GROUPS COMPLETE $(date +%H:%M:%S)" >> "$LOG"

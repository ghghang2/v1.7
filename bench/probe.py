#!/usr/bin/env python3
"""Benchmark probe client for llama-server.

Usage: probe.py PORT LABEL [JSON-spec]
Spec keys:
  filler_chars: int    - chars of filler prefix text (approx tokens = chars/4)
  prompt_text: str     - replaces filler (used with filler_chars ignored)
  max_tokens: int      - completion budget (default 256)
  n: int               - number of concurrent identical requests (default 1)
  stream: bool         - use streaming to measure TTFT (default false)
Prints one JSON line per request with timing metrics, then a summary line.
"""
import json
import sys
import time
import concurrent.futures as cf

import requests

BASE = f"http://127.0.0.1:{{port}}/v1"


def filler_text(n_chars: int) -> str:
    unit = "The quick brown fox jumps over the lazy dog near the lighthouse. "
    return (unit * (n_chars // len(unit) + 1))[:n_chars]


def run_one(port: int, spec: dict, idx: int) -> dict:
    url = BASE.format(port=port)
    text = spec.get("prompt_text") or filler_text(spec.get("filler_chars", 0))
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": text + "\n\nWrite a short summary (max 100 words) of the text above."},
        ],
        "max_tokens": spec.get("max_tokens", 256),
        "temperature": 0.0,
        "stream": bool(spec.get("stream", False)),
    }
    if payload["stream"]:
        payload["stream_options"] = {"include_usage": True}
    t0 = time.time()
    ttft = None
    n_out = 0
    n_in = None
    try:
        with requests.post(f"{url}/chat/completions", json=payload,
                           stream=payload["stream"], timeout=900) as r:
            r.raise_for_status()
            if payload["stream"]:
                for raw in r.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "ignore")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    if chunk.get("usage"):
                        n_in = chunk["usage"].get("prompt_tokens")
                        n_out = chunk["usage"].get("completion_tokens")
                    for ch in chunk.get("choices", []):
                        d = ch.get("delta", {})
                        c = d.get("content")
                        if c:
                            if ttft is None:
                                ttft = time.time() - t0
                            n_out += len(c)
            else:
                j = r.json()
                u = j.get("usage") or {}
                n_in = u.get("prompt_tokens")
                n_out = u.get("completion_tokens")
    except Exception as e:  # noqa: BLE001
        return {"idx": idx, "error": str(e), "wall_s": time.time() - t0}
    wall = time.time() - t0
    gen = max(wall - (ttft or 0.0), 1e-6)
    return {
        "idx": idx,
        "wall_s": round(wall, 3),
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "prompt_tokens": n_in,
        "completion_tokens": n_out,
        "tg_tok_s": round(n_out / gen, 2),
        "pp_tok_s": round(n_in / max(ttft or wall, 1e-6), 1) if n_in else None,
    }


def main() -> None:
    port = int(sys.argv[1])
    label = sys.argv[2]
    spec = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    n = int(spec.get("n", 1))
    results = []
    t0 = time.time()
    if n == 1:
        results.append(run_one(port, spec, 0))
    else:
        with cf.ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(lambda i: run_one(port, spec, i), range(n)))
    wall = time.time() - t0
    ok = [r for r in results if "error" not in r]
    tot_out = sum(r.get("completion_tokens") or 0 for r in ok)
    summary = {
        "label": label,
        "n": n,
        "wall_s": round(wall, 3),
        "total_completion_tokens": tot_out,
        "agg_tok_s": round(tot_out / wall, 2) if wall > 0 else None,
        "mean_tg_tok_s": round(sum(r["tg_tok_s"] for r in ok) / len(ok), 2) if ok else None,
        "mean_ttft_s": round(sum(r["ttft_s"] or 0 for r in ok) / len(ok), 3) if ok else None,
        "results": results,
    }
    print(json.dumps(summary))
    sys.stdout.flush()


if __name__ == "__main__":
    main()

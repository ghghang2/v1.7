#!/usr/bin/env python3
# ninfer-dash.py — real-time throughput dashboard for a ninfer-serve endpoint.
# Tails the engine's --request-log-jsonl file and serves a live web page and/or
# prints a rolling console summary. Shows the FULL engine picture:
#   * live decode/prefill tokens-per-second (from "throughput" events)
#   * complete scheduler queue state (running / waiting / prefilling / decode_ready
#     / materializing / capture_pending / terminal_pending)
#   * live request feed (starts, completions, errors, rejections)
#   * per-request latency + token counts + finish reasons
#   * server configuration (max-concurrency, pending limits, model id)
#
# Usage:
#   python3 ninfer-dash.py <request-log.jsonl> [port]
#   python3 ninfer-dash.py <request-log.jsonl> 8787
#   python3 ninfer-dash.py <request-log.jsonl> 8787 --tui
#
# The engine writes one JSON object per line per event. Tolerant of partial
# lines and schema drift: unknown events are counted, unknown fields ignored.

import json
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------
EV_START      = "request_start"
EV_FINISH     = "request_done"
EV_ERROR      = "request_error"
EV_REJECT     = "request_rejected"
EV_THROUGHPUT = "throughput"
EV_SERVER     = "server_start"

HISTORY_LEN = 600          # ~5 min of 1s-interval points
FEED_LEN    = 200          # keep the last N request feed items

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.server_info     = {}            # model, load time, weights id
        self.config          = {}            # parsed flags of interest
        self.last_throughput = None          # latest throughput event (raw-ish)
        self.history     = deque(maxlen=HISTORY_LEN)   # (t, decode_tps, prefill_tps, running, waiting)
        self.feed        = deque(maxlen=FEED_LEN)      # human-readable feed items (newest first)
        self.requests    = {}            # request label -> start time
        self.stats       = {
            "started": 0, "done": 0, "errors": 0, "rejected": 0, "unknown_events": 0,
            "error_messages": {},
        }
        self.latencies   = deque(maxlen=500)           # (id, wait, total, out, finish, requested)
        self.started_at  = time.time()
        self.lines_seen  = 0

    def feed_add(self, kind, text):
        self.feed.appendleft({"t": time.time(), "kind": kind, "text": text})


STATE = State()


def _req_label(e):
    r = e.get("request") or {}
    rid = r.get("request_id", r.get("id", "?"))
    mo  = r.get("requested_output_tokens", r.get("max_tokens", "?"))
    msgs = r.get("message_count", "?")
    return str(rid), mo, msgs


def _parse_server_start(e):
    argv = e.get("argv", [])
    cfg = {}
    it = iter(argv[1:])
    for a in it:
        if a.startswith("--") and "=" not in a:
            key = a[2:]
            nxt = next(it, None)
            if nxt is None or nxt.startswith("--"):
                cfg[key] = True
            else:
                cfg[key] = nxt
    art = e.get("artifact", {})
    STATE.server_info = {
        "model_id":   art.get("target", cfg.get("model-id", "?")),
        "load_secs":  art.get("load_seconds"),
        "weights_id": art.get("weights_id", ""),
    }
    STATE.config = {
        "model":             cfg.get("model-id", STATE.server_info["model_id"]),
        "max_concurrency":   cfg.get("max-concurrency", "?"),
        "pending_timeout_ms":cfg.get("pending-timeout-ms", "default"),
        "max_pending":       cfg.get("max-pending-requests", "default"),
        "max_context":       cfg.get("max-context", "?"),
        "kv_capacity":       cfg.get("kv-capacity", "?"),
        "kv_dtype":          cfg.get("kv-dtype", "?"),
        "spec":              cfg.get("spec", ""),
        "draft_tokens":      cfg.get("draft-tokens", ""),
        "device_slots":      cfg.get("device-state-slots", ""),
        "host_slots":        cfg.get("host-state-slots", ""),
    }
    STATE.feed_add("server",
                   "server_start: model=%s max-conc=%s pending-timeout=%sms max-pending=%s" % (
                       STATE.server_info["model_id"], cfg.get("max-concurrency"),
                       cfg.get("pending-timeout-ms", "default"),
                       cfg.get("max-pending-requests", "default")))


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------
def process_line(line: str):
    STATE.lines_seen += 1
    try:
        e = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return  # partial line mid-write
    ev = e.get("event", "")
    ts_ms = e.get("timestamp_unix_ms")
    now = time.time()

    if ev == EV_SERVER:
        _parse_server_start(e)
        return

    if ev == EV_THROUGHPUT:
        tps = e.get("throughput_tokens_per_second") or {}
        dec = float(tps.get("decode") or 0)
        pre = float(tps.get("prefill") or 0)
        sch = e.get("scheduler") or {}
        STATE.last_throughput = {
            "t": now, "ts_ms": ts_ms,
            "decode_tps": dec, "prefill_tps": pre,
            "scheduler": sch,
            "decode_batch": e.get("decode_batch") or {},
            "tokens": e.get("tokens") or {},
            "host_work": {
                "device_wait_seconds": (e.get("host_work") or {}).get("device_wait_seconds"),
                "elapsed_seconds":     (e.get("host_work") or {}).get("elapsed_seconds"),
            },
        }
        STATE.history.append((now, dec, pre,
                              int(sch.get("running") or 0), int(sch.get("waiting") or 0)))
        return

    if ev == EV_START:
        STATE.stats["started"] += 1
        rid, mo, msgs = _req_label(e)
        prep = (e.get("preparation_seconds") or {}).get("total")
        prep_s = " prep=%.1fms" % (prep * 1000) if isinstance(prep, (int, float)) else ""
        STATE.requests[rid] = now
        STATE.feed_add("start", "START #%s max_tokens=%s msgs=%s%s" % (rid, mo, msgs, prep_s))
        return

    if ev == EV_FINISH:
        STATE.stats["done"] += 1
        rid, mo, msgs = _req_label(e)
        res = e.get("result") or {}
        out = res.get("completion_tokens", 0)
        fin = res.get("finish_reason", "?")
        et = e.get("engine_timing") or {}
        wait = et.get("queue_wait_seconds")
        dur = e.get("duration_seconds")
        if dur is None:
            dur = now - STATE.requests.pop(rid, now)
        else:
            STATE.requests.pop(rid, None)
        wait_s = "%.1fs" % wait if isinstance(wait, (int, float)) else "?"
        STATE.latencies.appendleft((rid, wait_s, "%.1fs" % dur, out, fin, mo))
        STATE.feed_add("done", "DONE #%s out=%s fin=%s wait=%s total=%.1fs (req max_tokens=%s)"
                         % (rid, out, fin, wait_s, dur, mo))
        return

    if ev in (EV_ERROR, EV_REJECT):
        if ev == EV_ERROR:
            STATE.stats["errors"] += 1
        else:
            STATE.stats["rejected"] += 1
        rid, mo, msgs = _req_label(e)
        msg = (e.get("error") or {}).get("message", ev)
        STATE.stats["error_messages"][msg] = STATE.stats["error_messages"].get(msg, 0) + 1
        STATE.requests.pop(rid, None)
        STATE.feed_add("error", "%s #%s: %s" % ("ERROR" if ev == EV_ERROR else "REJECT", rid, msg))
        return

    # anything else: count + occasional note
    STATE.stats["unknown_events"] += 1
    if STATE.stats["unknown_events"] <= 5:
        STATE.feed_add("other", "event type '%s' (counted, not rendered)" % ev)


# ---------------------------------------------------------------------------
# File tailing (bounded, rotation-tolerant)
# ---------------------------------------------------------------------------
INGEST = {"pos": None}

def ingest_file(path: Path):
    try:
        st = path.stat()
    except OSError:
        return
    try:
        with path.open("rb") as f:
            pos = INGEST["pos"]
            if pos is None or pos > st.st_size:
                # fresh start or file rotation: attach at the tail so the
                # dashboard has immediate context without replaying GBs
                f.seek(max(0, st.st_size - 64 * 1024))
                replay = True
            else:
                f.seek(pos)
                replay = False
            chunk = f.read(8 * 1024 * 1024)  # bounded read per tick
            INGEST["pos"] = f.tell()
    except OSError:
        return
    for line in chunk.decode("utf-8", "replace").splitlines():
        process_line(line)
    if replay:
        STATE.feed_add("server", "dashboard attached (replayed tail of %d events)" %
                       max(0, STATE.lines_seen))


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0">
<title>ninfer-serve</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0d1117; color:#c9d1d9; font:14px/1.45 ui-monospace,Menlo,monospace; margin:0; padding:18px; }
  h1 { font-size:17px; margin:0 0 4px; color:#e6edf3; }
  .sub { color:#8b949e; font-size:12px; margin-bottom:14px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin-bottom:14px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 12px; }
  .card .k { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
  .card .v { font-size:21px; font-weight:700; margin-top:2px; }
  .card .s { color:#8b949e; font-size:11px; margin-top:2px; }
  .good { color:#3fb950; } .bad { color:#f85149; } .warn { color:#d29922; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width:900px){ .grid2 { grid-template-columns:1fr; } }
  .panel { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px; }
  .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:#8b949e; margin:0 0 8px; }
  canvas { width:100%; height:130px; display:block; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:3px 8px; border-bottom:1px solid #21262d; white-space:nowrap; }
  th { color:#8b949e; font-size:11px; text-transform:uppercase; }
  td.good { color:#3fb950; } td.warn { color:#d29922; }
  .feed { max-height:300px; overflow-y:auto; font-size:12.5px; }
  .feed div { padding:2px 0; border-bottom:1px solid #21262d; }
  .start { color:#79c0ff; } .done { color:#3fb950; } .error { color:#f85149; }
  .other { color:#8b949e; } .server { color:#d29922; }
  .cfg { font-size:12.5px; color:#8b949e; }
  .cfg b { color:#c9d1d9; font-weight:600; }
  .slot-row { display:flex; gap:4px; margin:6px 0 10px; flex-wrap:wrap; }
  .slot { width:26px; height:26px; border-radius:5px; background:#21262d; display:flex; align-items:center; justify-content:center; font-size:10px; color:#484f58; }
  .slot.on { background:#1f6feb; color:#fff; }
  .foot { color:#484f58; font-size:11px; margin-top:14px; }
</style></head>
<body>
<h1>ninfer-serve <span id="model"></span></h1>
<div class="sub" id="cfgsub"></div>

<div class="cards">
  <div class="card"><div class="k">Decode tok/s</div><div class="v good" id="dec">–</div><div class="s" id="decS"></div></div>
  <div class="card"><div class="k">Prefill tok/s</div><div class="v" id="pre">–</div><div class="s" id="preS"></div></div>
  <div class="card"><div class="k">Running / Waiting</div><div class="v" id="runwait">–</div><div class="s" id="runwaitS"></div></div>
  <div class="card"><div class="k">Done</div><div class="v" id="done">–</div><div class="s" id="doneS"></div></div>
  <div class="card"><div class="k">Errors / Rejected</div><div class="v" id="errs">–</div><div class="s" id="errsS"></div></div>
  <div class="card"><div class="k">Decode batch size</div><div class="v" id="batch">–</div><div class="s" id="batchS"></div></div>
</div>

<div class="panel" style="margin-bottom:10px;">
  <h2>Scheduler queue (full state) — slots lit = running</h2>
  <table id="sched"><tr>
    <th>running</th><th>waiting</th><th>prefilling</th><th>decode_ready</th>
    <th>materializing</th><th>capture_pending</th><th>terminal_pending</th>
  </tr><tr id="schedrow"></tr></table>
  <div class="slot-row" id="slots"></div>
</div>

<div class="grid2">
  <div class="panel"><h2>Throughput history (decode green, prefill amber)</h2>
    <canvas id="chart" height="130"></canvas></div>
  <div class="panel"><h2>Server configuration</h2><div class="cfg" id="cfg"></div>
    <h2 style="margin-top:12px">Error / reject messages (by count)</h2>
    <div class="feed" id="errmsgs"></div></div>
</div>

<div class="grid2" style="margin-top:10px">
  <div class="panel"><h2>Recent requests (id / queue wait / total / out tokens / finish / requested max_tokens)</h2>
    <table id="lat"></table></div>
  <div class="panel"><h2>Live feed</h2><div class="feed" id="feed"></div></div>
</div>

<div class="foot" id="foot"></div>
<script>
const $ = id => document.getElementById(id);
const fmt = n => (n==null) ? "–" : (n>=100 ? Math.round(n) : Number(n).toFixed(1));

function drawChart(hist){
  const c = $("chart"), ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = c.width = c.clientWidth * dpr;
  const h = c.height = 130 * dpr;
  ctx.clearRect(0,0,w,h);
  if (!hist.length) return;
  let max = 1;
  for (const p of hist) max = Math.max(max, p[1], p[2]);
  max *= 1.1;
  const n = hist.length;
  const x = i => i/Math.max(1,n-1) * w;
  const y = v => h - (v/max)*h;
  ctx.strokeStyle = "#21262d"; ctx.lineWidth = 1;
  for (const f of [0.25,0.5,0.75]) { ctx.beginPath(); ctx.moveTo(0, h*f); ctx.lineTo(w, h*f); ctx.stroke(); }
  const line = (idx, color) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    hist.forEach((p,i)=> i ? ctx.lineTo(x(i), y(p[idx])) : ctx.moveTo(x(0), y(p[idx])));
    ctx.stroke();
  };
  line(1, "#3fb950");   // decode
  line(2, "#d29922");   // prefill
  ctx.fillStyle = "#8b949e"; ctx.font = (11*dpr)+"px monospace";
  ctx.fillText("max " + fmt(max) + " tok/s", 6, 12*dpr);
}

function setSlotRow(sch, running){
  const row = $("schedrow");
  row.innerHTML = ["running","waiting","prefilling","decode_ready",
                  "materializing","capture_pending","terminal_pending"]
                  .map(k=>{
                    const v = sch[k]||0;
                    return `<td class="${v?(k==="running"?"good":(k==="waiting"?"warn":"")):""}">${v}</td>`;
                  }).join("");
  const mc = 8;  // filled below from config
  const slots = $("slots");
  const count = slots._mc || mc;
  if (slots.children.length !== count) {
    slots.innerHTML = "";
    for (let i=0;i<count;i++){ const d=document.createElement("div"); d.className="slot"; d.textContent=i+1; slots.appendChild(d); }
  }
  [...slots.children].forEach((el,i)=> el.className = "slot" + (i < running ? " on" : ""));
}

async function tick(){
  let d;
  try { d = await (await fetch("/events")).json(); } catch(e){ return; }
  const t = d.last_throughput || {};
  const s = t.scheduler || {};
  const running = s.running || 0;

  const c = d.config || {};
  const mc = parseInt(c.max_concurrency, 10);
  const slots = $("slots");
  if (mc > 0) slots._mc = mc;

  $("model").textContent = c.model || d.server_info.model_id || "";
  $("dec").textContent = fmt(t.decode_tps);
  const hw = t.host_work || {};
  $("decS").textContent = (hw.device_wait_seconds != null && hw.elapsed_seconds)
      ? ("device busy " + Math.round(100*hw.device_wait_seconds/hw.elapsed_seconds) + "% of interval") : "";
  $("pre").textContent = fmt(t.prefill_tps);
  const tok = t.tokens || {};
  $("preS").textContent = tok.computed_prefill != null ? (tok.computed_prefill + " tok/interval") : "";
  $("runwait").textContent = running + " / " + (s.waiting||0);
  $("runwaitS").textContent = "of max-concurrency " + (c.max_concurrency ?? "–");
  $("done").textContent = d.stats.done;
  $("doneS").textContent = "of " + d.stats.started + " started";
  $("errs").textContent = (d.stats.errors) + " / " + (d.stats.rejected);
  $("errs").className = "v " + (d.stats.errors ? "bad" : "good");
  const errsTop = Object.entries(d.stats.error_messages||{}).sort((a,b)=>b[1]-a[1])[0];
  $("errsS").textContent = errsTop ? (errsTop[1]+"× " + errsTop[0].slice(0,42)) : "";
  const b = t.decode_batch || {};
  $("batch").textContent = b.average_size != null ? fmt(b.average_size) : "–";
  $("batchS").textContent = b.rounds ? (b.rounds + " rounds/interval") : "";

  setSlotRow(s, running);

  const hist = d.history || [];
  drawChart(hist);

  $("cfg").innerHTML =
    "<b>model</b> " + (c.model||"–") +
    "<br><b>max-concurrency</b> " + (c.max_concurrency||"–") +
    " &nbsp; <b>max-context</b> " + (c.max_context||"–") +
    " &nbsp; <b>kv-capacity</b> " + (c.kv_capacity||"–") +
    "<br><b>kv-dtype</b> " + (c.kv_dtype||"–") +
    " &nbsp; <b>spec</b> " + (c.spec||"–") + " (draft " + (c.draft_tokens||"–") + ")" +
    "<br><b>state slots</b> device " + (c.device_slots||"–") + " / host " + (c.host_slots||"–") +
    "<br><b>pending-timeout-ms</b> " + (c.pending_timeout_ms||"–") +
    " &nbsp; <b>max-pending-requests</b> " + (c.max_pending||"–");
  $("cfgsub").textContent =
    "weights " + (d.server_info.weights_id||"") +
    (d.server_info.load_secs ? " (load " + d.server_info.load_secs.toFixed(1) + "s)" : "") +
    " · log lines ingested " + d.lines_seen +
    " · dashboard attached " + Math.round((Date.now()/1000) - d.started_at) + "s ago";

  const em = Object.entries(d.stats.error_messages||{}).sort((a,b)=>b[1]-a[1]);
  $("errmsgs").innerHTML = em.length
    ? em.map(([m,n])=>`<div class="error">${n}× ${m}</div>`).join("")
    : '<div class="other">none</div>';

  $("lat").innerHTML = '<tr><th>id</th><th>wait</th><th>total</th><th>out</th><th>finish</th><th>req</th></tr>' +
    (d.latencies||[]).slice(0,15).map(r=>
      `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td><td>${r[4]}</td><td>${r[5]}</td></tr>`).join("");

  $("feed").innerHTML = (d.feed||[]).slice(0,60).map(f=>`<div class="${f.kind}">${f.text}</div>`).join("");

  $("foot").textContent = "engine ts " + (t.ts_ms ? new Date(t.ts_ms).toLocaleTimeString() : "–") +
    " · last throughput sample " + (t.t ? Math.round((Date.now()/1000 - t.t)) + "s ago" : "–");
}
tick(); setInterval(tick, 1500);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/events"):
            ingest_file(LOG_PATH)
            payload = {
                "started_at": STATE.started_at,
                "lines_seen": STATE.lines_seen,
                "server_info": STATE.server_info,
                "config": STATE.config,
                "stats": STATE.stats,
                "last_throughput": STATE.last_throughput,
                "history": list(STATE.history),
                "feed": list(STATE.feed)[:120],
                "latencies": list(STATE.latencies)[:40],
            }
            self._send(200, "application/json", json.dumps(payload).encode())
        else:
            self._send(200, "text/html; charset=utf-8", PAGE.encode())

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------
# Console mode (--tui): no HTTP server, just a live terminal line
# ---------------------------------------------------------------------------
def console_loop(path: Path):
    last = 0.0
    while True:
        ingest_file(path)
        now = time.time()
        if now - last >= 2.0:
            last = now
            s = STATE.last_throughput or {}
            sch = s.get("scheduler") or {}
            st = STATE.stats
            sys.stdout.write(
                "\r[ninfer] dec=%8.1f pre=%8.1f tok/s | run=%d wait=%d pf=%d ready=%d | "
                "done=%d err=%d rej=%d   " % (
                    s.get("decode_tps") or 0, s.get("prefill_tps") or 0,
                    sch.get("running", 0), sch.get("waiting", 0),
                    sch.get("prefilling", 0), sch.get("decode_ready", 0),
                    st["done"], st["errors"], st["rejected"]))
            sys.stdout.flush()
        time.sleep(0.25)


# ---------------------------------------------------------------------------
LOG_PATH = Path("/ninfer-request-log-jsonl.log")

def main():
    global LOG_PATH
    args = [a for a in sys.argv[1:] if a != "--tui"]
    tui = "--tui" in sys.argv[1:]
    if args:
        LOG_PATH = Path(args[0])
    port = int(args[1]) if len(args) > 1 else 8787

    if tui:
        print("[ninfer-dash] console mode, log=" + str(LOG_PATH))
        console_loop(LOG_PATH)
    else:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        print(f"[ninfer-dash] serving http://0.0.0.0:{port}  log={LOG_PATH}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run.py – Start llama-server and provide simple status/stop helpers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from nbchat.core import config

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
SERVICE_INFO = Path(config.SERVICE_INFO_PATH)
LLAMA_LOG    = Path(config.LLAMA_LOG_PATH)
# RELEASE_REPO = f"{config.USER_NAME}/llamacpp_g4"
RELEASE_REPO = f"{config.USER_NAME}/llamacpp_5090"
MODEL        = config.MODEL_NAME
PORT         = config.PORT
N_PARALLEL   = config.N_PARALLEL
CTX_SIZE     = config.CTX_SIZE
N_GPU_LAYERS = config.N_GPU_LAYERS
WA_PORT      = os.environ.get("WA_PORT", "8764")
WA_ALLOW     = os.environ.get("WA_ALLOW", "")
REPO_ROOT    = Path(__file__).resolve().parent
CHANNELS_DIR = REPO_ROOT / "nbchat" / "channels"

# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _run_blocking(cmd: str, *, extra_env: dict | None = None) -> None:
    """Standard blocking run for setup tasks."""
    env = {**os.environ, **(extra_env or {})}
    subprocess.run(cmd, shell=True, env=env, check=True)


def _run_detached(cmd: str | list, log_path: Path, label: str, extra_env: dict | None = None) -> int:
    """
    Launches a command fully detached from the parent process.
    Returns the PID of the started process.
    """
    env = {**os.environ, **(extra_env or {})}
    log_file = log_path.open("w", encoding="utf-8", buffering=1)
    
    # start_new_session=True makes it the leader of a new process group
    # close_fds=True ensures the notebook doesn't hang on open pipes
    with open(os.devnull, "r") as devnull:
        p = subprocess.Popen(
            cmd,
            shell=isinstance(cmd, str),
            stdin=devnull,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env
        )
    
    log_file.close()
    print(f"[run] {label} started (PID: {p.pid})")
    return p.pid


def _is_port_free(port: int) -> bool:
    result = subprocess.run(["ss", "-tuln"], capture_output=True, text=True)
    return str(port) not in result.stdout


def _wait_for(url: str, *, timeout: int = 360, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _save_service_info(pids: dict[str, int]) -> None:
    SERVICE_INFO.write_text(json.dumps({
        "pids": pids,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))


def _load_service_info() -> dict:
    if not SERVICE_INFO.exists():
        raise FileNotFoundError("No service_info.json found – services likely not running.")
    return json.loads(SERVICE_INFO.read_text())


def _kill_pid(name: str, pid: int) -> None:
    if psutil is None:
        print(f"psutil missing – cannot gracefully kill {name} (PID {pid})")
        return
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            child.terminate()
        proc.terminate()
        print(f"✓ {name} (PID {pid}) stopped")
    except psutil.NoSuchProcess:
        print(f"! {name} (PID {pid}) was already dead")
    except Exception as exc:
        print(f"! Error stopping {name}: {exc}")

# --------------------------------------------------------------------------- #
#  Model download monitoring (first-run visibility)
#
#  On a first run, ``llama-server -hf MODEL`` downloads the model from Hugging
#  Face using its own (C++) downloader.  That progress is written to the log
#  file and is otherwise invisible, so a slow or stalled download looks like a
#  hang.  We therefore watch the Hugging Face cache directory that llama.cpp
#  writes into and render a live progress bar (percent, speed and ETA) until
#  the model file is complete.
# --------------------------------------------------------------------------- #

_HF_API_BASE = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
_BAR_WIDTH = 30
_DL_STALL_WARN_S = 120          # warn when the file stops growing this long
_DL_STALL_FAIL_S = 900          # abort when it stays stuck this long
_DL_TOTAL_TIMEOUT_S = 6 * 3600  # hard cap for the whole download
_DL_STABLE_DONE_S = 10          # a large file stable this long is treated as done
_DL_MIN_DONE_SIZE = 1 << 30     # >= 1 GiB before the "stable = done" fallback applies


def _human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0 or unit == "TiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _fmt_eta(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hf_repo_id(model: str) -> str:
    """HF repo id (``org/name``) from a ``-hf`` value such as ``org/name:quant``."""
    return model.split(":", 1)[0]


def _hf_quant(model: str) -> str:
    parts = model.split(":", 1)
    return parts[1] if len(parts) == 2 else ""


def _hf_blobs_dir(model: str) -> Path:
    """Path to the Hugging Face ``blobs`` dir that llama.cpp downloads into."""
    hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not hub:
        home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        hub = str(Path(home) / "hub")
    repo_dir = "models--" + _hf_repo_id(model).replace("/", "--")
    return Path(hub) / repo_dir / "blobs"


def _hf_repo_gguf_files(model: str) -> list:
    """Best-effort list of GGUF files in the repo: ``[{oid, name, size}, ...]``.

    Returns an empty list on any network/API failure so the caller can fall
    back to inferring progress from the log.
    """
    repo = _hf_repo_id(model)
    url = f"{_HF_API_BASE}/api/models/{repo}/tree/main"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "run.py/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    files = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("path", "")
        if not name.lower().endswith(".gguf"):
            continue
        lfs = item.get("lfs") or {}
        oid = lfs.get("oid") or item.get("oid") or ""
        size = lfs.get("size") or item.get("size") or 0
        if oid:
            files.append({"oid": oid, "name": name, "size": int(size)})
    return files


def _select_primary_file(files: list, quant: str):
    """Pick the GGUF file llama.cpp will fetch for the given quant (best effort)."""
    if not files:
        return None
    q = quant.strip().lower()
    if q:
        matches = [f for f in files if q in f["name"].lower()]
        if matches:
            return max(matches, key=lambda f: f["size"])
    return max(files, key=lambda f: f["size"])


def _largest_blob_size(blobs_dir: Path) -> int:
    """Largest file currently in the blobs dir (0 if none / missing)."""
    try:
        if not blobs_dir.exists():
            return 0
        best = 0
        for p in blobs_dir.iterdir():
            if p.is_file():
                s = p.stat().st_size
                if s > best:
                    best = s
        return best
    except Exception:
        return 0


def _read_log_tail(log_path: Path, nbytes: int) -> str:
    try:
        if not log_path.exists():
            return ""
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _latest_download_progress(log_path: Path):
    """Parse the newest llama.cpp progress line. Returns ``(pct, eta_str)`` or ``(None, None)``."""
    data = _read_log_tail(log_path, 65536)
    matches = re.finditer(
        r"(\d+\.\d+)%\s+t=(\d+):(\d+):(\d+)\s+ETA=(\d+):(\d+):(\d+)", data
    )
    last = None
    for m in matches:
        last = m
    if last is None:
        return None, None
    pct = float(last.group(1))
    eta = f"{int(last.group(5)):02d}:{last.group(6)}:{last.group(7)}"
    return pct, eta


def _log_indicates_loaded(log_path: Path) -> bool:
    data = _read_log_tail(log_path, 262144)
    return ("model loaded" in data) or ("listening on" in data)


def _log_indicates_download_failed(log_path: Path) -> bool:
    data = _read_log_tail(log_path, 262144)
    return "download failed" in data


def _render_bar(label: str, pct: float, size_str: str, speed_str: str, eta_str: str) -> None:
    frac = max(0.0, min(1.0, pct / 100.0))
    filled = int(round(frac * _BAR_WIDTH))
    bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
    line = (
        f"\r[dl] {label[:24]:24} {pct:5.1f}% "
        f"[{bar}] {size_str:<16} {speed_str:>10} ETA {eta_str:<8}"
    )
    print(line, end="", flush=True)


def _ensure_model_ready(model: str, log_path: Path) -> bool:
    """
    Wait for the model file to be present in the Hugging Face cache, showing a
    live progress bar (percent / speed / ETA) while llama.cpp downloads it.

    Returns True once the file is complete (or already cached) and False on a
    failure / stall / the server process dying.
    """
    blobs_dir = _hf_blobs_dir(model)
    primary = _select_primary_file(_hf_repo_gguf_files(model), _hf_quant(model))
    primary_oid = primary["oid"] if primary else None
    primary_size = primary["size"] if primary else 0
    label = (primary["name"] if primary else _hf_repo_id(model))[:24]

    # Already fully cached? Nothing to show.
    if primary_oid and _largest_blob_size(blobs_dir) >= primary_size and primary_size > 0:
        print(f"[run] Model already cached ({label}) -- no download needed.")
        return True

    print(f"[run] Downloading model '{label}' from Hugging Face ...")

    start = time.monotonic()
    last_growth_t = start
    first_byte = False
    last_size = 0
    last_speed = 0.0
    speed_ema = 0.0
    stall_warned = False
    total = 0

    while True:
        now = time.monotonic()
        elapsed = now - start

        # Hard cap on the whole download.
        if elapsed > _DL_TOTAL_TIMEOUT_S:
            print(f"\n[dl] Giving up after {int(elapsed // 60)} min (no completion detected).")
            return False

        size = _largest_blob_size(blobs_dir)

        # Track growth -> download speed (smoothed) and stall clock.
        if size > 0 and not first_byte:
            first_byte = True
            last_growth_t = now
        if size > last_size:
            dt = now - last_growth_t if (now - last_growth_t) > 0 else 1.0
            inst = (size - last_size) / dt
            speed_ema = inst if speed_ema == 0.0 else (0.8 * speed_ema + 0.2 * inst)
            last_growth_t = now
            stall_warned = False
        last_size = size

        log_pct, log_eta = _latest_download_progress(log_path)

        # Resolve total size: known from API, else estimate from log percent.
        if primary_size > 0:
            total = primary_size
            pct = 100.0 * size / total if total else 0.0
            eta = (total - size) / speed_ema if speed_ema > 0 else None
            eta_str = _fmt_eta(eta)
        elif log_pct and log_pct > 0 and size > 0:
            total = size / (log_pct / 100.0)
            pct = log_pct
            eta_str = log_eta
        else:
            total = 0
            pct = 0.0
            eta_str = log_eta or "--"

        speed_str = f"{_human_bytes(speed_ema)}/s" if speed_ema > 0 else "  --  "
        size_str = (
            f"{_human_bytes(size)}/{_human_bytes(total)}"
            if total
            else f"{_human_bytes(size)} (size?)"
        )
        _render_bar(label, pct, size_str, speed_str, eta_str)

        # Completion / failure signals.
        if _log_indicates_loaded(log_path):
            print(f"\n[dl] Model ready: {label}")
            return True
        if primary_oid and primary_size and size >= primary_size:
            print(f"\n[dl] Download complete: {label} ({_human_bytes(size)})")
            return True
        if _log_indicates_download_failed(log_path):
            print(f"\n[dl] llama-server reported a download failure -- see {log_path}")
            return False
        # llama.cpp reports ~100% (also covers the API-unreachable case).
        if log_pct is not None and log_pct >= 99.9 and size > 0:
            print(f"\n[dl] Download complete: {label} ({_human_bytes(size)})")
            return True
        # Fallback: a large file that has stopped growing is treated as done.
        if (not primary_size) and first_byte and size >= _DL_MIN_DONE_SIZE \
                and (now - last_growth_t) >= _DL_STABLE_DONE_S:
            print(f"\n[dl] Download complete: {label} ({_human_bytes(size)})")
            return True

        # Stall detection.
        if first_byte:
            idle = now - last_growth_t
            if idle > _DL_STALL_FAIL_S:
                print(f"\n[dl] Download stuck for {int(idle // 60)} min at {_human_bytes(size)} -- aborting.")
                return False
            if idle > _DL_STALL_WARN_S and not stall_warned:
                print(f"\n[dl] ... download appears stalled (no growth for {int(idle)}s)")
                stall_warned = True

        time.sleep(0.5)

# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #

def main() -> None:
    if not os.getenv("GITHUB_TOKEN"):
        sys.exit("[ERROR] GITHUB_TOKEN must be set")

    if not _is_port_free(PORT):
        sys.exit(f"[ERROR] Port {PORT} is already in use")

    # 1. Binary Setup
    _run_blocking(
        f"gh release download --repo {RELEASE_REPO} --pattern llama-server --skip-existing",
        extra_env={"GITHUB_TOKEN": os.environ["GITHUB_TOKEN"]},
    )
    _run_blocking("chmod +x ./llama-server")
#    _run_blocking("chmod +x ./llama-bench")

    pids = {}

    llama_cmd = [ ## 5090 tuned (r/LocalLLaMA Qwen3.8-27B consensus + bench/THROUGHPUT_FINDINGS.md)
        "./llama-server",
        "-hf", MODEL,
        "--port", str(PORT),
        "--parallel", str(N_PARALLEL),
        "--ctx-size", str(CTX_SIZE),
        "--n-gpu-layers", str(N_GPU_LAYERS),
        "--context-shift",
        "--flash-attn", "on",
        "--cache-type-k", "q8_0",
        "--cache-type-v", "q8_0",
        "--cache-ram", "-1",
        "--batch-size", "4096",
        "--ubatch-size", "4096",
        "--spec-default",
        "--spec-type", "draft-mtp",
        "--spec-draft-n-max", "1",
        "--reasoning", "on",
        "--reasoning-preserve",
        "--chat-template-kwargs", '{"reasoning_effort": "medium"}',
        "--reasoning-budget", "4096",
        "--reasoning-budget-message", "... I am thinking for too -- let me gather more info about the task.",
        "--no-mmproj", ## disable vision
        "--temp", ".65", 
        "--top-p", "0.95",
        "--top-k", "20",
        "--min-p", "0.05",
        "--repeat-penalty", "1.0",
        "--poll", "100",
        "--fit", "off",
        "--agent",
        "--load-mode", "mmap",   # was "mlock" -> RLIMIT_MEMLOCK failure
        "--metrics",
    ]
    pids["llama"] = _run_detached(llama_cmd, LLAMA_LOG, "llama-server")

    # 2. First-run model download: show live progress (percent / speed / ETA)
    if not _ensure_model_ready(MODEL, LLAMA_LOG):
        stop()
        sys.exit(f"[ERROR] model download failed or stalled -- see {LLAMA_LOG}")

    # # 3. Start WhatsApp Python Server
    # wa_py_cmd = f"python -m nbchat.channels.whatsapp_server"
    # pids["whatsapp_python"] = _run_detached(
    #     wa_py_cmd, 
    #     REPO_ROOT / "whatsapp_server.log", 
    #     "WhatsApp Python Server",
    #     extra_env={"WA_PORT": WA_PORT}
    # )
    # time.sleep(2) # Let FastAPI bind

    # # 4. Start WhatsApp Bridge (Node)
    # bridge_path = CHANNELS_DIR / "whatsapp_bridge.js"
    # if not bridge_path.exists():
    #     sys.exit(f"[ERROR] Bridge script not found: {bridge_path}")
    
    # wa_node_cmd = f"node {bridge_path}"
    # pids["whatsapp_bridge"] = _run_detached(
    #     wa_node_cmd, 
    #     REPO_ROOT / "whatsapp_bridge.log", 
    #     "WhatsApp Node Bridge",
    #     extra_env={"WA_PORT": WA_PORT, "WA_ALLOW": WA_ALLOW}
    # )

    # 5. Environment Setup
    print("Installing remaining dependencies...")
    _run_blocking("pip install -r requirements.txt -qqq")
    _run_blocking("apt install -y libxcomposite1 libgtk-3-0 libatk1.0-0")
    _run_blocking("playwright install --with-deps chromium")

    # 6. Final health check
    print("Waiting for llama-server health check...")
    if not _wait_for(f"http://localhost:{PORT}/health"):
        stop() # Cleanup what we started
        sys.exit("[ERROR] llama-server failed to start within timeout")

    _save_service_info(pids)
    print("\n" + "="*60)
    print("ALL SERVICES RUNNING SUCCESSFULLY!")
    print(f"WhatsApp QR: tail -f whatsapp_bridge.log")
    print("="*60)
    
    os._exit(0)


def status() -> None:
    try:
        info = _load_service_info()
    except FileNotFoundError as exc:
        print(exc)
        return

    print("=" * 60)
    print(f"Started at : {info['started_at']}")
    for name, pid in info["pids"].items():
        alive = psutil.pid_exists(pid) if psutil else "Unknown (psutil missing)"
        status_str = "RUNNING" if alive is True else "STOPPED"
        print(f"{name:16} (PID {pid}): {status_str}")
    print("=" * 60)


def stop() -> None:
    try:
        info = _load_service_info()
    except FileNotFoundError:
        print("No active services found in service_info.json")
        return

    print("Shutting down all services...")
    for name, pid in info["pids"].items():
        _kill_pid(name, pid)
    
    SERVICE_INFO.unlink(missing_ok=True)
    print("Cleanup complete.")


if __name__ == "__main__":
    commands = {"--status": status, "--stop": stop}
    if len(sys.argv) == 1:
        main()
    elif sys.argv[1] in commands:
        commands[sys.argv[1]]()
    else:
        print(f"Unknown command: {sys.argv[1]}")
        print("Usage: python run.py [--status | --stop]")
        sys.exit(1)
        

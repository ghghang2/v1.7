"""Optional external control socket for TUI v2/v3 (tui3 wave 6).

A small, defensive JSON-over-Unix-socket server that lets an external
process (or the :mod:`nbchat.tui2.ctl` CLI) drive a running TUI:

    $ python -m nbchat.tui2.ctl status
    $ python -m nbchat.tui2.ctl theme light
    $ python -m nbchat.tui2.ctl send "hello"
    $ python -m nbchat.tui2.ctl quit

Protocol: one newline-terminated JSON object per message.  Request
``{"cmd": "<name>", "arg": "<optional>"}``; response
``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.

Design notes (kept deliberately low-risk):

- Read-only commands (``status``, ``sessions``) are answered directly on
  the socket thread (safe: scalar/atomic reads + read-only db queries).
- Mutating commands (``theme``, ``send``, ``quit``) enqueue a closure onto
  the UI thread via the app's event queue (``events.put("call", fn)``) and
  reply with an immediate ``{"ok": true, "queued": true}`` ack; the client
  can poll ``status`` to observe the effect.
- The server is a daemon thread; if it dies the TUI keeps running.  Every
  socket operation is wrapped so a control-client problem can never block
  or crash the TUI.  Disable with ``NBCHAT_NO_CTL=1``; override the socket
  path with ``NBCHAT_CTL_SOCKET``.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Callable, Dict, List, Optional

DEFAULT_SOCKET = os.path.join(
    os.path.expanduser("~"), ".nbchat", "tui2-ctl.sock")


def socket_path() -> str:
    return os.environ.get("NBCHAT_CTL_SOCKET") or DEFAULT_SOCKET


def enabled() -> bool:
    """Whether the control socket should start (default: on)."""
    return os.environ.get("NBCHAT_NO_CTL", "") not in ("1", "true", "yes")


class ControlServer:
    """A newline-delimited-JSON control socket for a running TUI."""

    def __init__(self, path: str, dispatch: Callable, status_fn: Callable,
                 sessions_fn: Callable,
                 theme_fn: Optional[Callable] = None,
                 send_fn: Optional[Callable] = None,
                 quit_fn: Optional[Callable] = None,
                 result_fn: Optional[Callable] = None) -> None:
        self.path = path
        self.dispatch = dispatch          # fn(closure) -> enqueue on UI thread
        self.status_fn = status_fn        # fn() -> dict   (read-only)
        self.sessions_fn = sessions_fn    # fn() -> list   (read-only)
        self.result_fn = result_fn        # fn() -> dict   (read-only)
        self.theme_fn = theme_fn          # fn(name) on UI thread
        self.send_fn = send_fn            # fn(text) on UI thread
        self.quit_fn = quit_fn            # fn() on UI thread
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> bool:
        """Bind + listen.  Returns True on success (best-effort)."""
        if self._thread is not None:
            return True
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(self.path)
            self._server.listen(5)
            self._server.settimeout(0.5)
            self._running = True
            self._thread = threading.Thread(
                target=self._accept_loop, daemon=True, name="ctl-server")
            self._thread.start()
            return True
        except Exception:
            self._running = False
            self._server = None
            return False

    def stop(self) -> None:
        self._running = False
        try:
            if self._server is not None:
                self._server.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass

    # -- accept loop ---------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._server is not None
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True, name="ctl-conn")
            t.start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            req = json.loads(data.split(b"\n", 1)[0])
            resp = self._process(req)
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception:
            try:
                conn.sendall(b'{"ok": false, "error": "bad request"}\n')
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- command dispatch ----------------------------------------------
    def _process(self, req: Dict) -> Dict:
        cmd = str(req.get("cmd", "")).strip().lower()
        arg = req.get("arg", "")
        try:
            if cmd == "status":
                return {"ok": True, "data": self.status_fn()}
            if cmd == "sessions":
                return {"ok": True, "data": self.sessions_fn()}
            if cmd == "result":
                if self.result_fn is None:
                    return {"ok": False, "error": "result unsupported"}
                return {"ok": True, "data": self.result_fn()}
            if cmd == "theme":
                if self.theme_fn is None:
                    return {"ok": False, "error": "theme unsupported"}
                self.dispatch(lambda n=arg: self.theme_fn(n))
                return {"ok": True, "queued": True, "cmd": "theme"}
            if cmd == "send":
                if self.send_fn is None:
                    return {"ok": False, "error": "send unsupported"}
                self.dispatch(lambda t=arg: self.send_fn(t))
                return {"ok": True, "queued": True, "cmd": "send"}
            if cmd == "quit":
                if self.quit_fn is None:
                    return {"ok": False, "error": "quit unsupported"}
                self.dispatch(lambda: self.quit_fn())
                return {"ok": True, "queued": True, "cmd": "quit"}
            return {"ok": False, "error": f"unknown command: {cmd}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── client / CLI ─────────────────────────────────────────────────────

def call(path: str, cmd: str, arg: str = "", timeout: float = 5.0) -> Dict:
    """Send one command to a running TUI and return the parsed response."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall((json.dumps({"cmd": cmd, "arg": arg}) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b"\n", 1)[0])
    finally:
        s.close()




def bg(bg_args: Optional[List[str]] = None, timeout: float = 12.0) -> Dict:
    """Launch a detached background (headless) TUI and return a summary.

    The new process runs with ``--bg`` (a stdin EOF does not end it) in its
    own session (``start_new_session=True``), so it survives the launcher
    exiting — the "detach" half of the background-agent workflow.  Its
    stdout/stderr go to a log file and its control socket uses a dedicated
    background path so it never collides with an interactive TUI.  A
    trailing prompt (anything after ``--session <id>``) is submitted as the
    first task once the socket is up.

    ``bg_args`` is the raw CLI tail, e.g. ``["--session", "tui:x", "hello"]``.
    """
    import subprocess
    import sys
    import time

    bg_args = list(bg_args if bg_args is not None else [])
    session: Optional[str] = None
    prompt: List[str] = []
    i = 0
    while i < len(bg_args):
        if bg_args[i] == "--session" and i + 1 < len(bg_args):
            session = bg_args[i + 1]
            i += 2
        else:
            prompt.append(bg_args[i])
            i += 1

    base = os.path.join(os.path.expanduser("~"), ".nbchat")
    os.makedirs(base, exist_ok=True)
    sock_path = os.path.join(base, "tui2-bg.sock")
    log_path = os.path.join(base, "tui2-bg.log")

    env = dict(os.environ)
    env["NBCHAT_CTL_SOCKET"] = sock_path
    env.pop("NBCHAT_NO_CTL", None)

    cmd = [sys.executable, "-m", "nbchat.tui2", "--bg"]
    if session:
        cmd += ["--session", session]

    logf = open(log_path, "ab", buffering=0)
    devnull = open(os.devnull, "rb")
    try:
        proc = subprocess.Popen(
            cmd, env=env, stdout=logf, stderr=subprocess.STDOUT,
            stdin=devnull, start_new_session=True, cwd=os.getcwd())
    except Exception:
        logf.close()
        devnull.close()
        raise
    finally:
        logf.close()
        devnull.close()

    # Wait for the control socket to appear (or the process to die).
    up = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(sock_path):
            up = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    summary: Dict = {
        "ok": up,
        "pid": proc.pid,
        "socket": sock_path,
        "log": log_path,
        "up": up,
        "exit_code": proc.poll(),
    }
    if prompt:
        text = " ".join(prompt)
        if up:
            try:
                summary["sent"] = call(sock_path, "send", text)
            except Exception as exc:
                summary["sent"] = {"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}
        else:
            summary["sent"] = {"ok": False,
                               "error": "background TUI did not come up"}
    return summary

def main(argv: Optional[List[str]] = None) -> int:
    """``nbchat-ctl <cmd> [arg]`` — talk to a running TUI's control socket."""
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: python -m nbchat.tui2.ctl <status|sessions|result|theme|send|quit> [arg]")
        print("       python -m nbchat.tui2.ctl bg [--session ID] [initial prompt...]")
        return 0 if argv else 2
    cmd = argv[0].lower()
    arg = argv[1] if len(argv) > 1 else ""
    if cmd == "bg":
        # bg launches a *new* background TUI; no existing socket required.
        resp = bg(argv[1:])
        print(json.dumps(resp, indent=2, sort_keys=True))
        return 0 if resp.get("ok") else 1
    path = socket_path()
    if not os.path.exists(path):
        print(f"no running TUI found at {path} (is NBCHAT_NO_CTL set?)")
        return 1
    try:
        resp = call(path, cmd, arg)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(resp, indent=2, sort_keys=True))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

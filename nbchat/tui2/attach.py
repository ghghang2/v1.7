"""``--attach <socket>`` — reattach view: tail a running TUI conversation.

A small, read-only "tail -f" of a running (typically ``--bg``) TUI.  It
connects to the TUI's control socket and periodically queries the ``log``
command, printing new messages as they appear.  It never mutates the TUI
(a pure read path).  This is the "reattach" half of the background-agent
workflow: launch ``nbchat-ctl bg`` (or a ``--bg`` TUI), then ``--attach``
to a dedicated socket to watch its output live.

Additive / safe:
  * A brand-new entry point (``python -m nbchat.tui2 --attach <socket>``);
    the interactive TUI is untouched.
  * Read-only: it only issues the ``log`` command (a DB read on the TUI
    side); a socket error just ends the tail with a message.
  * ``--interval <s>`` sets the poll cadence (default 1.0 s).
"""
from __future__ import annotations

import argparse
import sys
import time


def _role_prefix(role: str) -> str:
    return {"user": "you", "assistant": "nbchat",
            "system": "system"}.get(role, role)


def run(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # Strip the --attach flag and --v2; keep the socket path (and any other
    # args) for the argparse below.
    parsed: list[str] = []
    for a in argv:
        if a in ("--attach", "-a", "--v2"):
            continue
        parsed.append(a)
    ap = argparse.ArgumentParser(prog="nbchat-attach",
                                 description="tail a running TUI conversation")
    ap.add_argument("socket", nargs="?", default=None,
                    help="control-socket path (default: the standard path)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="poll interval in seconds (default 1.0)")
    ap.add_argument("--frame", action="store_true",
                    help="render the full frame (log + editor + status) live, "
                         "instead of tailing the conversation")
    ns = ap.parse_args(parsed)
    from . import ctl
    path = ns.socket if ns.socket else ctl.socket_path()
    # The initial probe uses the `frame` command in --frame mode and `log`
    # otherwise; both are read-only.
    probe_cmd = "frame" if ns.frame else "log"
    try:
        resp = ctl.call(path, probe_cmd, timeout=5.0)
    except Exception as exc:
        print(f"cannot connect to {path}: {type(exc).__name__}: {exc}")
        print("(is the TUI running with NBCHAT_CTL_SOCKET pointed there?)")
        return 1
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}")
        return 1
    data = resp.get("data", {})
    if ns.frame:
        print(f"# attached to {path} (frame mode); Ctrl+C to detach")
    else:
        sid = data.get("session", "")
        print(f"# attached to {path} (session {sid}); Ctrl+C to detach")
    sys.stdout.flush()
    if ns.frame:
        # Full-frame reattach view: clear the screen and redraw the frame
        # (log + editor + status) on every poll.  A simple "mirror" of the
        # remote TUI (no mouse / keys forwarded; use `send` for input).
        try:
            while True:
                time.sleep(max(0.1, ns.interval))
                resp = ctl.call(path, "frame", timeout=5.0)
                if not resp.get("ok"):
                    print(f"\n[detached: {resp.get('error')}]")
                    return 1
                lines = resp.get("data", {}).get("lines", [])
                # Clear + redraw (CSI 2J + cursor home); no flicker guard
                # needed for a remote mirror (the local terminal redraws
                # from scratch each poll).
                sys.stdout.write("\033[2J\033[H")
                for ln in lines:
                    sys.stdout.write(ln + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            sys.stdout.write("\033[?1049l\033[0m")
            sys.stdout.flush()
            return 0
        except Exception as exc:
            print(f"\n[detached: {type(exc).__name__}: {exc}]")
            return 1
    seen = 0
    last_count = len(data.get("messages", []))
    try:
        while True:
            time.sleep(max(0.1, ns.interval))
            resp = ctl.call(path, "log", timeout=5.0)
            if not resp.get("ok"):
                print(f"\n[detached: {resp.get('error')}]")
                return 1
            msgs = resp.get("data", {}).get("messages", [])
            if len(msgs) != last_count or len(msgs) > seen:
                # Print the new/changed tail (from the last confirmed count).
                start = seen if seen <= len(msgs) else max(0, len(msgs) - 3)
                for m in msgs[start:]:
                    role = m.get("role", "")
                    text = m.get("text", "")
                    print(f"\n{_role_prefix(role)}\n{text}")
                sys.stdout.flush()
                seen = len(msgs)
                last_count = len(msgs)
    except KeyboardInterrupt:
        print("\n[detached]")
        return 0
    except Exception as exc:
        print(f"\n[detached: {type(exc).__name__}: {exc}]")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

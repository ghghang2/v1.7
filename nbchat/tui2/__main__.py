"""``python -m nbchat.tui2`` — run the TUI v2 chat application.

The full app (Phase 3) is the default; pass ``--demo`` to launch the
Phase 1 rendering demo instead.
"""
import sys


def run() -> int:
    # ``--v2`` is the v1 entry point's flag for this engine; drop it so the
    # app's own parser only sees the flags it knows (``--new``,
    # ``--session``).
    argv = [a for a in sys.argv[1:] if a != "--v2"]
    if "--demo" in argv:
        from .demo import run as demo_run

        return demo_run()
    if "--attach" in argv or "-a" in argv:
        # Reattach view: tail a running (typically --bg) TUI conversation.
        from .attach import run as attach_run

        return attach_run(argv)
    from .app import run as app_run

    return app_run(argv)


def _main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(_main())

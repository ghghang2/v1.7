"""``python -m nbchat.tui2`` — run the TUI v2 chat application.

The full app (Phase 3) is the default; pass ``--demo`` to launch the
Phase 1 rendering demo instead.
"""
import sys


def _main() -> int:
    if "--demo" in sys.argv[1:]:
        from .demo import run
        return run()
    from .app import run
    return run()


if __name__ == "__main__":
    raise SystemExit(_main())

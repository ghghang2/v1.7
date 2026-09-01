"""Allow ``python -m nbchat.tui`` to start the terminal UI."""
from __future__ import annotations

import sys

from nbchat.tui.app import run

if __name__ == "__main__":
    sys.exit(run())

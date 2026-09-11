"""Entry point for ``python -m nbchat.tui3``.

Runs the tui3 application (the tui2 base extended with the tui3 feature
wave).  Equivalent to ``python -m nbchat.tui --v3``.
"""

import sys

from .app import run

if __name__ == "__main__":
    sys.exit(run())

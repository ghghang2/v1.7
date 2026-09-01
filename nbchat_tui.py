#!/usr/bin/env python3
"""Convenience launcher for the nbchat terminal UI.

Equivalent to ``python -m nbchat.tui``.  Run it directly:

    python nbchat_tui.py            # interactive chat
    python nbchat_tui.py --check    # verify the llama-server is up
"""
from __future__ import annotations

import sys

from nbchat.tui.app import run

if __name__ == "__main__":
    sys.exit(run())

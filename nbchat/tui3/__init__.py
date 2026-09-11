"""nbchat.tui3 - the tui3 version of the chat TUI.

tui3 is the NEXT version after tui2. It is a clear, delineated feature set:

  * ``nbchat.tui2`` = the base harness + the fix wave + the first feature
    wave (the "strong working version" you are testing).
  * ``nbchat.tui3`` = the tui2 base, EXTENDED with the tui3 feature wave
    (this module).  It subclasses the tui2 ``ChatApp`` so it inherits the
    entire tui2 feature set and adds tui3 features on top.

The tui3 entry points::

    python -m nbchat.tui3            # run the tui3 app
    python -m nbchat.tui --v3        # dispatch to the tui3 app

Because tui3 subclasses the tui2 ``ChatApp`` and reuses the tui2 entry
plumbing, the version-to-feature-set boundary is explicit: every tui3
feature lives in :mod:`nbchat.tui3`, and the tui2 base is left untouched.
"""

from .app import ChatApp, run, VERSION

__all__ = ["ChatApp", "run", "VERSION"]

# The tui3 feature wave is wave 2 on top of the tui2 base.
VERSION = "tui3"

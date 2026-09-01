# nbchat Terminal UI (TUI) — Usage Guide

A minimal, **single-command** terminal chat interface for nbchat. It runs in a
plain terminal — no Jupyter, no `ipywidgets`, no browser. It reuses the **exact
same agent stack** as the notebook UI and the WhatsApp channel
(`ContextMixin` + `ConversationMixin`), so you get the full agentic experience
(tool calling, streaming, memory, context management) with only the output
layer swapped from widgets to `stdout`.

```
python -m nbchat.tui
```

---

## 1. Requirements

- Python 3.10+ with the project dependencies installed
  (`pip install -r requirements.txt` — no extra packages needed for the TUI).
- A running local `llama.cpp` server (see §2). The TUI talks to it over the
  OpenAI-compatible API at `SERVER_URL` in `repo_config.yaml`.

The TUI only uses the Python standard library (`argparse`, `sqlite3` via the
existing `db` module, `urllib`) plus the already-required `openai` client.

## 2. Start the LLM server

```bash
python run.py            # start llama-server + install deps
python run.py --status   # show service status
python run.py --stop     # stop the services
```

You can also verify connectivity without launching a chat:

```bash
python -m nbchat.tui --check     # prints "reachable" / "NOT reachable"
```

## 3. Launch the TUI

```bash
python -m nbchat.tui          # or:  python nbchat_tui.py
```

### Command-line options

| Option            | Description                                                        |
|-------------------|--------------------------------------------------------------------|
| `--new`           | Start a brand-new session instead of resuming the last one.        |
| `--session ID`    | Resume a specific session (see `/sessions`).                       |
| `--no-color`      | Disable ANSI colours (also auto-disabled on non-TTY / `NO_COLOR`). |
| `--check`         | Only check the server is reachable, then exit (no chat).           |
| `-h`, `--help`    | Show help.                                                         |

## 4. Chatting

Type a normal message and press **Enter**. The reply **streams in live**, and
the model's reasoning is shown first in dim text.

A typical exchange looks like this (colours on a colour terminal):

```
You:
  What files are in the repo?

  [thinking] Let me list the repository contents…           (dim)
» Let me check. I'll run a command to list files…           (cyan arrow)
  [tool] run_command(command=ls -la …)                      (blue)
         {"stdout": "total 210…"}
» The repository contains the nbchat package, run.py, …

❯
```

### Reading the output

| Marker        | Colour | Meaning                                            |
|---------------|--------|----------------------------------------------------|
| `You:`        | green  | Your message, echoed back.                         |
| `[thinking]`  | dim    | The model's reasoning / chain-of-thought (streaming). |
| `»`           | cyan   | The assistant's reply (streaming).                 |
| `[tool] …`    | blue   | A tool was called; the dim line is a result preview. |
| `!`           | red    | An agent notice / warning (e.g. max tool turns).   |

## 5. In-session commands

Type these at the prompt (they are not sent to the model):

| Command          | What it does                                            |
|------------------|---------------------------------------------------------|
| `/help`          | Show the built-in help.                                 |
| `/new`           | Start a new session.                                    |
| `/sessions`      | List all terminal sessions (ids start with `tui:`).     |
| `/load <id>`     | Load/switch to a specific session from `/sessions`.     |
| `/history`       | Print the current session's messages.                   |
| `/model`         | Show the active model, server URL, and session id.      |
| `/clear`         | Clear the screen.                                       |
| `/quit`          | Exit (as do **Ctrl+D**, and **Ctrl+C** at the prompt).  |

## 6. Input techniques

- **Multi-line input** — end a line with a trailing backslash `\` to continue
  it on the next line:
  ```
  ❯ Please fix the bug in \
  nbchat/tui/app.py where the \
  session id is not reset.
  ```
- **Interrupt a reply** — press **Ctrl+C** while a reply is streaming to stop
  it immediately. The in-progress turn is abandoned and you return to the
  prompt.
- **Exit** — `/quit`, **Ctrl+D**, or **Ctrl+C** at an empty prompt.

## 7. Sessions

- Every chat is a **session** with an id like `tui:23f3ff9950f1`.
- Sessions are persisted in `nbchat/chat_history.db` (SQLite), alongside the
  notebook and WhatsApp sessions.
- **Auto-resume:** on launch (without `--new`/`--session`), the TUI resumes
  the most recent terminal session, so a long conversation continues where it
  left off.
- Manage them at runtime with `/new`, `/sessions`, and `/load <id>`.

## 8. Features

The TUI inherits the entire agent stack, so everything the notebook UI does,
it does too — in the terminal:

- **Live streaming** of the assistant reply and the model's reasoning.
- **Agentic tool-calling loop** — the model can call tools repeatedly (up to
  `max_tool_turns`) until the task is complete.
- **L1 core memory** — persistent goal / constraints / active-entities /
  error-history injected each turn, so context survives long conversations.
- **L2 episodic memory** — importance-scored past tool exchanges retrieved
  back into context when relevant.
- **Token-budget context windowing** — history is walked back to fit the model
  context, with async structured summarisation of evicted turns and a hard
  trim as a last resort.
- **Output compression** — large tool outputs are compressed before going back
  to the model to save context.
- **Tool execution** with per-tool timeouts and retries for transient failures
  (timeouts / network / 5xx); deterministic errors are returned immediately.
- **Stall detection** — if the same tool calls repeat across turns, an
  interrupting nudge is injected.
- **Session persistence** and resume across restarts.
- **Lightweight colours** with automatic fallback for non-TTY / `NO_COLOR`
  (piped output stays clean).

### Available tools

`browser`, `create_file`, `get_weather`, `make_change_to_file`,
`push_to_github`, `repo_overview`, `run_command`, `run_tests`, `send_email`.

(These are auto-discovered from `nbchat/tools/` and are shared with the
notebook and WhatsApp front-ends.)

## 9. Configuration

Runtime values live in **`repo_config.yaml`** at the repo root. The most
relevant for the TUI:

| Key                        | Effect                                             |
|----------------------------|----------------------------------------------------|
| `SERVER_URL`               | Where the TUI talks to llama-server.               |
| `MODEL_NAME`               | Model served (shown by `/model`).                  |
| `max_tool_turns`           | Max tool-calling iterations per message.           |
| `stall_turns`              | Repeats before stall detection fires.              |
| `context_headroom_ratio`, `prefix_token_reserve`, `persist_fraction` | Context-management tuning. |
| `browser_timeout`, `tests_timeout`, `other_tools_timeout` | Per-tool wall-clock budgets (seconds). |

Change a value and restart the server / TUI for it to take effect.

## 10. Troubleshooting

- **"llama-server is not reachable"** in the banner, or LLM calls failing —
  start the server: `python run.py`, then confirm with `python -m nbchat.tui --check`.
- **No colours** — expected when output is piped or `NO_COLOR` is set; use a
  real terminal (and drop `--no-color`) to enable them.
- **A reply stops mid-sentence** — you pressed **Ctrl+C** (intentional
  interrupt), or the server/process was killed. Nothing is lost: the message
  and any completed tool actions are already persisted; just continue.
- **Can't find a session** — list them with `/sessions`; ids are prefixed
  `tui:`. Use `/load <id>` to switch.
- **`ModuleNotFoundError: No module named 'nbchat'`** when running tests — the
  package isn't installed; run pytest from the repo root (a root-level
  `conftest.py` makes the import work) — `python -m pytest -q`.

## 11. Where things live

```
nbchat/tui/
  agent.py     TerminalAgent — the agent + terminal output hooks
  app.py       REPL, banner, slash commands, argparse entry point
  colors.py    ANSI palette (auto-disable on non-TTY / NO_COLOR)
  __main__.py  enables `python -m nbchat.tui`
nbchat_tui.py  root launcher: `python nbchat_tui.py`
tests/test_tui.py  TUI tests (no server required)
```

## 12. Run the tests

```bash
python -m pytest -q          # 18 TUI tests, no server needed
```

---

*The TUI shares its behaviour with the notebook UI and WhatsApp channel — any
fix to the shared agent stack applies to all three front-ends.*

# nbchat

nbchat is a lightweight LLM inference harness: an agentic chat loop (tool
calling, streaming, L1/L2 memory, context windowing and output compression)
that talks to a local [llama.cpp](https://github.com/ggml-org/llama.cpp)
server over the OpenAI-compatible API.

The same agent stack (tool loop, memory, context management) powers every
front-end. Only the input/output layer changes:

| Front-end | Start with | Needs |
|-----------|-----------|-------|
| **Terminal UI (TUI)** | `python -m nbchat.tui` | a plain terminal |
| **Email bridge** (extends the TUI) | `python -m nbchat.tui --email` | the TUI + Gmail app password |
| **Supervisor** (extends the TUI) | `python -m nbchat.tui --supervisor` | the TUI (uses the 2nd parallel slot) |
| **WhatsApp channel** | `python -m nbchat.channels.whatsapp_server` | FastAPI + a Node bridge |
| **Jupyter notebook UI** | open a `.ipynb` and `from nbchat.ui.chatui import ChatUI` | Jupyter + `ipywidgets` |

---

## 1. Start the LLM server

```bash
python run.py           # downloads/starts llama-server + installs deps
python run.py --status  # show service status
python run.py --stop    # stop the services
```

The server URL, model and context size come from `repo_config.yaml`. Verify
connectivity without launching a chat:

```bash
python -m nbchat.tui --check     # prints "reachable" / "NOT reachable"
```

---

## 2. Chat from the terminal (TUI)

```bash
python -m nbchat.tui            # or: python nbchat_tui.py
```

Type a message and press Enter — the reply (and the model's reasoning) stream
in live. The agent can call tools repeatedly until the task is done.

```
You:
  List the Python files in the repo and tell me which define a Supervisor class.

  [thinking] Let me list the files, then search for the class…           (dim)
» I'll list the files first.
  [tool] run_command(command="find . -name '*.py' …")
         {"stdout": "./nbchat/core/supervisor.py …"}
» The `Supervisor` class is defined in `nbchat/core/supervisor.py`.

➊
```

### Command-line options

```
--new          force a brand-new session
--session ID   resume a specific session id (see /sessions)
--no-color     disable ANSI colours
--check        only check the llama-server is reachable, then exit
```

### In-session commands

```
/help            show help
/new             start a new session
/sessions        list terminal sessions (ids start with 'tui:')
/load <id>       load one of the sessions from /sessions
/history         print the current session's messages
/model           show the active model and server
/clear           clear the screen
/sup <question>  ask the supervisor about system state (needs --supervisor)
/quit            exit (Ctrl+C / Ctrl+D also work)
```

**Input techniques**

- **Multi-line** — end a line with a trailing backslash `\` to wrap it.
- **Interrupt / redirect** — type a new message (or press `Ctrl+C`) while a
  reply is streaming to stop it and redirect the agent immediately.

Sessions persist in `nbchat/chat_history.db`; the most recent one is resumed
on the next start.

---

## 3. Email bridge — your inbox becomes the chat input box

A daemon thread polls your Gmail inbox (IMAP) and injects **matching** emails
into the chat stream as user interjections — exactly as if you typed them.
Optionally it sends the agent's reply back by email.

```bash
export GHG_APP_PASSWORD="your-16-char-app-password"
python -m nbchat.tui --email            # auto-reply ON (default)
python -m nbchat.tui --email --no-auto-reply   # inject only, don't email back
```

### Example

While the TUI is open, send yourself a Gmail email:

```
From:    ghghang2@gmail.com
Subject: nbchat: what files are dirty in git right now?
Body:    check git status and tell me.
```

The bridge detects it within one poll interval (default 3 s) and the agent
answers in the terminal — and (with auto-reply) emails you the answer back.

You get three emails back, all in the **same Gmail thread** (replies carry
`In-Reply-To`/`References` headers, so nothing scatters into a new thread):

1. **Ack** — sent the moment the email is queued:
   `Received: <subject> / Priority: low / You are in the queue.`
2. **Working** — sent when the worker starts on it:
   `Working on: <subject> / This may take a moment.`
3. **The answer** — the agent's reply, under `Re: <subject>`.

Replying to any of these system emails in Gmail is safe and works as a
normal new command — the bridge identifies its own outbound mail by the
`X-Nbchat: outbound` header (added to every message it sends, including
`send_email` tool output), not by the subject line.

### What gets injected

Only emails that satisfy **all** of these are processed:

1. **Not** one of the bridge's own outbound emails (no `X-Nbchat` header).
2. Sent **from your own address** (`ghghang2@gmail.com`).
3. Subject contains `nbchat` (routes to the assistant) **or** `supervisor`
   (routes to the supervisor, when one is running — see §4).
4. Sent **since this chat session started** (a 60 s grace window). Older
   unread mail is left untouched — never read, never replied to.

Everything else is silently marked read and ignored. Emails are marked read
only **after** they have been injected, so a crash never discloses a message.

### Priority & preemption

Emails whose subject contains `supervisor`, `urgent` or `high priority`
(case-insensitive) are **high priority**: they jump ahead of queued
low-priority emails, and if a low-priority email is *already being
processed*, the in-flight turn is interrupted and the low-priority email is
re-queued to be retried afterwards. High-priority emails also get a
`Priority: high` line in the ack.

Under the hood, detection (a fast header-only IMAP peek, with batched
mark-read) and processing (the LLM turn) run in separate threads on a
priority queue, so a slow turn never delays pickup of the next command.

### Configuration

| Key in `repo_config.yaml` | Default | Effect |
|---------------------------|---------|--------|
| `email_poll_interval`     | `3`     | seconds between IMAP polls. |
| `email_auto_reply`        | `true`  | send the agent's reply back to the sender. |

---

## 4. Supervisor — a second, always-on LLM on the 2nd parallel slot

The supervisor is an independent LLM instance that runs on the server's
second parallel slot (`n_parallel: 2`), so it never blocks the assistant's
in-flight turn. It has two capabilities:

1. **State queries** — answer a question about the server, git status, task
   stats, or the assistant's current progress, with one non-streaming call.
2. **Watchdog** — periodically review the assistant's in-flight work and, if
   it looks off-track, inject a one-line corrective instruction into the
   assistant's interjection queue (drained at the next safe point).

```bash
python -m nbchat.tui --supervisor
```

### Ask the supervisor from the terminal

```
➊ /sup what model and context size are we running?
  [supervisor] asking: what model and context size are we running?
  [supervisor] Model Qwen3.8-27B-GGUF:UD-Q4_K_XL, ctx 131072, n_parallel 2.
```

The call runs on a background thread, so the prompt returns immediately and
you can keep typing while the supervisor answers.

### Ask the supervisor by email

With `--supervisor` and `--email` both on, an email whose subject contains
`supervisor` is routed to the supervisor instead of the assistant. The
**body** is treated as the question (the subject is used only for routing
and threading), and the answer is emailed back in the same thread:

```
From:    ghghang2@gmail.com
Subject: supervisor: how many errors in the chat log?
Body:    give me the task stats.
```

Supervisor emails are given **queue priority** (see §3) over normal emails,
so a question is answered in real-time even if it arrives while a long
normal email turn is still queued — the in-flight low-priority turn is
interrupted and re-queued.

### Configuration

| Key in `repo_config.yaml` | Default | Effect |
|---------------------------|---------|--------|
| `supervisor_enabled`       | `false` | start the watchdog by default (also gated by `--supervisor`). |
| `supervisor_interval`      | `60`    | seconds between watchdog reviews. |
| `supervisor_cooldown`      | `300`   | min seconds between two interjections. |
| `supervisor_max_output_tokens` | `512` | max tokens for a supervisor answer. |

---

## 5. WhatsApp channel

A headless agent + FastAPI bridge serves WhatsApp messages over HTTP. Each
sender JID gets its own isolated session (prefixed `wa:`) in the shared
`chat_history.db`.

```bash
python -m nbchat.channels.whatsapp_server
# or:
uvicorn nbchat.channels.whatsapp_server:app --host 127.0.0.1 --port 8764
```

### Example

The Node bridge (`whatsapp_bridge.js`) forwards an inbound message:

```bash
curl -X POST http://127.0.0.1:8764/message \
  -H "Content-Type: application/json" \
  -d '{"jid": "+15551234567@s.whatsapp.net", "text": "what is the repo status?"}'
```

Response:

```json
{"reply": "The repo is on branch main, 2 files dirty, all tests passing."}
```

---

## 6. Jupyter notebook UI

The full widget-based chat interface. In a notebook cell:

```python
from nbchat.ui.chatui import ChatUI
chat = ChatUI()
```

This renders the streaming chat UI (reasoning, tool calls, monitoring panel)
using `ipywidgets`. It reuses the exact same `ContextMixin` +
`ConversationMixin` stack as the TUI and WhatsApp channel.

---

## 7. Tools

Tools are auto-discovered from `nbchat/tools/` (any module exposing a `func`
callable plus `name`/`description`). They are shared by **all** front-ends.
The model decides when to call them; you don't invoke them directly.

| Tool | What it does | Example call the model makes |
|------|--------------|------------------------------|
| `run_command` | Run a shell command in the repo, return stdout/stderr/exit code. | `run_command(command="git status --porcelain")` |
| `run_tests` | Run the pytest suite, return pass/fail counts. | `run_tests()` |
| `create_file` | Create a new file under the repo root. | `create_file(path="notes.md", content="…")` |
| `make_change_to_file` | Apply a unified diff (create/update/delete). | `make_change_to_file(path="a.py", op_type="update", diff="…")` |
| `get_weather` | Current/forecast weather for a city. | `get_weather(city="Berlin")` |
| `browser` | Visit a URL, perform actions, extract page text. | `browser(url="https://example.com")` |
| `push_to_github` | Commit + push the repo to GitHub. | `push_to_github(commit_message="…")` |
| `repo_overview` | Build a markdown table of all Python functions. | `repo_overview()` |
| `send_email` | Send a plain-text Gmail email (stamped `X-Nbchat: outbound` so the email bridge never re-injects it). | `send_email(subject="…", body="…")` |

### Example: a multi-tool task

```
You:
  Find the tests that touch the email bridge, run them, and if any fail, fix the
  first one and push.

» I'll locate the tests, run them, then act on the result.
  [tool] run_command(command="grep -rl email_bridge tests/")
  [tool] run_tests()
  [tool] make_change_to_file(path="tests/test_email_bridge.py", …)
  [tool] run_tests()
  [tool] push_to_github(commit_message="Fix email bridge test")
» Done — all 95 tests pass and the fix is pushed.
```

---

## 8. Memory (L1 core + L2 episodic)

The agent keeps long conversations coherent with two memory layers, shared by
every front-end:

- **L1 core memory** — a compact, always-injected block holding the current
  goal, constraints, active entities and recent errors. Updated as the
  conversation progresses so context survives very long sessions.
- **L2 episodic memory** — importance-scored past tool exchanges, persisted to
  `chat_history.db` and retrieved back into context when topically relevant
  (matched against the active entities).

Tuning lives in `repo_config.yaml`:

| Key | Default | Effect |
|-----|---------|--------|
| `l2_retrieval_limit` | `5` | max episodic exchanges retrieved per turn. |
| `persist_fraction` | `0.40` | top 40% of exchanges by importance go to L2. |
| `core_memory_active_entities_limit` | `20` | max entities tracked in L1. |

---

## 9. Context windowing & output compression

- **Token-budget windowing** — history is walked back to fit the model
  context (`context_headroom_ratio`, `prefix_token_reserve`). Evicted turns
  are asynchronously summarised into a structured prior-context block; a hard
  trim is the last resort.
- **Output compression** — large tool outputs are compressed (skeletons for
  code/JSON/YAML, head/tail otherwise) before going back to the model to save
  context.

---

## 10. Configuration

All runtime values live in **`repo_config.yaml`** at the repo root. The most
commonly edited keys:

| Key | Effect |
|-----|--------|
| `SERVER_URL`, `MODEL_NAME` | Where and what to talk to. |
| `n_parallel` | Parallel slots (2 = assistant + supervisor). |
| `max_tool_turns`, `stall_turns` | Agentic loop limits + stall detection. |
| `context_headroom_ratio`, `prefix_token_reserve`, `persist_fraction` | Context / memory tuning. |
| `browser_timeout`, `tests_timeout`, `other_tools_timeout` | Per-tool wall-clock budgets (s). |
| `email_poll_interval`, `email_auto_reply` | Email bridge. |
| `supervisor_enabled`, `supervisor_interval`, `supervisor_cooldown` | Supervisor. |

Change a value and restart the server / front-end for it to take effect.

---

## Layout

```
nbchat/
  core/     config, OpenAI client, SQLite db, compressor, monitoring, retry,
            supervisor, email_inbox (IMAP), email_smtp (SMTP),
            remote (git + GitHub client for push_to_github)
  tools/    auto-discovered tool functions (run_command, git, browser, …)
  ui/       context_manager (L1/L2 memory + windowing), conversation (agentic
            loop), chatui (Jupyter), tool executor, styles
  tui/      terminal UI (TerminalAgent + REPL + email bridge)
  channels/ WhatsApp bridge (FastAPI + Node)
run.py      start/stop the local llama-server
repo_config.yaml   all runtime configuration
```

---

## Tests

```bash
python -m pytest -q
```

The TUI, email-bridge and supervisor tests (`tests/test_tui.py`,
`tests/test_email_bridge.py`, `tests/test_supervisor.py`) do **not** require a
running llama-server or a real IMAP/SMTP connection — network calls are
mocked.

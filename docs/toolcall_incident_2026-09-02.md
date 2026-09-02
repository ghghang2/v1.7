# Legacy XML Tool-Call Drift — Incident Report & Fix Plan

**Date:** 2026-09-02
**Model:** `qwen3.8-27b` via llama.cpp (ctx 131072, n_parallel 2, reasoning on)
**Status:** Analysis saved; **no code changes applied** (per instruction).
Fix design below is complete and ready to apply when approved.

---

## 1. Incident

The model intermittently emits tool calls as legacy XML **text** in the
assistant content —

```
<tool_call>
<function=run_command>
<parameter=command>
...
</parameter>
</function>
</tool_call>
```

— instead of the structured `tool_calls` channel. Consequences:

1. `nbchat/ui/conversation.py::_stream_response` only reads
   `delta.tool_calls`, so a turn that contains only text markup returns
   `tool_calls=None` and is **treated as a final answer**: nothing
   executes. The TUI displays the raw markup as the "reply".
2. The unexecuted markup is stored in `chat_log` (`db.log_message`) and
   re-fed to the model on **every subsequent turn** via
   `chat_builder.build_messages` — a self-reinforcing drift. The model is
   shown, repeatedly, exact examples of the failure pattern it just
   produced.
3. In a severe case the loop consumed itself: the assistant's *attempts to
   fix the bug* were emitted as unexecuted markup and stored too (rows
   463, 482, 516 below).

### Evidence (quantified from `nbchat/chat_history.db`)

**25 assistant rows containing literal `<tool_call>` markup**, all today
18:57–20:19, across 4 TUI sessions:

| id   | ts            | session            | len  | error_flag |
|------|---------------|--------------------|------|------------|
| 150  | 18:57:46      | tui:d4f14887491c   | 363  | 0          |
| 399  | 19:09:33      | tui:b9f386163c5c   | 2099 | 0          |
| 439  | 19:11:46      | tui:b9f386163c5c   | 5550 | 1          |
| 463  | 19:15:25      | tui:b9f386163c5c   | 9774 | 1          |
| 466  | 19:21:21      | tui:b9f386163c5c   | 1045 | 0          |
| 470  | 19:21:43      | tui:b9f386163c5c   | 582  | 0          |
| 473  | 19:22:10      | tui:b9f386163c5c   | 1328 | 0          |
| 476  | 19:22:54      | tui:b9f386163c5c   | 1012 | 0          |
| 482  | 19:23:49      | tui:b9f386163c5c   | 7670 | 1          |
| 485  | 19:24:34      | tui:b9f386163c5c   | 976  | 1          |
| 491  | 19:25:07      | tui:b9f386163c5c   | 7587 | 0          |
| 498  | 19:25:56      | tui:b9f386163c5c   | 1075 | 0          |
| 509  | 19:31:29      | tui:b9f386163c5c   | 9678 | 0          |
| 516  | 19:35:21      | tui:b9f386163c5c   | 12916| 1          |
| 522  | 19:38:03      | tui:b9f386163c5c   | 8974 | 0          |
| 529  | 19:38:51      | tui:b9f386163c5c   | 8879 | 0          |
| 532  | 19:39:30      | tui:b9f386163c5c   | 591  | 1          |
| 535  | 19:40:27      | tui:b9f386163c5c   | 1212 | 0          |
| 545  | 19:47:24      | tui:b9f386163c5c   | 1250 | 0          |
| 556  | 19:49:31      | tui:b9f386163c5c   | 1918 | 0          |
| 559  | 19:52:25      | tui:b9f386163c5c   | 12451| 1          |
| 563  | 19:53:19      | tui:b9f386163c5c   | 12966| 1          |
| 778  | 20:03:56      | tui:00f23f09ca24   | 253  | 0          |
| 831  | 20:11:43      | tui:ce21fd2eae5e   | 113  | 0          |
| 875  | 20:19:33      | tui:ce21fd2eae5e   | 1363 | 1          |

Notes:

- Row **150** is the earliest observed drift: the *entire* assistant
  content is two `<tool_call>` blocks calling `read_file` — a turn where
  nothing was said and nothing executed.
- Rows **463 / 482 / 516** are prior fix attempts for *this exact bug*
  (a patch for `conversation.py`, a Python patch-script heredoc, a
  `create_file` of a one-shot patch) — all emitted as markup, all
  unexecuted. The working tree stayed clean; the fixes never landed.
- Rows **559 / 563** show the loop's endgame: the assistant gave up on
  executing and instead wrote step-by-step *manual* patch instructions,
  then tried to commit those instructions to
  `docs/toolcall_recovery_fix.md` — again as unexecuted markup. The file
  does not exist.
- **6 rows in `episodic_store.outcome_summary`** also contain the
  markup, so L2 retrieval re-serves the pattern to new sessions.
- This is a **format** failure, not truncation: all rows are
  `finish_reason=stop`, well under `max_llm_output_tokens: 32768`.
- The system prompt already forbids text-emitted tool calls ("tool calls
  are emitted ONLY through the tool-calling mechanism, never as text
  markup such as `<tool_call>`") — the instruction does not hold under
  drift, which is why the fix must be mechanical.

### Corroborating telemetry

- `inference_metrics.log` 20:00–20:05: a burst of ~15 short calls at
  P≈36k–40k prompt tokens, C≈100–300 completion tokens each — the model
  chattering ~1–3-line fragments per turn with no tool execution. The
  prompt grows monotonically (36252 → 40944) because each unexecuted
  markup turn is appended to history and re-fed.
- `compaction.log` 20:01–20:05: repeated
  "Possible truncation detected (voice_unclosed=True, content_tail='
  Working on it. <voice>Almost there, sir.</voice')" — a second,
  related symptom: the drift also truncates the `<voice>` close tag
  (`</voice`), which the truncation guard then loops on with continue
  nudges.
- `compaction.log` ~19:45: `openai.APIConnectionError: Connection
  refused` against `localhost:8080` — llama-server went down at some
  point during the incident (recovered before 20:00).

---

## 2. Root cause (four compounding defects)

1. **Model drift (trigger, probabilistic).** Under long context
   (P ≈ 36k–40k) and high reasoning effort, `qwen3.x-27b` intermittently
   reverts to its training-time XML tool-call format. Prompt
   instruction alone does not prevent it.
2. **No recovery path.** `_stream_response` reads only
   `delta.tool_calls`. Text-emitted markup in `delta.content` is
   discarded and the turn finalises with nothing executed.
3. **Poison feedback.** The unexecuted markup is persisted to
   `chat_log` (and to `episodic_store`) and re-fed verbatim on every
   later turn — each poisoned turn makes the next drift more likely.
4. **No scrub at build time.** `chat_builder.build_messages` passes
   poisoned assistant rows straight into the request; there is no
   sanitisation between storage and the model.

---

## 3. Immediate workaround (no code changes)

For the *running* instance, in priority order:

1. **`/new`** in the TUI. History is loaded per session
   (`db.load_history(session_id)`); a fresh session loads a clean window
   and stops the poisoned re-feeding for that session. (Poisoned rows
   stay in the DB for the old session; L2 episodic rows are session-
   scoped retrieval, so the main feedback loop is broken.)
2. **Restart the TUI process** if the drift is already active in the new
   session. The drift correlates with accumulated context (P tokens);
   a fresh process starts from a minimal prompt.
3. **`/effort medium`** for the session while working around it. Drift
   has been observed on reasoning-heavy turns; lower effort reduces the
   per-turn completion budget the model spends "writing markup".
4. **Restart llama-server** if requests fail with connection refused
   (it died once during this incident; see `compaction.log`).

Known limitation of the workaround: the poison remains in
`chat_log`/`episodic_store`, so (a) `/load <old-session-id>` re-enters
the loop, and (b) L2 retrieval may surface the 6 poisoned summaries.
Definitive removal requires the Part 3 scrub below.

---

## 4. Fix design (ready to apply; not yet applied)

Three parts. All code below is final and idempotent-guarded
(refuses to re-patch).

### Part 1 — `nbchat/ui/conversation.py`: recover text-emitted calls

Imports: add `import re` and `import uuid` after `import logging`.

Module-level helpers (insert before `class ConversationMixin:`):

```python
_TOOL_BLOCK_RE = re.compile(
    r"<tool_call>\s*<function=([A-Za-z_]\w*)>\s*(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<parameter=([A-Za-z_]\w*)>\s*(.*?)\s*</parameter>", re.DOTALL
)


def _recover_text_tool_calls(content: str) -> list[dict] | None:
    """Parse legacy XML <tool_call> blocks out of assistant *text*.

    Some models emit tool calls as <tool_call><function=name>
    <parameter=k>v</parameter></function></tool_call> text instead of the
    structured tool_calls channel.  Returns tool-call dicts in the same
    shape as the structured channel, or None if no complete block parsed.
    """
    calls: list[dict] = []
    for m in _TOOL_BLOCK_RE.finditer(content or ""):
        name, body = m.group(1), m.group(2)
        args = {pm.group(1): pm.group(2) for pm in _PARAM_RE.finditer(body)}
        if not args:
            continue
        try:
            args_json = json.dumps(args)
        except (TypeError, ValueError):
            continue
        calls.append({
            "id": f"recovered_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": args_json},
        })
    return calls or None


def _strip_tool_blocks(content: str) -> str:
    """Remove legacy <tool_call> markup from text, keeping surrounding prose.

    A truncated trailing block (an unclosed <tool_call> at the end) is
    removed too, so a half-emitted call is never re-fed to the model.
    """
    if not content:
        return ""
    text = _TOOL_BLOCK_RE.sub(" ", content)
    text = re.sub(r"<tool_call>.*$", " ", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
```

Loop hook (in `_run_conversation_loop`, insert **between** the
`if reasoning:` block and the existing
`if not tool_calls or finish_reason != "tool_calls":` line):

```python
            # ── Legacy XML tool-call recovery ───────────────────────────────
            # Some models emit tool calls as <tool_call>...</tool_call> text
            # in the content instead of the structured tool_calls channel.
            # Treated as a final answer, nothing executes — and the
            # unexecuted markup stored in history is re-fed on every
            # subsequent turn, reinforcing the drift.  Recover complete,
            # parseable blocks and route them through the normal
            # tool-execution path below; if the markup is malformed or
            # truncated, nudge the model to re-emit via the proper channel.
            if not tool_calls and content and "<tool_call" in content:
                recovered = _recover_text_tool_calls(content)
                if recovered:
                    _log.warning(
                        "Recovered %d tool call(s) emitted as legacy XML text; "
                        "executing via normal path.", len(recovered),
                    )
                    self._on_agent_message(
                        f"Recovered {len(recovered)} tool call(s) emitted as "
                        "text markup; executing them now."
                    )
                    tool_calls = recovered
                    finish_reason = "tool_calls"
                    content = _strip_tool_blocks(content)
                elif turn < self.MAX_TOOL_TURNS:
                    _log.warning(
                        "Unparseable <tool_call> text markup in content "
                        "(tail=%r); nudging model to re-emit.",
                        (content or "")[-80:],
                    )
                    _nudge = (
                        "Your previous reply contained a tool call written as text "
                        "markup (e.g. <tool_call>), which was NOT executed. Re-issue "
                        "that tool call using the structured tool-calling mechanism, "
                        "not as text."
                    )
                    self._on_agent_message(
                        "Detected unexecuted tool call in text; asking the "
                        "model to re-emit it."
                    )
                    _clean = _strip_tool_blocks(content) or (
                        "[tool call emitted as text markup; not executed]"
                    )
                    with self._history_lock:
                        self.history.append(("assistant", _clean, "", "", "", 0))
                    db.log_message(self.session_id, "assistant", _clean)
                    with self._history_lock:
                        self.history.append(("user", _nudge, "", "", "", 0))
                    db.log_message(self.session_id, "user", _nudge)
                    messages.append({"role": "assistant", "content": _clean})
                    messages.append({"role": "user", "content": _nudge})
                    continue
```

Design notes:

- Recovered calls get fresh `recovered_*` ids; the rest of the loop
  (executor, compression, monitoring, L1/L2) is untouched.
- Only *complete, parseable* blocks execute. Truncated/malformed markup
  must NOT be executed (it may be missing arguments) — the nudge path
  re-emits a cleaned assistant row (markup stripped, never the raw
  markup) so history stays clean even on the failure path.
- The `elif turn < self.MAX_TOOL_TURNS` guard prevents an infinite
  nudge loop at the turn ceiling; at the ceiling the turn finalises
  normally.

### Part 2 — `nbchat/ui/chat_builder.py`: scrub at build time

Defence in depth: even if a poisoned row was written before Part 1
existed (or by another path), it is never re-fed to the model.

Imports: add `import re`.

Module-level helpers (insert before `def build_messages(`):

```python
_TOOL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
_TOOL_TRAIL_RE = re.compile(r"<tool_call>.*$", re.DOTALL)


def _scrub_tool_markup(content: str) -> str:
    """Remove unexecuted legacy <tool_call> XML markup from assistant text.

    The model occasionally emits tool calls as text instead of the
    structured tool_calls channel.  Re-feeding that markup to the model
    reinforces the drift, so message lists are built with it stripped.
    Surrounding prose is preserved.
    """
    if not content:
        return ""
    text = _TOOL_BLOCK_RE.sub(" ", content)
    text = _TOOL_TRAIL_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
```

Plain `assistant` branch (replace the `else:` body):

```python
            else:
                cleaned = _scrub_tool_markup(content)
                if content and not cleaned:
                    cleaned = (
                        "[Your previous reply contained only a tool call written as "
                        "text markup; it was not executed.]"
                    )
                messages.append({"role": "assistant", "content": cleaned})
```

`assistant_full` branch (extend, after the existing
`if msg.get("tool_calls") and not msg.get("content"):` handling):

```python
                if msg.get("content"):
                    # Scrub any text-emitted tool markup that slipped in.
                    msg["content"] = _scrub_tool_markup(msg["content"]) or None
                messages.append(msg)
```

Note: rows that carry *real* structured `tool_calls` keep their markup
free content (the content field of such rows is prose or None); the
marker text is only used when the row is *entirely* unexecuted markup,
so the model still sees that a call was intended but not made.

### Part 3 — one-off DB scrub

Removes the existing poison so `/load <old-session-id>` and L2
retrieval stop re-serving the pattern:

```python
import re, sqlite3

_BLOCK = re.compile(r"<tool_call>.*?</tool_call>", re.S)
_TRAIL = re.compile(r"<tool_call>.*$", re.S)

def scrub(t):
    if not t:
        return t
    t = _BLOCK.sub(" ", t)
    t = _TRAIL.sub(" ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

conn = sqlite3.connect("nbchat/chat_history.db")
cur = conn.cursor()

rows = cur.execute(
    "SELECT id, content FROM chat_log "
    "WHERE role='assistant' AND content LIKE '%<tool_call%'"
).fetchall()
for rid, c in rows:
    cur.execute("UPDATE chat_log SET content=? WHERE id=?",
                (scrub(c) or "[tool call emitted as text markup; not executed]", rid))
conn.commit()
print(f"scrubbed {len(rows)} chat_log rows")

rows = cur.execute(
    "SELECT id, outcome_summary FROM episodic_store "
    "WHERE outcome_summary LIKE '%<tool_call%'"
).fetchall()
for rid, c in rows:
    cur.execute("UPDATE episodic_store SET outcome_summary=? WHERE id=?",
                (scrub(c) or "(scrubbed)", rid))
conn.commit()
print(f"scrubbed {len(rows)} episodic_store rows")
```

Expected: 25 chat_log rows and 6 episodic_store rows (as of this report).
Run **before** any new sessions so the clean baseline is verified.

### Optional (separate decision, not part of the patch)

- Tighten `DEFAULT_SYSTEM_PROMPT` (`repo_config.yaml`) — the line already
  exists; a stronger phrasing may help but **mutates `messages[0]`, the
  KV-cache-stable slot** (invalidates the prompt cache on every existing
  server load). Do it knowingly, and expect one slow first turn after
  the change.
- Lower default `reasoning_effort` for tool-heavy sessions if drift
  recurs after the patch — the patch makes drift *harmless*, but not
  *impossible*.

---

## 5. Verification plan (post-apply)

1. `python3 -m py_compile nbchat/ui/conversation.py nbchat/ui/chat_builder.py`
2. Add `tests/test_toolcall_recovery.py` covering:
   - `_recover_text_tool_calls`: two-block sample (shape of row 150),
     multiline heredoc command value, truncated block → `None`,
     plain text → `None`, empty/None → `None`, valid JSON args,
     `recovered_*` id shape.
   - `_strip_tool_blocks`: prose preserved, truncated tail removed,
     empty input.
   - `build_messages`: poisoned plain-`assistant` row scrubbed;
     pure-markup row replaced by the "not executed" marker;
     `assistant_full` row with structured `tool_calls` keeps its
     `tool_calls` intact while content is scrubbed.
3. `pytest tests/ -q` — full suite green before any push.
4. Run the Part 3 scrub; re-run the quantification query from §1 and
   confirm 0 remaining `chat_log` rows match `content LIKE
   '%<tool_call%'` for role `assistant`.
5. In the TUI, verify a drift turn logs
   `Recovered N tool call(s) emitted as legacy XML text` in
   `compaction.log` and the tool actually executes (a
   `tool`-role row appears in `chat_log` with the `recovered_*` id).

---

## 6. What was NOT done (and why)

- **No code applied.** Per instruction (2026-09-02): "save this to
  markdown instead of trying to make any fixes". A patch script and a
  test file were drafted during the investigation and then **deleted**;
  the working tree is clean apart from this document.
- No `repo_config.yaml` changes (KV-cache slot concern, §4 optional).
- The unrelated `</voice` truncation loop (§1 telemetry) is a *symptom*
  of the same drift — the close tag simply gets cut inside the
  malformed stream. Part 1's nudge path resolves it indirectly (the
  turn no longer finalises on broken markup). If it recurs *without*
  any `<tool_call>` markup, it needs its own look.

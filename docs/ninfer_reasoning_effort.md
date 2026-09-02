# Setting Reasoning Effort in `ninfer-serve`

Investigation of the `ninfer` codebase (checked out at `/workspace/ninfer`) into how the
reasoning effort of a served model is controlled, what the default is, and how to set it to
`medium` even though `ninfer-serve` exposes no such startup flag.

## TL;DR

- The startup flag is genuinely absent: `ninfer-serve` offers no `--reasoning-effort` option
  and the codebase reads no environment variable for it. The public documentation is correct.
- The default is determined by the **chat template embedded in the model artifact**. For the
  Qwen 3.6 / 3.8 27B "ReasoningEffort" template the default is **`xhigh`** (hard-coded).
- To run with `medium`, pass the effort **per request in the HTTP body**. All three served
  protocols accept it:
  - OpenAI Chat Completions: `reasoning_effort`
  - OpenAI Responses: `reasoning.effort`
  - Anthropic Messages: `output_config.effort`

## 1. Why there is no startup flag

The full startup-flag parser for the server lives in `src/serve/serve_options.cpp`. The
thinking-related process flags are only:

| Flag | Effect |
|---|---|
| `--no-thinking` | Turn thinking off by default for requests |
| `--preserve-thinking` | Keep closed-turn assistant reasoning in later prompts |
| `--default-thinking-budget N` | Cap model-origin thinking tokens for thinking-enabled requests |

There is no `--default-reasoning-effort` (or anything similar), and a repo-wide search finds no
`getenv`/`setenv` usage in `src/`, `apps/`, or `include/` — so there is no hidden environment
variable either. Effort is intentionally a per-request concept.

## 2. Where the default comes from

Request resolution happens in `resolve_prompt_semantics()` (`src/serve/translate.cpp`, ~line 113):

1. If the HTTP request carries an explicit effort field, that value wins.
2. Otherwise, if thinking is enabled, the server falls back to
   `capabilities.reasoning_effort.default_effort` — a capability value decoded from the
   `frontend/chat_template.jinja` embedded in the loaded `.ninfer` artifact.

For the Qwen 3.6/3.8 27B `ReasoningEffort` template semantics the default is hard-coded in
`src/targets/qwen3_6/impl/frontend/chat_template.cpp`:

```cpp
PromptCapabilities CompiledChatTemplate::capabilities() const noexcept {
    PromptCapabilities result;
    result.enable_thinking = true;
    if (semantics_ == ChatTemplateSemantics::ReasoningEffort) {
        result.reasoning_effort.low            = true;
        result.reasoning_effort.medium         = true;
        result.reasoning_effort.xhigh          = true;
        result.reasoning_effort.default_effort = ReasoningEffort::XHigh;  // <-- the default
    }
    return result;
}
```

The template render path applies the same fallback a second time:
`options.reasoning_effort.value_or(ReasoningEffort::XHigh)` (line ~355).

The documentation agrees:

- `docs/serving.md`: "omitting effort uses that template's declared default."
- `docs/cli.md` (CLI app): `--reasoning-effort low|medium|xhigh` — "omitting the option uses the
  template default."

**Therefore the default reasoning effort for the registered Qwen 27B templates is `xhigh`.**

Template behavior detail: for this template, `low` and `xhigh` each append a special
instruction line to the prompt ("keep your thinking brief..." / "think carefully...");
`medium` appends nothing — it is the plain, unmodified template.

## 3. How to get `medium`: set it in the request body

The server parses and validates the effort per request:

- Shared parser/validator: `parse_requested_reasoning_effort()` in `src/serve/request.h`
  (accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`).
- OpenAI Chat Completions: `src/serve/openai_chat_request.cpp` (~line 837)
- OpenAI Responses: `src/serve/openai_responses_request.cpp` (~line 930)
- Anthropic Messages: `src/serve/anthropic_messages_request.cpp` (~line 892)

### OpenAI Chat Completions — `POST /v1/chat/completions`

```json
{
  "model": "qwen3.8-27b",
  "reasoning_effort": "medium",
  "messages": [{"role": "user", "content": "Hello"}]
}
```

### OpenAI Responses — `POST /v1/responses`

```json
{
  "model": "qwen3.8-27b",
  "reasoning": { "effort": "medium" },
  "input": "Hello"
}
```

### Anthropic Messages — `POST /v1/messages`

```json
{
  "model": "qwen3.8-27b",
  "output_config": { "effort": "medium" },
  "messages": [{"role": "user", "content": "Hello"}]
}
```

Semantics:

- `"none"` disables thinking for the request.
- `"low"`, `"medium"`, `"xhigh"` are the efforts exposed by the registered templates.
- `"minimal"`, `"high"`, `"max"` parse but are rejected with
  `reasoning_effort_not_supported` for these templates.
- A contradictory combination of `reasoning_effort` and `enable_thinking` returns
  `conflicting_template_option`.

The local CLI app has the equivalent flag: `ninfer-cli ... --reasoning-effort low|medium|xhigh`.

## 4. If a process-wide default of `medium` is required

The server deliberately has no startup override for effort (contrast with
`--default-max-tokens` / `--default-thinking-budget`). Options:

1. **Thin proxy (recommended, no rebuild):** run a small reverse proxy in front of
   `ninfer-serve` that injects `reasoning_effort: "medium"` (or the protocol-specific spelling)
   into any inbound request that does not already specify an effort.
2. **Code change:** patch `default_effort` to `ReasoningEffort::Medium` in
   `src/targets/qwen3_6/impl/frontend/chat_template.cpp` (and the matching `value_or` fallback),
   then rebuild. Caveats: applies to every artifact using that template semantics and diverges
   from upstream.

## Key source references

| File | What it shows |
|---|---|
| `src/serve/serve_options.cpp` / `.h` | Complete startup flag set; no effort flag |
| `src/serve/translate.cpp` (~113) | Request effort → template default resolution |
| `src/targets/qwen3_6/impl/frontend/chat_template.cpp` (355, 429–433) | `default_effort = XHigh`; `value_or(XHigh)`; per-effort prompt instructions |
| `src/serve/request.h` (130–160) | Accepted effort strings |
| `src/serve/openai_chat_request.cpp` (837), `openai_responses_request.cpp` (930), `anthropic_messages_request.cpp` (892) | Per-protocol effort parsing/validation |
| `docs/serving.md` (~177, 202–209, 401), `docs/cli.md` (~42, 204) | Documented default behavior |

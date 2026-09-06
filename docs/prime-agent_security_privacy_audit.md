# prime-agent — Security & Data-Privacy Audit

**Date:** 2026-09-06
**Repo:** https://github.com/PrimeIntellect-ai/prime-agent (checked out at `/v1.7/tmp/prime-agent`)
**Commit audited:** `a062ed2` ("chore(ai): bump impersonated Claude Code and Copilot client versions")
**Package audited:** `packages/coding-agent` (TypeScript, Node.js)

---

## Verdict

**The library is acceptable for use in a privacy-sensitive deployment — provided two
defaults are addressed:**

1. **Product analytics telemetry is ON by default.** It is low-sensitivity
   (no prompt text, no code, no file paths — only counters, durations, token
   totals, and coarse OS/model categories), but it still phones home to
   `api.primeintellect.ai` and should be disabled if you want a zero-egress
   guarantee.
2. **Agent-trace upload is OFF by default, but if it is ever enabled (requires
   a Prime login) it uploads your FULL session transcripts — prompts, code,
   tool outputs — to Prime's backend.** Ensure it stays off and that no Prime
   credentials are present.

With the hardening steps in §5 applied, the only network traffic the agent
performs is (a) model inference to the endpoint you configure and (b)
optional, user-initiated downloads (extension installs). At the network
layer, this can be reduced to **localhost-only** for a fully air-gapped
workflow.

No keyloggers, no clipboard access, no environment-variable or credential
exfiltration, no obfuscated payloads, no third-party analytics SDKs, no
remote code loading from telemetry paths, and no code that transmits file
contents or workspace paths were found.

---

## 1. Audit method

- Full read of the two privacy-relevant modules:
  `src/core/telemetry.ts` (794 lines) and `src/core/agent-traces.ts` (~1,000 lines),
  plus `src/core/settings-manager.ts`, `src/core/prime-inference-auth.ts`,
  `src/utils/version-check.ts`, `src/utils/tools-manager.ts`,
  `src/modes/daemon/daemon-mode.ts`.
- Exhaustive grep of every `fetch`/network call site in the package (non-test):
  telemetry client, trace uploader, version check, extension/tool downloads,
  model-registry fetch.
- Inspection of every telemetry event property (all five event names) to confirm
  nothing carries prompt text, code, or filesystem paths.
- Trace upload payload composition verified against `prepareAgentTraceUpload`
  (reads the full session file, base64, chunked upload to Prime API).
- Default values of all settings verified in `settings-manager.ts`.

## 2. Inventory of every network egress point

### 2.1 Product analytics (telemetry) — **ON by default**

- **Endpoint:** `https://api.primeintellect.ai/api/v1/agent-analytics/events`
  (`DEFAULT_TELEMETRY_ENDPOINT`, `telemetry.ts:13`). **Hard-coded; not
  overrideable by env var.**
- **Trigger:** every agent session (`agent started`, `agent command used`,
  `agent run completed`, `agent session ended`) and one-time `onboarding
  completed`.
- **Transport:** batches of 10 events, flushed every 10 s or on exit, via
  Node `fetch`, 1.5 s timeout. **Failures are swallowed silently**
  (`catch {}` in `TelemetryClient.send`) — telemetry never affects agent
  operation, and there is no crash-report side channel.
- **Identifier:** a `installation_id` (random UUID persisted locally in
  `~/.agent/telemetry.json`) + per-session random UUID. No hostname, no
  username, no home directory, no git identity.
- **Exact fields sent (per event, verified in source):**
  - Common: `version` (agent version), `os_family`, `architecture`,
    `install_method` (e.g. npm/standalone), `execution_mode` (interactive/
    cli/daemon), `session_id` (random UUID).
  - `agent started`: nothing else.
  - `agent command used`: `command_name` (slash-command name).
  - `agent run completed`: `outcome` (success/error/aborted), durations in ms
    (total, TTFT, first model event, max model latency), `model_call_count`,
    `turn_count`, `tool_call_count`, `tool_error_count`, token counters
    (input/output/cache-read/cache-write/total), `compaction_count`,
    `retry_count`, `provider_category` (coarse bucket: anthropic/openai/
    google/prime/openrouter/bedrock/vertex/mistral/groq/xai/custom),
    `model_category` (coarse bucket: claude/gpt/o1/o3/o4/gemini/glm/kimi/
    qwen/deepseek/llama/mistral/custom), `error_category` (one of:
    authentication, rate_limit, overloaded, network, context_limit,
    server, unknown, null).
  - `agent session ended`: aggregate counters only (duration, run/prompt/tool
    counts, usage totals).
  - `onboarding completed`: outcome + `auth_category` (e.g. `api_key`,
    `environment`, `stored`) — which *kind* of credential you use, not the
    credential.
- **NOT sent:** prompt text, code, file contents, file paths, cwd, git repo
  name/commit, environment variables, API keys, user/hostname, IP-identifying
  headers (server can still see your public IP — unavoidable for any HTTPS
  egress), per-model strings (only category buckets).
- **Gate (all three must pass):** `isTelemetryEnabled` =
  `globalSettings.telemetry.enabled && projectSettings.telemetry.enabled &&
  runtimeOverrides.telemetry.enabled`, each **defaulting to `true`**, plus an
  env-var check: `PI_TELEMETRY` in `["0","false","no","off"]` disables
  unconditionally (an explicit on-value is also respected as an override).
- **Risk:** low. Metadata about your usage (volume, which model family,
  success rate) is attributed to a random per-install ID and visible to
  PrimeIntellect. No content. Public IP is exposed as with any HTTPS call.

### 2.2 Agent-trace upload (session transcripts) — **OFF by default**

- **Endpoint:** `https://api.primeintellect.ai/api/v1/agent-traces/sessions/{id}`
  (base URL from `PRIME_AGENT_TRACES_BASE_URL` env, default
  `https://api.primeintellect.ai`).
- **What is sent if enabled:** the **entire session transcript** — full
  prompt/response text, tool outputs, code — read from the session file,
  base64-encoded, chunked upload. This is the single biggest privacy surface
  in the codebase.
- **Gate:** requires (a) `agentTraces.enabled` setting = `true` (**default
  `false`**, `settings-manager.ts:841`), AND (b) a credential in priority
  order: `PRIME_AGENT_TRACES_API_KEY` env → stored
  `prime-agent-traces` API key → `PRIME_API_KEY` env → stored Prime
  Inference key. Without a credential the upload silently does not happen.
- **Trigger:** installed on every session via
  `installAgentTraceUpload` (`agent-session-services.ts:226`); the actual
  upload is gated by the checks above, plus per-file checks (empty session,
  size cap).
- **Mitigation:** do not enable it; do not store a Prime credential in
  `~/.agent/` if you never intend to upload; unset the two env vars.

### 2.3 Version check — ON by default, benign

- **Endpoint:** `https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev`
  (Cloudflare R2 release manifest; base overridable via
  `PRIME_AGENT_DOWNLOAD_BASE_URL`).
- **Sent:** your public IP, `User-Agent: prime-agent/<version>`, `accept`
  header. Nothing else.
- **Gate:** skipped if `PI_SKIP_VERSION_CHECK` or `PI_OFFLINE` set.
- **Risk:** trivial (reveals you run prime-agent + version to whoever owns
  that bucket).

### 2.4 Session "share" (slash command) — **manual, user-initiated only**

- If the user runs the share command, a (code-verified) redacted transcript
  is posted to a **GitHub Gist** via `api.github.com`, and a link of the form
  `https://pi.dev/session/#<gistId>` (viewer base overridable via
  `PI_SHARE_VIEWER_URL`) is produced.
- **Risk:** only if you use the feature. If your org forbids GitHub Gists or
  `pi.dev`, simply don't use the command (and you can block both domains at
  the network layer, §5).

### 2.5 Extension / tool installs — only if you install extensions

- `tools-manager.ts` fetches from `api.github.com/repos/.../releases/latest`
  and downloads release artifacts, running a version check on the installed
  binary. This is a normal "install plugin" flow; it does not happen unless
  you explicitly add extensions.
- **Security note:** extensions are third-party code run with your agent's
  privileges — review before installing. Unrelated to data leakage.

### 2.6 Model inference — your traffic, your choice

- All model calls go to the base URL you configure (for your deployment:
  `http://127.0.0.1:8080`). If you point it at a local endpoint, **no
  inference data ever leaves the machine**. This is the dominant data flow
  and it is entirely under your control.

### 2.7 Local-only sinks (no egress)

- Daemon crash handlers (`daemon-mode.ts`) write stack traces to the local
  rotating daemon log and stderr only.
- Structured logs, session files, `telemetry.json` queue: all on local disk
  under `~/.agent/` (or configured agent dir).

## 3. What the code does NOT do (negative findings)

| Concern | Finding |
|---|---|
| Prompt / code / file-content exfiltration | **None.** Only the opt-in trace upload (§2.2) ever sends transcript content. |
| Env-var / credential exfiltration | **None.** No code reads `process.env` values into any outbound payload (it reads only its own config vars: `PI_*`, `PRIME_*` gates/keys). |
| Third-party analytics SDKs | **None.** Single hand-rolled fetch-based client; no `analytics`, `posthog`, `segment`, `sentry`, `mixpanel` dependencies. |
| Remote code execution / dynamic loading from network in telemetry paths | **None.** No `eval`/`Function`/`import()` of fetched content. |
| Obfuscated or base64 payloads in the analytics channel | **None** (plain JSON). Base64 appears only in the opt-in trace uploader (needed for chunking). |
| Hostname / username / home-dir / git-identity in any payload | **None.** (git repo/commit appear only inside the trace upload object, which is the full transcript you opt into.) |
| Crash/exception reporting service | **None.** Errors stay in local logs. |
| Telemetry on failure retry loops / data amplification | Bounded: fixed batch size 10, 10 s interval, 1.5 s request timeout, in-memory queue. |

## 4. Residual caveats

1. **Your public IP is exposed to `api.primeintellect.ai` and the R2 bucket**
   while telemetry/version-check are enabled. If even IP correlation must be
   avoided, disable them (§5) — after which, with a local model endpoint and
   no extensions, the machine can run with zero external egress.
2. **Model/provider category + usage volume** are learnable by PrimeIntellect
   while telemetry is on (aggregate, per random install ID, no content).
3. **Extensions, MCP servers, and skills you install** are third-party code
   with full local privilege; they are outside this audit's codebase review.
   Any of them could in principle exfiltrate data. Vet them.
4. **The `share` command** sends to GitHub Gists; treat it as "publishes to
   the internet" even though it is redacted.
5. This audit is of source at commit `a062ed2`. **Re-verify after major
   updates** (the two files to diff: `src/core/telemetry.ts`,
   `src/core/agent-traces.ts`).

## 5. Hardening runbook

### A. In-library settings (edit `~/.agent/settings.json`)

```json
{
  "telemetry": { "enabled": false },
  "agentTraces": { "enabled": false }
}
```

- Per-project override (optional): same two keys in
  `<project>/.agent/settings.json`. All three levels (global / project /
  runtime) AND together, so one `false` anywhere disables analytics.
- Verify: `grep -E '"enabled"' ~/.agent/settings.json` → both `false`.

### B. Environment variables (export in your shell profile / container env)

```bash
# Kill product analytics outright (overrides settings)
export PI_TELEMETRY=off

# Kill the version-check phone-home
export PI_SKIP_VERSION_CHECK=1

# Belt-and-suspenders: the offline switch (also skips version check)
export PI_OFFLINE=1

# Ensure no trace-upload credential can resolve
unset PRIME_AGENT_TRACES_API_KEY PRIME_API_KEY PRIME_AGENT_TRACES_BASE_URL
```

### C. Credentials hygiene

```bash
# Remove any stored Prime credentials so the trace uploader has no key to use
# (back up first if you ever want them):
ls ~/.agent/            # inspect; the auth store lives here
# delete/blank the prime-agent-traces and prime-inference entries in the
# auth storage file, e.g.:
rm ~/.agent/auth.json   # (adjust to the actual filename found in ~/.agent/)
```

### D. Network layer — outside the library (the strongest guarantee)

This is the "beyond the env vars" part: enforce the policy in the kernel so
**no version of prime-agent (or a compromised extension) can exfiltrate**,
regardless of what the code decides to do.

**1) Blocklist the known egress destinations (simple):**

```bash
# On Debian/Ubuntu with iptables (run as root):
iptables -A OUTPUT -d api.primeintellect.ai -j DROP
iptables -A OUTPUT -d *.r2.dev -j DROP
# If you will never use the share command:
iptables -A OUTPUT -d api.github.com -j DROP
iptables -A OUTPUT -d github.com -j DROP
iptables -A OUTPUT -d pi.dev -j DROP
# Persist: apt install iptables-persistent && netfilter-persistent save
```

(Caveat: blocking `*.r2.dev` and `api.github.com` also blocks any future
prime-agent feature that uses them, and any extension you install that needs
GitHub. That is the price of certainty — see option 2 for allowlisting.)

**2) Allowlist mode (tighter): only your model endpoint + localhost**

For a fully self-hosted deployment (model on 127.0.0.1:8080), you can drop
*all* outbound traffic from the agent's user and re-allow only what you need:

```bash
useradd -m primeuser   # run the agent as a dedicated user, not root
# As root:
iptables -A OUTPUT -m owner --uid-owner primeuser -o lo -j ACCEPT   # local model
iptables -A OUTPUT -m owner --uid-owner primeuser -j DROP           # nothing else
# Then re-allow ONLY what a given task needs, e.g.:
#   - your inference host if the model is not local:
#   iptables -I OUTPUT -m owner --uid-owner primeuser -d <model-host> -p tcp --dport 443 -j ACCEPT
#   - DNS, if you need it:
#   iptables -I OUTPUT -m owner --uid-owner primeuser -p udp --dport 53 -j ACCEPT
```

This gives the property: **even if prime-agent's code or an extension is
later found to leak data, the kernel will not deliver it anywhere except the
hosts you explicitly allow.**

**3) Sinkhole via DNS (defense in depth, for multi-container hosts):**

```bash
# /etc/hosts entries (or a DNS-level block in your container network)
0.0.0.0 api.primeintellect.ai
0.0.0.0 pub-728493de92a943e2a9b2d17b4719f318.r2.dev
```

**4) Container / host network policy (if running in Docker/K8s):**

- Run the agent container with a custom egress firewall (e.g. `iptables` on
  the host, or Cilium/NetworkPolicy in K8s) that matches the rules above.
- For Docker: `docker run --network mynet` where `mynet` has a route only to
  the inference host, or use `--network host` + the iptables owner rules.

**5) Verification (post-hardening proof):**

```bash
# While a prime-agent session runs, watch for any egress attempts:
sudo tcpdump -ni any host api.primeintellect.ai or host r2.dev &
# or with auditd:
sudo auditctl -a always,exit -F dir=$HOME/.agent -p wa
# Expected after steps A–D: zero packets to the blocked hosts, and (allowlist
# mode) egress only to the model endpoint.
```

**6) Ongoing:**

- Keep the agent in a dedicated non-root user / container (§D.2) so a
  compromised extension can't read other users' secrets.
- Store API keys for *local* inference only; do not put cloud provider keys
  in the agent's environment if the model is local.
- After any prime-agent update, re-run the diff check:
  `git diff a062ed2..HEAD -- packages/coding-agent/src/core/telemetry.ts packages/coding-agent/src/core/agent-traces.ts`
  and re-scan for new `fetch(` sites:
  `grep -rn "fetch(" packages/coding-agent/src --include=*.ts | grep -v test`.

## 6. Bottom line

- **Analytics telemetry:** on by default, off via one setting + one env var;
  content-free payload; safe to keep on if you accept usage metadata, disable
  if you don't.
- **Trace upload:** off by default; only fires with an explicit setting AND a
  Prime credential; keep both absent.
- **Version check / share / extension installs:** trivial or user-initiated;
  coverable by network rules.
- **With settings + env + kernel-level egress policy:** the deployment can be
  made to have *provable* zero-egress to anyone except your own model
  endpoint. **Recommended: yes, safe to use.**

# Alfred Voice Channel — Local Setup (Fedora + XFCE)

Step-by-step setup for the voice channel on a **Fedora laptop running XFCE**,
talking to the nbchat server.

## How it works

```
 ┌──────────────────────── Fedora laptop (XFCE) ─────────────────────────┐
 │                                                                       │
 │  mic ──► faster-whisper (local STT) ──► POST /voice (text only)       │
 │                                                           │           │
 │  speakers ◄── aplay ◄── Piper TTS ◄── GET /voice/stream ◄┤           │
 │        (Alfred speaks verified events: received, started,            │
 │         complete, failed, interrupted, status, model <voice> tags)   │
 └───────────────────────────────────│───────────────────────────────────┘
                                     │  SSH tunnel
                    ssh -L 8765:127.0.0.1:8765 you@server
 ┌───────────────────────────────────▼───────────────────────────────────┐
 │  Server:  python -m nbchat.tui --voice                                │
 │   └─ VoiceBridge (FastAPI+uvicorn, 127.0.0.1:8765, in-process)        │
 │        POST /voice        transcript  → TUI agent (as a user turn)    │
 │        GET /voice/stream  SSE events  → from the verified event bus   │
 │        GET /health        liveness + subscriber count                 │
 └───────────────────────────────────────────────────────────────────────┘
```

Only **text** crosses the tunnel — audio never leaves the laptop. The bridge
binds to `127.0.0.1` on the server, so the SSH tunnel is the only entry point.

---

## Part 1 — Server side (once)

1. Make sure voice is enabled. `repo_config.yaml` on the server already has:

   ```yaml
   voice_enabled: true
   voice_port: 8765
   ```

   If you change `voice_port`, use the same number in every laptop command
   below.

2. Start (or restart) the TUI **with** the voice flag:

   ```bash
   python -m nbchat.tui --voice
   ```

   You should see a line like:

   ```
     voice   Alfred bridge ACTIVE on 127.0.0.1:8765 (ssh -L 8765:127.0.0.1:8765 user@server)
   ```

   (`--voice` is optional when `voice_enabled: true`, but keeps the intent
   explicit.)

3. Quick self-check from the server itself:

   ```bash
   curl -s http://127.0.0.1:8765/health
   # {"ok":true,"subscribers":0}
   ```

---

## Part 2 — Laptop system packages (Fedora, XFCE)

On the laptop, as your user:

```bash
sudo dnf update
sudo dnf install python3 python3-pip git \
                 portaudio portaudio-devel \
                 alsa-utils pulseaudio-utils espeak-ng \
                 xterm
```

What each one is for:

| package          | why |
|------------------|-----|
| `portaudio`      | mic capture via `sounddevice` |
| `portaudio-devel`| only needed if `sounddevice` builds a wheel from source |
| `alsa-utils`     | `aplay` — plays Piper's raw PCM output |
| `pulseaudio-utils`| `pactl` — for checking audio devices in the diagnostics below |
| `espeak-ng`      | fallback speech (and handy for a quick TTS test) |
| `xterm`          | terminal for running the client under XFCE autostart (Part 5) |

Verify the microphone is visible:

```bash
arecord -l                     # list capture hardware
pactl list sources short       # list Pulse/PipeWire source devices
```

If the list is empty, the mic (or USB headset) isn't attached or is in a
bad port — fix that before going further.

---

## Part 3 — Laptop Python environment

1. Clone the repo (or add the server as a git remote if you already have it):

   ```bash
   git clone <your-nbchat-remote> ~/nbchat
   cd ~/nbchat
   ```

2. Create a venv and install the laptop-client dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install sounddevice faster-whisper requests
   ```

   - `sounddevice` — mic capture (needs PortAudio from Part 2)
   - `faster-whisper` — local speech-to-text (CPU, int8)
   - `requests` — talks to the bridge over the tunnel

   The first STT run auto-downloads the Whisper model (`small` by default,
   ~460 MB) to `~/.cache/huggingface`. That download happens once, on the
   first recording.

---

## Part 4 — Piper (text-to-speech)

1. Create a home for Piper and its voice:

   ```bash
   mkdir -p ~/.local/share/alfred/voices
   cd ~/.local/share/alfred
   ```

2. Download the Piper binary (x86_64 Linux). This URL is verified working:

   ```bash
   curl -LO https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz
   tar xzf piper_linux_x86_64.tar.gz        # yields ./piper
   chmod +x piper
   ```

   (For a newer build, browse the [piper releases page](https://github.com/rhasspy/piper/releases)
   and pick the latest `piper_linux_x86_64.tar.gz` asset.)

3. Download a voice — the default the client looks for is the composed,
   dry-witted British voice `en_GB-alan-medium`:

   ```bash
   cd ~/.local/share/alfred/voices
   curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx
   curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json
   ```

   The `.onnx.json` must sit next to the `.onnx`.

4. Test Piper end-to-end (should speak "Hello from Alfred" through your
   speakers):

   ```bash
   echo "Hello from Alfred." | \
     ~/.local/share/alfred/piper --model ~/.local/share/alfred/voices/en_GB-alan-medium.onnx --output-raw | \
     aplay -q -r 22050 -f S16_LE -c 1
   ```

5. Tell the client where Piper lives — add to `~/.bashrc`:

   ```bash
   export ALFRED_PIPER_BIN=$HOME/.local/share/alfred/piper
   export ALFRED_PIPER_VOICE=$HOME/.local/share/alfred/voices/en_GB-alan-medium.onnx
   # optional: ALFRED_WHISPER_MODEL=small   # tiny | base | small | medium
   ```

   Then `source ~/.bashrc`.

---

## Part 5 — SSH tunnel

The laptop reaches the bridge at `127.0.0.1:8765`, which only works while an
SSH tunnel forwards that port to the server's localhost.

1. Put your normal SSH settings in `~/.ssh/config`:

   ```
   Host nbchat-server
       HostName <server-ip-or-hostname>
       User <your-server-user>
       # IdentityFile ~/.ssh/id_ed25519   # uncomment if you use a key
       LocalForward 8765 127.0.0.1:8765
   ```

2. Open the tunnel (leave this shell running, or add `-fN` to background it):

   ```bash
   ssh -N nbchat-server          # or: ssh -fN nbchat-server
   ```

3. Verify from the laptop:

   ```bash
   curl -s http://127.0.0.1:8765/health
   # {"ok":true,"subscribers":0}
   ```

   If this prints JSON, the tunnel, the bridge, and the laptop are all
   wired up correctly.

---

## Part 6 — Run Alfred and use it

```bash
cd ~/nbchat
source .venv/bin/activate
python -m nbchat.voice.laptop_client --port 8765
```

You should see:

```
[alfred] connected to http://127.0.0.1:8765
[alfred] Enter to start recording, Enter again to stop & send, Ctrl-C to quit
[alfred] >
```

Usage:

1. Press **Enter** — recording starts from your default mic.
2. Speak your command (e.g. *"Remind me to water the plants"*).
3. Press **Enter** again — it stops, transcribes locally, and sends the text.

You will then hear Alfred on the laptop:

- immediately: *"Very well, sir. I'm on it."* (verified `received`)
- when the turn starts: *"Underway, sir."* (`started`)
- on completion: *"All done, sir."* (`complete`) — or the failure /
  interrupted lines if that's what actually happened
- plus any `<voice>` lines the assistant chooses to speak (e.g.
  *"The digest is ready, sir."*), and periodic supervisor status updates
  when `--supervisor` is on and a turn is in flight.

Meanwhile the server-side TUI shows the transcript as a normal user turn:

```
  ♪ [voice] Remind me to water the plants
```

### Server-side sanity checks (from the laptop, over the tunnel)

```bash
curl -s http://127.0.0.1:8765/health          # liveness + subscriber count

curl -N -s http://127.0.0.1:8765/voice/stream # watch the raw SSE stream
                                             # (Ctrl-C to stop)

curl -s -X POST http://127.0.0.1:8765/voice \
     -H 'Content-Type: application/json' \
     -d '{"text":"Alfred, ping the terminal"}'
# {"ok":true}   ->  note: this injects a real turn into the TUI
```

---

## Part 7 — Start automatically with XFCE (optional)

The push-to-talk fallback reads **Enter from stdin**, so the client must run
inside a terminal. The cleanest XFCE setup is one autostart entry that opens
a terminal doing the tunnel + client together:

1. `xfce4-settings-editor` → **Autostart** → **Add**, or create the file
   `~/.config/autostart/alfred.desktop` directly:

   ```ini
   [Desktop Entry]
   Type=Application
   Name=Alfred voice client
   Comment=SSH tunnel + nbchat voice client
   Exec=xterm -hold -e bash -lc 'ssh -fN nbchat-server; cd ~/nbchat && .venv/bin/python -m nbchat.voice.laptop_client --port 8765'
   Terminal=false
   ```

2. Log out/in (or the next session) and a small `xterm` appears with Alfred
   waiting at the `[alfred] >` prompt.

Tips:

- The `xterm -hold` keeps the window open if the client exits, so you can
  read the error.
- If you prefer a different key than Enter for push-to-talk, that is a
  code-level upgrade (`xbindkeys` / `pynput`); the Enter fallback is
  deliberately dependency-free.
- To change the default input mic, set `SDL_AUDIODRIVER`/PulseAudio
  defaults or check with:

  ```bash
  .venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"
  ```

---

## Troubleshooting

| symptom | fix |
|---|---|
| `curl 127.0.0.1:8765` fails on the laptop | tunnel not up — `ssh -N nbchat-server` (or check the `LocalForward` line in `~/.ssh/config`); confirm the server TUI printed `Alfred bridge ACTIVE` |
| `address already in use` on the laptop | another Alfred client (or stale tunnel) holds 8765: `ss -ltnp \| grep 8765`, kill the old one |
| `No module named 'sounddevice'` | you're not in the venv — `source .venv/bin/activate` |
| mic not found | `arecord -l` / `pactl list sources short`; check the headset port; try `ALSA_DEFAULT_INPUT` once the device is visible |
| no sound from speakers | test Piper directly (Part 4 step 4); check `pactl get-sink-volume @DEFAULT_SINK@` and that the output isn't muted |
| client prints `stream error … retrying` | the stream dropped (server TUI restarted, tunnel flapped) — it reconnects automatically with backoff; make sure the tunnel command includes the forward |
| first recording takes a long time | one-time Whisper model download; subsequent recordings start in ~1 s |
| TTS silent but everything else works | Piper binary or voice missing — set `ALFRED_PIPER_BIN` / `ALFRED_PIPER_VOICE` (Part 4 step 5); the client deliberately degrades to silence rather than crashing |
| Alfred says nothing at all | the model was asked to stay silent for trivial answers by design; also confirm the event actually happened on the server (watch `curl -N /voice/stream` while you work in the TUI) |

## What is *not* configured by this doc

- **Email bridge** (`--email`) needs the `GHG_APP_PASSWORD` env var on the
  server; voice does not need it.
- **Supervisor voice status** needs `--supervisor` on the TUI; the event is
  then grounded in a live `gather_state()` snapshot and rate-limited by
  `voice_status_min_interval` (300 s default).

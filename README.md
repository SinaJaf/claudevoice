# claudevoice

Voice for [Claude Code](https://claude.com/claude-code): a one-on-one spoken conversation with Claude,
right inside the chat. You talk, press **Done**, Claude answers out loud with
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (a natural local voice), and the mic opens
again by itself for your next turn. No clicking the mic on and off between turns.

```
Auto · Read aloud · ⚙ · ● Call               ← idle, in the prompt footer
Done · Mute · ● listening ⚙ · End call       ← your turn
Stop · ▃▅▇▅ speaking ⚙ · End call            ← Claude talking
```

How a call goes:

1. **● Call** starts it. Claude says it's listening.
2. Talk. Pause and think as long as you like: nothing is sent until you press **Done**.
3. Claude's reply is read aloud, then the mic opens again on its own. Back to step 2.

- **Mute** pauses the mic: nothing is recorded while muted, what you already said is kept, and
  **Unmute** carries on the same turn.
- **Stop** cuts Claude's voice; the mic opens right away.
- **End call** stops the mic and the voice. It never cancels work Claude is doing.
- Only the buttons you can use right now are shown; a small live indicator beside the gear says
  what is happening (listening, muted, writing, thinking, speaking).
- **Read aloud** (outside a call) speaks the last reply once.
- **Listening** uses the system's own speech recognizer, which keeps your words as you said them:
  on a Mac the engine behind macOS dictation (on-device), on Windows the engine behind voice typing.
  Each asks for permission once. On Linux it uses Whisper locally; OpenAI's cloud transcription is
  an option everywhere.
- Switch headphones, speakers or microphones whenever you like: the voice and the mic follow the
  system's current devices, even mid-turn.
- **⚙** settings: the plugin on/off switch, listening engine, microphone, voice and speed. Off leaves
  only the gear, so you can turn it back on.
- **Per-chat settings**: under each setting, open **▸ this chat** to choose a different value for the
  chat you're in. Pick **Same as all chats** to go back to the shared setting.

## Install

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 and the packages on first use)
- `espeak-ng`, Kokoro's pronunciation fallback: `brew install espeak-ng` (macOS),
  `sudo apt install espeak-ng libportaudio2` (Debian/Ubuntu), `winget install eSpeak-NG.eSpeak-NG`
  (Windows)

Then, at the prompt of a Claude Code terminal session:

```
/plugin install claudevoice --marketplace SinaJaf/claudevoice
```

Answer `y` to add the marketplace and pick the user scope. In the desktop app, click **+** next to
the prompt, then **Plugins**, **Add marketplace** with `SinaJaf/claudevoice`, then **Add plugin**.
Open sessions pick it up with `/reload-plugins`; new sessions load it on their own.

The first Read aloud or Call sets up the Python environment and downloads the Kokoro voice
model (~330 MB), so expect a few minutes once. On a Mac the first call also builds the small speech
helper (needs the Xcode command line tools: `xcode-select --install`). The footer says what it
is doing. Everything lives under the plugin folder and `~/.claudevoice/`.

## How it works

```
Claude Code (any surface)
  prompt footer controls  ──$.http──▶  claudevoice server (Kokoro, warm, 127.0.0.1:8765)
  ● Call: mic ─▶ Done ─▶ speech-to-text ─▶ prompt ─▶ reply ─▶ speak ─▶ mic   └─ speakers
```

- `hooks/register.tsx` is the Claude Code plugin: the footer controls, the settings dialog, the
  call loop (a detached listener per turn, `$.prompt.submit`, `turn.complete`), and a spoken-style
  note Claude receives during calls. It also routes the app's built-in speech calls through the chosen speaking engine.
- `claudevoice/` is the Python side, run through the project's `uv` environment:
  `tts_server.py` keeps Kokoro loaded and plays audio, following the system's output device;
  `clean.py` turns
  markdown into speakable text (code fences become "python code block, 12 lines", paths collapse
  to file names, `app.py:42` reads as "app.py line 42").
- `listen.py` records one turn until Done (Mute pauses it) and transcribes it; `record.py` follows
  the system's input device; `stt.py` picks the engine: `apple/ClaudeVoiceSTT.swift`, a tiny signed
  helper app running the Mac's on-device recognizer (macOS grants speech recognition only to an
  app that declares it), `windows/dictate.ps1` driving Windows' recognizer from the built-in
  Windows PowerShell (it listens to the default mic itself, phrase by phrase, until Done), Whisper (mlx-whisper on Apple Silicon, faster-whisper elsewhere), or a
  cloud endpoint.
- Cloud voices are OpenAI-compatible endpoints (OpenAI, Groq, a local server) and ElevenLabs. Keys
  are read from `OPENAI_API_KEY` / `ELEVENLABS_API_KEY` in your environment or login shell; nothing
  secret is written to disk. If a cloud voice can't be used, Kokoro says why and reads the reply.

## Settings

The gear covers the common ones. Everything is in `~/.claudevoice/config.json`, also editable with
`uv run --project <plugin folder> python -m claudevoice config KEY VALUE`:

| key | default | meaning |
|-----|---------|---------|
| `voice` | `af_heart` | Kokoro voice, or a comma blend (`af_heart,af_bella`); see the model's VOICES.md |
| `lang_code` | `a` | `a` American, `b` British (Kokoro) |
| `speed` | `1.05` | playback pace |
| `stt_engine` | `auto` | `apple` (Mac, on-device), `windows` (Windows), `local` (Whisper), `openai`; `auto` picks the system's own recognizer, Whisper on Linux |
| `stt_language` | `` | language for the apple and windows engines (`en-US`, `fa-IR`...); empty follows the system |
| `whisper_model` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, `large-v3` |
| `input_device` | `` (system default) | microphone name; empty follows the system's input as it changes |
| `tts_engine` | `kokoro` | `kokoro`, `openai`, `elevenlabs` |
| `tts_voice`, `tts_model`, `tts_base_url`, `tts_api_key_env` | OpenAI defaults | cloud speech (ElevenLabs voice id goes in `tts_voice`) |
| `max_chars` | `2500` | longest reply read in full |
| `port` | `8765` | local server port |


## Extras (macOS and Linux)

`./install.sh` front-loads the downloads and installs a `claudevoice` command:

- `claudevoice chat`: a terminal speech-to-speech loop around `claude -p` (Whisper → Claude →
  Kokoro, streamed sentence by sentence; needs the `claude` CLI logged in).
- `claudevoice say "text" [--voice bm_george]`, `claudevoice voices`, `claudevoice stop-server`.
- `CLAUDEVOICE_ALWAYS_ON_HOOKS=1 ./install.sh` registers optional Stop/UserPromptSubmit hooks
  that read *every* reply aloud; `claudevoice on` / `off` toggles them.

## Development

```bash
claude plugin validate .      # manifest + hooks module
claude plugin test .          # tests/panel.test.tsx against the engine
```

Edits reach a running session with `/reload-plugins` when the plugin was installed from this folder
(`claude plugin marketplace add <folder>` then `claude plugin install claudevoice@claudevoice`).

## Troubleshooting

- Logs: `~/.claudevoice/server.log` (speech) and `~/.claudevoice/claudevoice.log` (hooks).
- "speech recognition is not allowed": turn on **Claude Voice transcription** in System Settings,
  Privacy & Security, Speech Recognition.
- Windows: "turn on Online speech recognition": Windows Settings, Privacy & security, Speech. The
  windows engine uses the default microphone (Settings, System, Sound, Input); pick Whisper in the
  gear to choose a specific one.
- "The microphone is giving silence": the input carries no signal (lid closed, unplugged, or no
  microphone permission). Pick another microphone in the gear.
- Silent after picking a cloud voice: the key isn't visible to the server. Export it in your shell
  profile and run `claudevoice stop-server` (it restarts on the next use).
- spaCy error "Python int too large to convert to C ssize_t": `preshed` must stay pinned to 3.0.10.
- The plugin runs the Python side with `uv`; GUI apps start with a short PATH, so it looks in
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` and `~/.cargo/bin` too.

## License

MIT. Kokoro-82M weights are Apache-2.0.

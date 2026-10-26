# claudevoice

Voice for [Claude Code](https://claude.com/claude-code): a one-on-one spoken conversation with Claude.
You talk with Claude Code's own microphone (its built-in dictation), and Claude answers out loud
with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), a natural local voice. There is no
separate speech-to-text model: your words stay exactly as Claude's dictation wrote them.

```
Auto · Read aloud · ⚙ · ● Call                               ← idle, in the prompt footer
● your turn · ✓ Done · ■ Stop · ⚙ · ✕ End call               ← on a call
```

- **● Call**: starts the conversation in this chat. Talk with the app's mic button, pause and think
  as long as you like, then press **✓ Done**: it sends what's in the prompt box. Claude's reply is
  read aloud, then it's your turn again. Pressing Enter yourself works too.
- **■ Stop** cuts Claude's voice and hands you the turn; typing into the box does the same.
- **✕ End call** stops the voice. It never cancels work Claude is doing.
- During a call the controls stay in the same places; the one that matters now lights up, and the
  status animates (your turn, thinking, speaking).
- **Read aloud** (outside a call): speaks the last reply once.
- Switch headphones or speakers whenever you like: replies follow the system's current output from
  the next sentence.
- **⚙**: the plugin on/off switch, the voice (Kokoro voices, or OpenAI and ElevenLabs cloud voices)
  and the speed. Off leaves only the gear, so you can turn it back on.
- **Per-chat settings**: under each setting in the gear, open **▸ this chat** to choose a different
  value for the chat you're in. Pick **Same as all chats** to go back to the shared setting.

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
model (~330 MB), so expect a few minutes once. The footer says what it
is doing. Everything lives under the plugin folder and `~/.claudevoice/`.

## How it works

```
Claude Code (any surface)
  prompt footer controls  ──$.http──▶  claudevoice server (Kokoro, warm, 127.0.0.1:8765)
  ● Call: Claude's dictation ─▶ prompt box ─▶ Done ─▶ reply ─▶ speak   └─ speakers
```

- `hooks/register.tsx` is the Claude Code plugin: the footer controls, the settings dialog, the
  call loop (Done reads the prompt box with `$.prompt.read` and sends it; `turn.complete` speaks
  the answer), and a spoken-style note Claude receives during calls. It also routes the app's built-in speech calls through the chosen speaking engine.
- `claudevoice/` is the Python side, run through the project's `uv` environment:
  `tts_server.py` keeps Kokoro loaded and plays audio, following the system's output device;
  `clean.py` turns
  markdown into speakable text (code fences become "python code block, 12 lines", paths collapse
  to file names, `app.py:42` reads as "app.py line 42").
- `listen.py`, `record.py` and `stt.py` (Whisper) serve only the terminal `claudevoice chat`
  extra below; the plugin never records the microphone itself.
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
| `tts_engine` | `kokoro` | `kokoro`, `openai`, `elevenlabs` |
| `tts_voice`, `tts_model`, `tts_base_url`, `tts_api_key_env` | OpenAI defaults | cloud speech (ElevenLabs voice id goes in `tts_voice`) |
| `max_chars` | `2500` | longest reply read in full |
| `port` | `8765` | local server port |

The terminal `claudevoice chat` also reads `stt_engine`, `whisper_model`, `stt_model`,
`input_device`, `silence_seconds` and `vad_threshold`.

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
- Dictation is Claude Code's own: if the mic button types nothing, check the app's microphone
  permission in your system settings.
- Silent after picking a cloud voice: the key isn't visible to the server. Export it in your shell
  profile and run `claudevoice stop-server` (it restarts on the next use).
- spaCy error "Python int too large to convert to C ssize_t": `preshed` must stay pinned to 3.0.10.
- The plugin runs the Python side with `uv`; GUI apps start with a short PATH, so it looks in
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` and `~/.cargo/bin` too.

## License

MIT. Kokoro-82M weights are Apache-2.0.

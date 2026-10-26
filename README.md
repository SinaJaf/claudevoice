# claudevoice

Voice for [Claude Code](https://claude.com/claude-code): Claude reads its replies aloud with
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), and you can hold a spoken call with it,
all from a small set of controls inside the prompt box. Listening and speaking run locally by
default (Whisper and Kokoro on your machine); cloud engines are optional.

```
Auto · Read aloud · ⚙ · ● Call          ← in the prompt footer, under the mic
```

- **Read aloud**: speaks the last reply, once. **Stop** cuts it.
- **● Call**: a spoken conversation inside the current session. Claude listens, sends what you
  said as the next prompt, reads the answer aloud, then listens again. **Talk now** interrupts
  Claude mid-sentence; **End call** hangs up and never cancels work Claude is doing.
- **Hold mode** (default): pause and think as long as you like, press **Done** when you've finished. There is no
  time limit; nothing is sent until you press Done. **Auto mode**: a one-second pause sends what you said.
- **Mute mic** during a call: recording stops at once, the unfinished take is thrown away, and nothing is
  recorded or sent until you press **Unmute mic**.
- Switch headphones, speakers or microphones whenever you like: replies follow the system's current output
  (from the next sentence), and recording follows the system's current input, even mid-sentence.
- **⚙**: the plugin on/off switch, the listening engine (local Whisper in four sizes, or OpenAI's
  cloud transcription), the microphone, the speaking engine (Kokoro voices, OpenAI voices, ElevenLabs), speed, and
  hold/auto. Changes apply immediately. Off leaves only the gear, so you can turn it back on.
- **Per-chat settings**: under each setting in the gear, open **▸ this chat** to choose a different value for the
  chat you're in. Pick **Same as all chats** to go back to the shared setting.

## Install

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 and the packages on first use)
- `espeak-ng`, Kokoro's pronunciation fallback: `brew install espeak-ng` (macOS),
  `sudo apt install espeak-ng libportaudio2` (Debian/Ubuntu), `winget install eSpeak-NG.eSpeak-NG`
  (Windows)
- a microphone; the OS asks for permission the first time you start a call

Then, at the prompt of a Claude Code terminal session:

```
/plugin install claudevoice --marketplace SinaJaf/claudevoice
```

Answer `y` to add the marketplace and pick the user scope. In the desktop app, click **+** next to
the prompt, then **Plugins**, **Add marketplace** with `SinaJaf/claudevoice`, then **Add plugin**.
Open sessions pick it up with `/reload-plugins`; new sessions load it on their own.

The first Read aloud or Call sets up the Python environment and downloads the models (Kokoro
~330 MB, Whisper large-v3-turbo ~1.6 GB), so expect a few minutes once. The footer says what it
is doing. Everything lives under the plugin folder and `~/.claudevoice/`.

## How it works

```
Claude Code (any surface)
  prompt footer controls  ──$.http──▶  claudevoice server (Kokoro, warm, 127.0.0.1:8765)
  ● Call: listener ─▶ Whisper ─▶ prompt ─▶ reply ─▶ speak      └─ speakers
```

- `hooks/register.tsx` is the Claude Code plugin: the footer controls, the settings dialog, the
  call loop (`$.prompt.submit`, `turn.complete`), and a spoken-style note Claude receives during
  calls. It also routes the app's built-in speech calls through the chosen speaking engine.
- `claudevoice/` is the Python side, run through the project's `uv` environment:
  `tts_server.py` keeps Kokoro loaded and plays audio; `listen.py` records one utterance
  (energy-based pause detection, or hold mode) and transcribes it; `stt.py` picks mlx-whisper on
  Apple Silicon and faster-whisper (CTranslate2, CPU or CUDA) elsewhere; `clean.py` turns
  markdown into speakable text (code fences become "python code block, 12 lines", paths collapse
  to file names, `app.py:42` reads as "app.py line 42").
- Cloud engines are OpenAI-compatible endpoints (OpenAI, Groq, a local server) and ElevenLabs. Keys
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
| `stt_engine` | `local` | `local` or `openai` |
| `whisper_model` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, `large-v3` |
| `stt_model`, `stt_base_url`, `stt_api_key_env` | OpenAI defaults | cloud transcription |
| `tts_engine` | `kokoro` | `kokoro`, `openai`, `elevenlabs` |
| `tts_voice`, `tts_model`, `tts_base_url`, `tts_api_key_env` | OpenAI defaults | cloud speech (ElevenLabs voice id goes in `tts_voice`) |
| `input_device` | `` (system default) | microphone name, as `claudevoice devices` lists them; empty follows the system's input as it changes, and a named mic that is unplugged falls back to it |
| `silence_seconds` | `1.2` | pause that ends a recording in auto mode |
| `vad_threshold` | `0.012` | mic energy that counts as speech; raise in noisy rooms |
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
- "The microphone is giving silence": the input carries no signal at all. A MacBook's built-in mic
  is muted with the lid closed, an unplugged USB mic falls back to it, and macOS may not have granted
  the app microphone access. Pick another microphone in the gear, or `claudevoice devices`.
- Silent after picking a cloud voice: the key isn't visible to the server. Export it in your shell
  profile and run `claudevoice stop-server` (it restarts on the next use).
- spaCy error "Python int too large to convert to C ssize_t": `preshed` must stay pinned to 3.0.10.
- The plugin runs the Python side with `uv`; GUI apps start with a short PATH, so it looks in
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` and `~/.cargo/bin` too.

## License

MIT. Kokoro-82M weights are Apache-2.0.

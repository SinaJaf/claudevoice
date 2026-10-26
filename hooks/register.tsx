import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ListenMode, VoicePhase, VoiceSettings } from '../types'

const SERVER = 'http://127.0.0.1:8765'

const PANE = 'claudevoice'
const SETTINGS_PANE = 'claudevoice-settings'
const MODE_STORE_KEY = 'listenMode'

const phase = atom({ plugin: 'claudevoice', key: 'phase' } as const, 'idle' as VoicePhase)
const isCall = atom({ plugin: 'claudevoice', key: 'isCall' } as const, false)
const heard = atom({ plugin: 'claudevoice', key: 'heard' } as const, '')
const note = atom({ plugin: 'claudevoice', key: 'note' } as const, '')
const mode = atom({ plugin: 'claudevoice', key: 'mode' } as const, 'hold' as ListenMode)
const settings = atom({ plugin: 'claudevoice', key: 'settings' } as const, {} as VoiceSettings)
const enabled = atom({ plugin: 'claudevoice', key: 'enabled' } as const, true)
const ENABLED_STORE_KEY = 'enabled'

const SPOKEN_STYLE =
  'Voice call in progress: the user is speaking through a microphone and your final ' +
  'reply is read aloud by a text-to-speech engine. Answer the way a thoughtful colleague ' +
  'would speak: natural, warm, direct, short sentences, a little personality. No markdown, ' +
  'no bullet lists, no headers, no tables. Do not read out code; describe what it does. ' +
  'Keep most replies under 120 words and end with a short question when you need a decision.'

type Engine = EngineInterface

// ---- where things are on this machine ----------------------------------------------

/** The plugin's folder; the Python side (claudevoice/, pyproject.toml) sits beside hooks/. */
function root($: Engine): string {
  return $.plugin.root
}

async function home($: Engine): Promise<string> {
  return (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.'
}

/** ~/.claudevoice/<name>, matching claudevoice/config.py (CLAUDEVOICE_HOME overrides). */
async function flagPath($: Engine, name: string): Promise<string> {
  const dir = (await $.env.get('CLAUDEVOICE_HOME')) ?? `${await home($)}/.claudevoice`
  return `${dir}/${name}`
}

/** uv, wherever the installer put it: GUI apps start with a short PATH. */
async function uv($: Engine): Promise<string> {
  const h = await home($)
  for (const candidate of [`${h}/.local/bin/uv`, '/opt/homebrew/bin/uv', '/usr/local/bin/uv', `${h}/.cargo/bin/uv`, `${h}/.local/bin/uv.exe`]) {
    if (await $.fs.exists(candidate)) return candidate
  }
  return 'uv'
}

let pythonArgv: string[] | null = null

/** The interpreter for the Python side: the project's venv once it exists, else `uv run`. */
async function python($: Engine): Promise<string[]> {
  if (pythonArgv) return pythonArgv
  const r = root($)
  for (const candidate of [`${r}/.venv/bin/python`, `${r}/.venv/Scripts/python.exe`]) {
    if (await $.fs.exists(candidate)) return (pythonArgv = [candidate])
  }
  return (pythonArgv = [await uv($), 'run', '--quiet', '--project', r, 'python'])
}

/** First use on a machine: create the venv and pull the speech packages (a few minutes). */
async function ensurePython($: Engine): Promise<boolean> {
  const r = root($)
  if ((await $.fs.exists(`${r}/.venv/bin/python`)) || (await $.fs.exists(`${r}/.venv/Scripts/python.exe`))) return true
  await update($, note, () => 'installing the voice engine (first time only, a few minutes)…')
  const ran = await $.process.run([await uv($), 'sync', '--project', r], { cwd: r, timeoutMs: 600000 })
  await update($, note, () => '')
  if (ran.exitCode !== 0) {
    const why = ran.stderr.trim().split('\n').pop() ?? ''
    $.ui.toast(`voice setup failed (is uv installed? https://docs.astral.sh/uv/): ${why}`)
    return false
  }
  pythonArgv = null
  return true
}

// ---- talking to the Kokoro server --------------------------------------------

async function health($: Engine): Promise<{ speaking: boolean; last_error?: string } | null> {
  try {
    const r = await $.http.fetch(`${SERVER}/health`)
    return r.ok ? (JSON.parse(r.text) as { speaking: boolean; last_error?: string }) : null
  } catch {
    return null
  }
}

async function ensureServer($: Engine): Promise<boolean> {
  if (await health($)) return true
  if (!(await ensurePython($))) return false
  await update($, note, () => 'starting the voice server (downloads the voice model the first time)…')
  const ran = await $.process.run([...(await python($)), '-m', 'claudevoice', 'start'], { cwd: root($), timeoutMs: 360000 })
  await update($, note, () => '')
  if (ran.exitCode !== 0 || !(await health($))) {
    $.ui.toast('voice server failed to start; see ~/.claudevoice/server.log')
    return false
  }
  return true
}

async function speak($: Engine, text: string): Promise<boolean> {
  if (!(await ensureServer($))) return false
  try {
    const r = await $.http.fetch(`${SERVER}/speak`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, interrupt: true }),
    })
    return r.ok
  } catch {
    return false
  }
}

async function stopSpeaking($: Engine): Promise<void> {
  try {
    await $.http.fetch(`${SERVER}/stop`, { method: 'POST', body: '{}' })
  } catch {
    // server not up: nothing is playing
  }
}

async function touch($: Engine, path: string): Promise<void> {
  try {
    await $.fs.write(path, '')
  } catch {
    // the listener then stops on its own at its cap
  }
}

// ---- the features -----------------------------------------------------------------

/** Read the last reply in this session aloud, once. */
async function readAloud($: Engine): Promise<void> {
  const messages = await $.session.messages()
  const last = [...messages].reverse().find(m => m.role === 'assistant' && m.text.trim() !== '')
  if (!last) {
    $.ui.toast('Nothing to read yet')
    return
  }
  if (!(await speak($, last.text))) return
  await update($, phase, () => 'speaking')
  watchPlayback($)
}

/** Poll the server until playback ends, then go idle or, in a call, listen again. */
function watchPlayback($: Engine): void {
  $.clock.after(300, () => {
    void (async () => {
      const h = await health($)
      if (h?.speaking) {
        watchPlayback($)
        return
      }
      if (h?.last_error) {
        await update($, note, () => h.last_error ?? '')
        $.ui.toast(h.last_error)
      }
      if (await read($, isCall)) {
        void listenOnce($)
      } else {
        await update($, phase, () => 'idle')
      }
    })()
  })
}

/** Record one utterance (until a pause, or until Done in hold mode), transcribe, send. */
async function listenOnce($: Engine): Promise<void> {
  if (!(await read($, isCall))) return
  await update($, phase, () => 'listening')
  const argv = [...(await python($)), '-m', 'claudevoice.listen']
  if ((await read($, mode)) === 'hold') argv.push('--manual')
  const ran = await $.process.run(argv, { cwd: root($), timeoutMs: 600000 })
  if (!(await read($, isCall))) return
  if (ran.exitCode !== 0) {
    await endCall($, `listening failed: ${ran.stderr.trim().split('\n').pop() ?? 'unknown error'}`)
    return
  }
  let text = ''
  let cancelled = false
  let silent = false
  let device = ''
  try {
    const parsed = JSON.parse(ran.stdout.trim().split('\n').pop() ?? '{}') as {
      text?: string; cancelled?: boolean; silent?: boolean; device?: string
    }
    text = (parsed.text ?? '').trim()
    cancelled = parsed.cancelled === true
    silent = parsed.silent === true
    device = parsed.device ?? ''
  } catch {
    text = ''
  }
  if (cancelled) return
  if (silent) {
    // The input carried no signal at all: unplugged, lid closed, or no permission.
    const why = `The microphone "${device}" is giving silence. Check it is connected and allowed, or pick another one in the voice settings.`
    await endCall($, why)
    $.ui.toast(why)
    if (await speak($, 'I cannot hear anything from the microphone. Please check it, or choose another one in the voice settings.')) {
      await update($, phase, () => 'speaking')
      watchPlayback($)
    }
    return
  }
  if (text === '') {
    if ((await read($, mode)) === 'hold') $.ui.toast("I didn't catch any words; still listening")
    void listenOnce($) // silence or noise: keep listening
    return
  }
  await update($, heard, () => text)
  await update($, phase, () => 'thinking')
  await $.prompt.submit({ text })
}

async function startCall($: Engine): Promise<void> {
  if (await read($, isCall)) return
  await update($, isCall, () => true)
  await update($, note, () => '')
  await update($, phase, () => 'loading')
  if (!(await ensureServer($))) {
    await endCall($, '')
    return
  }
  const warm = await $.process.run([...(await python($)), '-m', 'claudevoice.listen', '--warm'], { cwd: root($), timeoutMs: 600000 })
  if (warm.exitCode !== 0) {
    await endCall($, `could not load Whisper: ${warm.stderr.trim().split('\n').pop() ?? ''}`)
    return
  }
  const greeting = (await read($, mode)) === 'hold'
    ? "I'm listening. Take your time, and press Done when you've finished."
    : "I'm listening."
  await speak($, greeting)
  await update($, phase, () => 'speaking')
  watchPlayback($)
}

async function endCall($: Engine, reason: string): Promise<void> {
  await update($, isCall, () => false)
  await update($, phase, () => 'idle')
  await update($, note, () => reason)
  await touch($, await flagPath($, 'listen.stop'))
  await stopSpeaking($)
}

/** Hold mode: finish the recording and send it. */
async function doneTalking($: Engine): Promise<void> {
  if ((await read($, phase)) === 'listening') await touch($, await flagPath($, 'listen.done'))
}

/** Cut Claude off and start listening right away. */
async function talkNow($: Engine): Promise<void> {
  if ((await read($, phase)) === 'speaking') await stopSpeaking($) // watchPlayback then listens
}

async function toggleMode($: Engine): Promise<void> {
  const next: ListenMode = (await read($, mode)) === 'hold' ? 'auto' : 'hold'
  await update($, mode, () => next)
  await $.store.set(MODE_STORE_KEY, next)
  $.ui.toast(next === 'hold' ? 'Hold mode: press Done when you have finished talking' : 'Auto mode: a pause sends what you said')
}

// ---- settings ----------------------------------------------------------------------

const WHISPER_MODELS: readonly [string, string][] = [
  ['small', 'Whisper small (fast)'],
  ['medium', 'Whisper medium'],
  ['large-v3-turbo', 'Whisper large v3 turbo (default)'],
  ['large-v3', 'Whisper large v3 (most accurate)'],
]
const KOKORO_VOICES: readonly [string, string][] = [
  ['af_heart', 'Heart (US, warm)'], ['af_bella', 'Bella (US)'], ['af_nicole', 'Nicole (US, soft)'],
  ['am_michael', 'Michael (US)'], ['am_fenrir', 'Fenrir (US)'],
  ['bf_emma', 'Emma (UK)'], ['bm_george', 'George (UK)'], ['bm_fable', 'Fable (UK)'],
]
const OPENAI_VOICES = ['alloy', 'ash', 'coral', 'echo', 'nova', 'onyx', 'sage', 'shimmer'] as const
const SPEEDS = ['0.9', '1.0', '1.05', '1.15', '1.25'] as const

/** "mlx-community/whisper-large-v3-mlx" -> "large-v3", so old configs still match an option. */
function shortWhisper(name: string): string {
  return name.replace(/^.*\/whisper-/, '').replace(/-mlx$/, '')
}

async function loadSettings($: Engine): Promise<VoiceSettings> {
  const py = await python($)
  const ran = await $.process.run([...py, '-m', 'claudevoice', 'config'], { cwd: root($), timeoutMs: 60000 })
  let parsed: Record<string, unknown> = {}
  try {
    parsed = JSON.parse(ran.stdout) as Record<string, unknown>
  } catch {
    parsed = {}
  }
  const flat: VoiceSettings = {}
  for (const [k, v] of Object.entries(parsed)) flat[k] = String(v)
  // The microphones on this machine, newline-separated; the default one first.
  const mics = await $.process.run([...py, '-m', 'claudevoice.listen', '--devices'], { cwd: root($), timeoutMs: 60000 })
  try {
    const list = JSON.parse(mics.stdout.trim().split('\n').pop() ?? '[]') as { name: string; default: boolean }[]
    flat._microphones = list.map(d => (d.default ? `${d.name}\t(system default)` : d.name)).join('\n')
  } catch {
    flat._microphones = ''
  }
  await update($, settings, () => flat)
  return flat
}

async function setSetting($: Engine, key: string, value: string): Promise<void> {
  await update($, settings, s => ({ ...s, [key]: value }))
  const ran = await $.process.run([...(await python($)), '-m', 'claudevoice', 'config', key, value], { cwd: root($), timeoutMs: 60000 })
  if (ran.exitCode !== 0) $.ui.toast(`could not save ${key}: ${ran.stderr.trim()}`)
}

async function openSettings($: Engine): Promise<void> {
  await loadSettings($)
  await $.ui.open({ id: SETTINGS_PANE, title: 'Voice settings', focus: true, closeOnEscape: true, rows: 22 })
}

/** "local:<whisper repo>" or "openai:<model>" from the config, and back. */
function listeningValue(s: VoiceSettings): string {
  return s.stt_engine === 'openai' ? `openai:${s.stt_model ?? ''}` : `local:${shortWhisper(s.whisper_model ?? '')}`
}
async function pickListening($: Engine, value: string): Promise<void> {
  const [engine, model] = value.split(':', 2) as ['local' | 'openai', string]
  await setSetting($, 'stt_engine', engine)
  await setSetting($, engine === 'local' ? 'whisper_model' : 'stt_model', model)
}

function speakingValue(s: VoiceSettings): string {
  const engine = s.tts_engine ?? 'kokoro'
  return engine === 'kokoro' ? `kokoro:${s.voice ?? 'af_heart'}` : `${engine}:${s.tts_voice ?? ''}`
}
async function pickSpeaking($: Engine, value: string): Promise<void> {
  const [engine, voice] = value.split(':', 2) as [string, string]
  await setSetting($, 'tts_engine', engine)
  if (engine === 'kokoro') await setSetting($, 'voice', voice)
  else if (voice !== '') await setSetting($, 'tts_voice', voice)
}

async function setEnabled($: Engine, on: boolean): Promise<void> {
  await update($, enabled, () => on)
  await $.store.set(ENABLED_STORE_KEY, on)
  if (!on) {
    if (await read($, isCall)) await endCall($, '')
    else await stopSpeaking($)
    await update($, phase, () => 'idle')
  }
}

// ---- drawing -------------------------------------------------------------------------

type Surface = Parameters<Engine['ui']['resolve']>[0]

/** One compact row of controls, drawn in the band above the prompt and in the pane. */
async function controls($: Engine, e: Surface, compact: boolean) {
  const { Box, Button, Text } = $.ui.resolve(e)
  const current = await read($, phase)
  const inCall = await read($, isCall)
  const listenMode = await read($, mode)
  const lastHeard = await read($, heard)
  const message = await read($, note)

  const status: Record<VoicePhase, string> = {
    idle: inCall ? 'on a call' : '',
    loading: 'loading voice…',
    listening: listenMode === 'hold' ? '● listening, press Done when finished' : '● listening, pause to send',
    thinking: 'thinking…',
    speaking: '♪ speaking',
  }

  return (
    <Box flexDirection="column">
      <Box>
        {status[current] !== '' && <Text dimColor>{status[current]}</Text>}
        <Box flexGrow={1} />
        {!inCall && <Button key="read" label="Read aloud" hotkey="r" plain onPress={() => void readAloud($)} />}
        {!inCall && <Text> </Text>}
        {(current === 'speaking' || (inCall && current !== 'listening')) && (
          <Button key="stop" label="Stop" hotkey="s" plain onPress={() => void stopSpeaking($)} />
        )}
        {(current === 'speaking' || (inCall && current !== 'listening')) && <Text> </Text>}
        <Button
          key="mode"
          label={listenMode === 'hold' ? 'mode: hold' : 'mode: auto'}
          hotkey="m"
          plain
          dimColor
          onPress={() => void toggleMode($)}
        />
        <Text> </Text>
        <Button key="settings" label="⚙" hotkey="g" plain onPress={() => void openSettings($)} />
        <Text> </Text>
        {inCall && current === 'listening' && listenMode === 'hold' && (
          <Button key="done" label="Done" hotkey="d" variant="primary" onPress={() => void doneTalking($)} />
        )}
        {inCall && current === 'speaking' && (
          <Button key="talk" label="Talk now" hotkey="t" variant="primary" onPress={() => void talkNow($)} />
        )}
        {inCall && (current === 'listening' || current === 'speaking') && <Text> </Text>}
        {inCall ? (
          <Button key="end" label="End call" hotkey="e" onPress={() => void endCall($, '')} />
        ) : (
          <Button key="call" label="● Call" hotkey="c" variant="primary" onPress={() => void startCall($)} />
        )}
      </Box>
      {!compact && message !== '' && <Text dimColor>{message}</Text>}
      {!compact && inCall && lastHeard !== '' && <Text dimColor>you: {lastHeard}</Text>}
    </Box>
  )
}

/** The controls drawn inside the prompt footer, where the dim mode labels sit (under the mic).
 *  Styled like the footer's own items: no chrome, dim, brighter on hover, no hotkey chips. */
async function footer($: Engine, e: Surface, modes: readonly string[]) {
  const { Box, Button, Text } = $.ui.resolve(e)
  const isOn = await read($, enabled)
  const current = await read($, phase)
  const inCall = await read($, isCall)
  const listenMode = await read($, mode)
  const quiet = { dimColor: true, hover: { dimColor: false, scope: 'voice' } } as const
  const gear = <Button key="settings" label="⚙" plain {...quiet} onPress={() => void openSettings($)} />
  const sep = <Text dimColor> · </Text>

  if (!isOn) {
    return (
      <Box>
        {modes.length > 0 && <Text dimColor>{modes.join(' & ')}</Text>}
        {modes.length > 0 && sep}
        {gear}
      </Box>
    )
  }

  const status: Record<VoicePhase, string> = {
    idle: '',
    loading: 'loading voice…',
    listening: listenMode === 'hold' ? '● listening · press Done when finished' : '● listening · pause to send',
    thinking: 'thinking…',
    speaking: '♪ speaking',
  }
  const busy = current === 'speaking' || (inCall && current !== 'listening')

  return (
    <Box>
      {modes.length > 0 && <Text dimColor>{modes.join(' & ')}</Text>}
      {modes.length > 0 && sep}
      {status[current] !== '' && <Text dimColor>{status[current]}</Text>}
      {status[current] !== '' && sep}
      {!inCall && <Button key="read" label="Read aloud" plain {...quiet} onPress={() => void readAloud($)} />}
      {!inCall && sep}
      {busy && <Button key="stop" label="Stop" plain {...quiet} onPress={() => void stopSpeaking($)} />}
      {busy && sep}
      {gear}
      {sep}
      {inCall && current === 'listening' && listenMode === 'hold' && (
        <Button key="done" label="Done" plain hover={{ bold: true, scope: 'voice' }} onPress={() => void doneTalking($)} />
      )}
      {inCall && current === 'speaking' && (
        <Button key="talk" label="Talk now" plain hover={{ bold: true, scope: 'voice' }} onPress={() => void talkNow($)} />
      )}
      {inCall && (current === 'listening' || current === 'speaking') && sep}
      {inCall ? (
        <Button key="end" label="End call" plain hover={{ bold: true, scope: 'voice' }} onPress={() => void endCall($, '')} />
      ) : (
        <Button key="call" label="● Call" plain hover={{ bold: true, scope: 'voice' }} onPress={() => void startCall($)} />
      )}
    </Box>
  )
}

async function settingsPane($: Engine, e: Surface) {
  if (e.surface === 'mobile') {
    // No Select on mobile: point at the command line instead.
    const { Text } = $.ui.resolve(e)
    return <Text dimColor>Voice settings are edited on the desktop or terminal, or with: claudevoice config</Text>
  }
  const { Box, Button, Select, Text } = $.ui.resolve(e)
  const s = await read($, settings)
  const listenMode = await read($, mode)

  const listening = [
    ...WHISPER_MODELS.map(([repo, label]) => ({ value: `local:${repo}`, label: `Local: ${label}` })),
    { value: 'openai:gpt-4o-mini-transcribe', label: 'Cloud: OpenAI gpt-4o-mini-transcribe' },
    { value: 'openai:gpt-4o-transcribe', label: 'Cloud: OpenAI gpt-4o-transcribe' },
  ]
  const speaking = [
    ...KOKORO_VOICES.map(([v, label]) => ({ value: `kokoro:${v}`, label: `Local Kokoro: ${label}` })),
    ...OPENAI_VOICES.map(v => ({ value: `openai:${v}`, label: `Cloud OpenAI: ${v}` })),
    { value: 'elevenlabs:', label: `Cloud ElevenLabs: voice ${s.tts_voice ?? ''}` },
  ]

  const isOn = await read($, enabled)
  return (
    <Box flexDirection="column">
      <Text>Voice plugin</Text>
      <Select
        key="plugin"
        options={[
          { value: 'on', label: 'On: voice controls in the prompt footer' },
          { value: 'off', label: 'Off: only the gear stays, to turn it back on' },
        ]}
        value={isOn ? 'on' : 'off'}
        onSelect={(v: string) => void setEnabled($, v === 'on')}
      />
      <Box marginTop={1}><Text>Listening (speech to text)</Text></Box>
      <Select key="listening" options={listening} value={listeningValue(s)} onSelect={(v: string) => void pickListening($, v)} />
      <Box marginTop={1}><Text>Microphone</Text></Box>
      <Select
        key="microphone"
        options={[
          { value: '', label: 'System default' },
          ...(s._microphones ?? '').split('\n').filter(x => x !== '').map(line => {
            const [name, tag] = line.split('\t') as [string, string | undefined]
            return { value: name, label: tag ? `${name} ${tag}` : name }
          }),
        ]}
        value={s.input_device ?? ''}
        onSelect={(v: string) => void setSetting($, 'input_device', v)}
      />
      <Box marginTop={1}><Text>Speaking (text to speech)</Text></Box>
      <Select key="speaking" options={speaking} value={speakingValue(s)} onSelect={(v: string) => void pickSpeaking($, v)} />
      <Box marginTop={1}><Text>Speed</Text></Box>
      <Select
        key="speed"
        options={SPEEDS.map(x => ({ value: x, label: x === '1.0' ? '1.0 (natural)' : x }))}
        value={String(Number(s.speed ?? '1.05'))}
        onSelect={(v: string) => void setSetting($, 'speed', v)}
      />
      <Box marginTop={1}><Text>When you talk</Text></Box>
      <Select
        key="listen-mode"
        options={[
          { value: 'hold', label: 'Hold: I pause as much as I like, then press Done' },
          { value: 'auto', label: 'Auto: a one-second pause sends what I said' },
        ]}
        value={listenMode}
        onSelect={(v: string) => void (async () => {
          await update($, mode, () => (v === 'auto' ? 'auto' : 'hold'))
          await $.store.set(MODE_STORE_KEY, v)
        })()}
      />
      <Box marginTop={1}>
        <Text dimColor>Cloud engines read OPENAI_API_KEY and ELEVENLABS_API_KEY from your environment. Set the ElevenLabs voice id with: claudevoice config tts_voice ID</Text>
      </Box>
      <Box marginTop={1}>
        <Button key="close" label="Close" role="dismiss" onPress={() => void $.ui.close({ id: SETTINGS_PANE })} />
      </Box>
    </Box>
  )
}

// ---- registration ---------------------------------------------------------------

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'voice-panel', description: 'Open the voice pane (read aloud, call)' })
    await update($, isCall, () => false)
    await update($, phase, () => 'idle')
    const saved = await $.store.get(MODE_STORE_KEY)
    await update($, mode, () => (saved === 'auto' ? 'auto' : 'hold'))
    const savedEnabled = await $.store.get(ENABLED_STORE_KEY)
    await update($, enabled, () => savedEnabled !== false)
    return next(e)
  })

  on('command.run', { command: 'voice-panel' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Voice' })
    return { text: 'Voice pane opened.' }
  })

  // While a call is on, Claude answers in spoken style.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!(await read($, isCall))) return composed
    return { sections: [...composed.sections, { id: 'claudevoice-call', text: SPOKEN_STYLE, scope: 'session' }] }
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined && (await read($, isCall))) {
      await update($, phase, () => 'thinking')
    }
    return next(e)
  })

  // In a call, every finished reply is spoken, then we listen again.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined || !(await read($, isCall))) return result
    const answer = e.answer.trim()
    $.clock.after(0, () => {
      void (async () => {
        if (answer !== '' && !e.isAborted && (await speak($, answer))) {
          await update($, phase, () => 'speaking')
          watchPlayback($)
        } else {
          void listenOnce($)
        }
      })()
    })
    return result
  })

  // Any plugin's $.audio.speak (the platform `say` voice) goes through Kokoro instead.
  on('audio.speak', async ($, e, next) => {
    if (!(await read($, enabled))) return next(e)
    if (await speak($, e.text)) {
      await update($, phase, () => 'speaking')
      watchPlayback($)
    }
    return { value: { via: 'system' as const } }
  })

  // Desktop and terminal: the controls live in the prompt footer, under the mic.
  on('ui.render', { component: 'SessionMode' }, ($, e) => footer($, e, e.props.modes))

  // Elsewhere (no footer site) fall back to a compact bar above the prompt.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || e.surface === 'desktop' || e.surface === 'terminal') return next(e)
    return controls($, e, true)
  })

  // The optional pane (/voice-panel) adds what was heard and any error.
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => controls($, e, false))

  // The gear's settings dialog.
  on('ui.render', { component: 'Pane', requestId: SETTINGS_PANE }, ($, e) => settingsPane($, e))
}

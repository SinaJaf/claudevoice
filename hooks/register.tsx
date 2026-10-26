import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ChatSettings, VoicePhase, VoiceSettings } from '../types'

const SERVER = 'http://127.0.0.1:8765'

const PANE = 'claudevoice'
const SETTINGS_PANE = 'claudevoice-settings'

const phase = atom({ plugin: 'claudevoice', key: 'phase' } as const, 'idle' as VoicePhase)
const isCall = atom({ plugin: 'claudevoice', key: 'isCall' } as const, false)
const heard = atom({ plugin: 'claudevoice', key: 'heard' } as const, '')
const note = atom({ plugin: 'claudevoice', key: 'note' } as const, '')
const settings = atom({ plugin: 'claudevoice', key: 'settings' } as const, {} as VoiceSettings)
const enabled = atom({ plugin: 'claudevoice', key: 'enabled' } as const, true)
/** This chat's own settings: a key here overrides the all-chats setting (see ChatSettings). */
const chat = atom({ plugin: 'claudevoice', key: 'chat' } as const, {} as ChatSettings)
/** Which settings rows have their "this chat" dropdown open. */
const chatOpen = atom({ plugin: 'claudevoice', key: 'chatOpen' } as const, {} as Record<string, boolean>)
/** Animation frame counter for the footer's live status. */
const tick = atom({ plugin: 'claudevoice', key: 'tick' } as const, 0)
const CHAT_STORE_PREFIX = 'chat:'
const SAME_AS_ALL = '__all__'
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

// ---- this chat's settings over the all-chats ones ------------------------------------

async function chatStoreKey($: Engine): Promise<string> {
  return CHAT_STORE_PREFIX + (await $.session.id())
}

/** Is the plugin on here? This chat's choice, else the all-chats one. */
async function isEnabled($: Engine): Promise<boolean> {
  const c = await read($, chat)
  return c.plugin !== undefined ? c.plugin === 'on' : read($, enabled)
}

async function setChatSetting($: Engine, key: keyof ChatSettings, value: string): Promise<void> {
  const next: ChatSettings = { ...(await read($, chat)) }
  if (value === SAME_AS_ALL) delete next[key]
  else next[key] = value
  await update($, chat, () => next)
  const storeKey = await chatStoreKey($)
  if (Object.keys(next).length === 0) await $.store.delete(storeKey)
  else await $.store.set(storeKey, next)
  if (key === 'plugin' && !(await isEnabled($))) await quiet($)
}

async function speak($: Engine, text: string): Promise<boolean> {
  if (!(await ensureServer($))) return false
  // This chat's voice and speed, when it has its own; the server uses the config otherwise.
  const c = await read($, chat)
  const body: Record<string, unknown> = { text, interrupt: true }
  if (c.speaking !== undefined) {
    const [engine, voice] = c.speaking.split(':', 2) as [string, string]
    body.engine = engine
    if (voice !== '') body.voice = voice
  }
  if (c.speed !== undefined) body.speed = Number(c.speed)
  try {
    const r = await $.http.fetch(`${SERVER}/speak`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
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


// ---- the features -----------------------------------------------------------------
// You talk with Claude's own microphone (its dictation fills the prompt box); this
// plugin sends what is there when you press Done and speaks every reply back.

async function setPhase($: Engine, p: VoicePhase): Promise<void> {
  await update($, phase, () => p)
  if (p !== 'idle') startTicker($)
}

let ticking = false

/** Advance the footer's animation while something is happening; stops when idle. */
function startTicker($: Engine): void {
  if (ticking) return
  ticking = true
  const step = (): void => {
    $.clock.after(250, () => {
      void (async () => {
        if ((await read($, phase)) === 'idle') {
          ticking = false
          return
        }
        await update($, tick, t => (t + 1) % 1000)
        step()
      })()
    })
  }
  step()
}

/** Read the last reply in this session aloud, once. */
async function readAloud($: Engine): Promise<void> {
  const messages = await $.session.messages()
  const last = [...messages].reverse().find(m => m.role === 'assistant' && m.text.trim() !== '')
  if (!last) {
    $.ui.toast('Nothing to read yet')
    return
  }
  if (!(await speak($, last.text))) return
  await setPhase($, 'speaking')
  watchPlayback($)
}

/** Poll the server until playback ends; in a call it is then your turn. */
function watchPlayback($: Engine): void {
  $.clock.after(300, () => {
    void (async () => {
      if ((await read($, phase)) !== 'speaking') return // stopped, or a new turn began
      const h = await health($)
      if (h?.speaking) {
        watchPlayback($)
        return
      }
      if (h?.last_error) {
        await update($, note, () => h.last_error ?? '')
        $.ui.toast(h.last_error)
      }
      await setPhase($, (await read($, isCall)) ? 'listening' : 'idle')
    })()
  })
}

async function startCall($: Engine): Promise<void> {
  if (await read($, isCall)) return
  await update($, isCall, () => true)
  await update($, note, () => '')
  await setPhase($, 'loading')
  if (!(await ensureServer($))) {
    await endCall($, '')
    return
  }
  if (await speak($, "I'm here. Talk with the mic, take your time, and press Done when you've finished.")) {
    await setPhase($, 'speaking')
    watchPlayback($)
  } else {
    await setPhase($, 'listening')
  }
}

/** Hang up: stop the voice. Never touches the work Claude is doing. */
async function endCall($: Engine, reason: string): Promise<void> {
  await update($, isCall, () => false)
  await setPhase($, 'idle')
  await update($, note, () => reason)
  await stopSpeaking($)
}

/** Send what your dictation (or typing) left in the prompt box. */
async function done($: Engine): Promise<void> {
  const { text } = await $.prompt.read()
  const said = text.trim()
  if (said === '') {
    $.ui.toast('Nothing in the prompt box yet: talk with the mic first')
    return
  }
  if ((await read($, phase)) === 'speaking') await stopSpeaking($)
  await $.prompt.fill({ text: '' })
  await update($, heard, () => said)
  await setPhase($, 'thinking')
  await $.prompt.submit({ text: said })
}

/** Stop Claude's voice; in a call it is your turn right away. */
async function stop($: Engine): Promise<void> {
  await stopSpeaking($)
  if ((await read($, phase)) === 'speaking') await setPhase($, (await read($, isCall)) ? 'listening' : 'idle')
}

// ---- settings ----------------------------------------------------------------------

const KOKORO_VOICES: readonly [string, string][] = [
  ['af_heart', 'Heart (US, warm)'], ['af_bella', 'Bella (US)'], ['af_nicole', 'Nicole (US, soft)'],
  ['am_michael', 'Michael (US)'], ['am_fenrir', 'Fenrir (US)'],
  ['bf_emma', 'Emma (UK)'], ['bm_george', 'George (UK)'], ['bm_fable', 'Fable (UK)'],
]
const OPENAI_VOICES = ['alloy', 'ash', 'coral', 'echo', 'nova', 'onyx', 'sage', 'shimmer'] as const
const SPEEDS = ['0.9', '1.0', '1.05', '1.15', '1.25'] as const

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
  if (!(await isEnabled($))) await quiet($)
}

/** The plugin was switched off here: hang up and stop talking. */
async function quiet($: Engine): Promise<void> {
  if (await read($, isCall)) await endCall($, '')
  else await stopSpeaking($)
  await setPhase($, 'idle')
}

// ---- drawing -------------------------------------------------------------------------

type Surface = Parameters<Engine['ui']['resolve']>[0]

const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] as const
const WAVE = ['▁▃▅▃', '▃▅▇▅', '▅▇▅▃', '▇▅▃▁', '▅▃▁▃', '▃▁▃▅'] as const

/** The live status: what is happening now, animated. */
async function liveStatus($: Engine, e: Surface) {
  const { Box, Text } = $.ui.resolve(e)
  const current = await read($, phase)
  const t = await read($, tick)
  if (current === 'idle') return null
  const glyph = (text: string, color: string) => <Text key="glyph" color={color}>{text}</Text>
  const parts = {
    loading: [glyph(SPINNER[t % SPINNER.length] ?? '', 'claude'), 'starting the voice'],
    listening: [glyph(t % 4 < 2 ? '●' : '○', 'success'), 'your turn · talk with the mic, then Done'],
    thinking: [glyph(SPINNER[t % SPINNER.length] ?? '', 'claude'), 'thinking'],
    speaking: [glyph(WAVE[t % WAVE.length] ?? '', 'claude'), 'speaking'],
  }[current]
  return (
    <Box>
      {parts[0]}
      <Text dimColor> {parts[1]}</Text>
    </Box>
  )
}

/** One compact row of controls, drawn in the band above the prompt and in the pane. */
async function controls($: Engine, e: Surface, compact: boolean) {
  const { Box, Button, Text } = $.ui.resolve(e)
  const current = await read($, phase)
  const inCall = await read($, isCall)
  const lastHeard = await read($, heard)
  const message = await read($, note)
  await read($, chat) // redraw when this chat's settings change

  return (
    <Box flexDirection="column">
      <Box>
        {await liveStatus($, e)}
        <Box flexGrow={1} />
        {!inCall && <Button key="read" label="Read aloud" hotkey="r" plain onPress={() => void readAloud($)} />}
        {!inCall && <Text> </Text>}
        {inCall && <Button key="done" label="Done" hotkey="d" variant="primary" onPress={() => void done($)} />}
        {inCall && <Text> </Text>}
        {(inCall || current === 'speaking') && <Button key="stop" label="Stop" hotkey="s" plain onPress={() => void stop($)} />}
        {(inCall || current === 'speaking') && <Text> </Text>}
        <Button key="settings" label="⚙" hotkey="g" plain onPress={() => void openSettings($)} />
        <Text> </Text>
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

/** The controls drawn inside the prompt footer, under the mic. In a call the same
 *  controls always stand in the same places; the one that matters now lights up. */
async function footer($: Engine, e: Surface, modes: readonly string[]) {
  const { Box, Button, Text } = $.ui.resolve(e)
  const isOn = await isEnabled($)
  const current = await read($, phase)
  const inCall = await read($, isCall)
  const quiet = { dimColor: true, hover: { dimColor: false, scope: 'voice' } } as const
  const lit = { hover: { bold: true, scope: 'voice' } } as const
  const gear = <Button key="settings" label="⚙" plain {...quiet} onPress={() => void openSettings($)} />
  const sep = <Text dimColor> · </Text>
  const modeLabel = modes.length > 0 ? <Text dimColor>{modes.join(' & ')}</Text> : null

  if (!isOn) {
    return (
      <Box>
        {modeLabel}
        {modeLabel && sep}
        {gear}
      </Box>
    )
  }

  const status = await liveStatus($, e)
  const yourTurn = current === 'listening'
  const speaking = current === 'speaking'
  return (
    <Box>
      {modeLabel}
      {modeLabel && sep}
      {status}
      {status && sep}
      {!inCall && <Button key="read" label="Read aloud" plain {...quiet} onPress={() => void readAloud($)} />}
      {!inCall && sep}
      {inCall && (
        <Box key="done-box">
          <Text color={yourTurn ? 'success' : undefined} dimColor={!yourTurn}>✓ </Text>
          <Button key="done" label="Done" plain dimColor={!yourTurn} {...(yourTurn ? lit : { hover: quiet.hover })} onPress={() => void done($)} />
        </Box>
      )}
      {inCall && sep}
      {(inCall || speaking) && (
        <Box key="stop-box">
          <Text dimColor={!speaking}>■ </Text>
          <Button key="stop" label="Stop" plain dimColor={!speaking} {...(speaking ? lit : { hover: quiet.hover })} onPress={() => void stop($)} />
        </Box>
      )}
      {(inCall || speaking) && sep}
      {gear}
      {sep}
      {inCall ? (
        <Box key="end-box">
          <Text color="error">✕ </Text>
          <Button key="end" label="End call" plain {...lit} onPress={() => void endCall($, '')} />
        </Box>
      ) : (
        <Box key="call-box">
          <Text color="claude">● </Text>
          <Button key="call" label="Call" plain {...lit} onPress={() => void startCall($)} />
        </Box>
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
  const here = await read($, chat)
  const open = await read($, chatOpen)
  const isOn = await read($, enabled)

  type Option = { value: string; label: string }
  const plugin: Option[] = [
    { value: 'on', label: 'On: voice controls in the prompt footer' },
    { value: 'off', label: 'Off: only the gear stays, to turn it back on' },
  ]
  const speaking: Option[] = [
    ...KOKORO_VOICES.map(([v, label]) => ({ value: `kokoro:${v}`, label: `Local Kokoro: ${label}` })),
    ...OPENAI_VOICES.map(v => ({ value: `openai:${v}`, label: `Cloud OpenAI: ${v}` })),
    { value: 'elevenlabs:', label: `Cloud ElevenLabs: voice ${s.tts_voice ?? ''}` },
  ]
  const speeds: Option[] = SPEEDS.map(x => ({ value: x, label: x === '1.0' ? '1.0 (natural)' : x }))
  /** One setting: its title with a "this chat" dropdown, the all-chats choice,
   *  and, when the dropdown is open, the same choice for this chat only. */
  const row = (
    id: keyof ChatSettings, title: string, options: Option[], allValue: string,
    onAll: (v: string) => void, first = false,
  ) => {
    const own = here[id]
    const isOpen = open[id] === true
    const ownLabel = own === undefined ? '' : `: ${options.find(o => o.value === own)?.label ?? own}`
    return (
      <Box key={`row-${id}`} flexDirection="column" marginTop={first ? 0 : 1}>
        <Box>
          <Text>{title}</Text>
          <Text> </Text>
          <Button
            key={`chat-${id}`}
            label={`${isOpen ? '▾' : '▸'} this chat${own === undefined ? '' : ' (own setting)'}`}
            plain
            dimColor={own === undefined}
            hover={{ dimColor: false }}
            onPress={() => void update($, chatOpen, o => ({ ...o, [id]: !isOpen }))}
          />
        </Box>
        <Select key={id} options={options} value={allValue} onSelect={onAll} />
        {!isOpen && own !== undefined && <Text dimColor>  this chat uses{ownLabel}</Text>}
        {isOpen && (
          <Box flexDirection="column" marginLeft={2}>
            <Text dimColor>This chat only:</Text>
            <Select
              key={`chat-${id}-select`}
              options={[{ value: SAME_AS_ALL, label: 'Same as all chats' }, ...options]}
              value={own ?? SAME_AS_ALL}
              onSelect={(v: string) => void setChatSetting($, id, v)}
            />
          </Box>
        )}
      </Box>
    )
  }

  return (
    <Box flexDirection="column">
      <Text dimColor>Each choice applies to all chats. Open "this chat" under a setting to change it for this chat only.</Text>
      <Box marginTop={1} flexDirection="column">
        {row('plugin', 'Voice plugin', plugin, isOn ? 'on' : 'off', (v: string) => void setEnabled($, v === 'on'), true)}
        {row('speaking', 'Speaking (text to speech)', speaking, speakingValue(s), (v: string) => void pickSpeaking($, v))}
        {row('speed', 'Speed', speeds, String(Number(s.speed ?? '1.05')), (v: string) => void setSetting($, 'speed', v))}
      </Box>
      <Box marginTop={1}>
        <Text dimColor>You talk with Claude's own microphone. Cloud voices read OPENAI_API_KEY and ELEVENLABS_API_KEY from your environment. Set the ElevenLabs voice id with: claudevoice config tts_voice ID</Text>
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
    const savedEnabled = await $.store.get(ENABLED_STORE_KEY)
    await update($, enabled, () => savedEnabled !== false)
    const savedChat = await $.store.get(await chatStoreKey($))
    await update($, chat, () => (savedChat !== null && typeof savedChat === 'object' ? (savedChat as ChatSettings) : {}))
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

  // You started talking or typing into the box: Claude stops talking.
  on('prompt.edit', async ($, e, next) => {
    const result = await next(e)
    if (result.text.trim() !== '' && (await read($, isCall)) && (await read($, phase)) === 'speaking') await stop($)
    return result
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined && (await read($, isCall))) await setPhase($, 'thinking')
    return next(e)
  })

  // In a call, every finished reply is spoken; then it is your turn.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined || !(await read($, isCall))) return result
    const answer = e.answer.trim()
    $.clock.after(0, () => {
      void (async () => {
        if (answer !== '' && !e.isAborted && (await speak($, answer))) {
          await setPhase($, 'speaking')
          watchPlayback($)
        } else {
          await setPhase($, 'listening')
        }
      })()
    })
    return result
  })

  // Any plugin's $.audio.speak (the platform `say` voice) goes through Kokoro instead.
  on('audio.speak', async ($, e, next) => {
    if (!(await isEnabled($))) return next(e)
    if (await speak($, e.text)) {
      await setPhase($, 'speaking')
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

  // The optional pane (/voice-panel) adds what was sent and any error.
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => controls($, e, false))

  // The gear's settings dialog.
  on('ui.render', { component: 'Pane', requestId: SETTINGS_PANE }, ($, e) => settingsPane($, e))
}

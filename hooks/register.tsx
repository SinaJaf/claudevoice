import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ChatSettings, ListenMode, Patience, ReadingState, VoicePhase, VoiceSettings } from '../types'

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
/** In a call: the microphone records nothing until unmuted; what came before is kept. */
const muted = atom({ plugin: 'claudevoice', key: 'muted' } as const, false)
/** The result file of the listener now running ('' when none): one listener at a time. */
const take = atom({ plugin: 'claudevoice', key: 'take' } as const, '')
/** What the listener hears right now: waiting, hearing, pause, transcribing ('' when idle). */
const hearing = atom({ plugin: 'claudevoice', key: 'hearing' } as const, '')
/** live: Claude answers when you have finished talking; hold: when you press Done. */
const listenMode = atom({ plugin: 'claudevoice', key: 'listenMode' } as const, 'live' as ListenMode)
/** Live mode: talking while Claude speaks stops its voice. */
const talkOver = atom({ plugin: 'claudevoice', key: 'talkOver' } as const, true)
/** Live mode: how long a pause runs before Claude answers. */
const patience = atom({ plugin: 'claudevoice', key: 'patience' } as const, 'normal' as Patience)
// A file or selection being read aloud, as the voice server reports it.
const NOT_READING: ReadingState = { title: '', at: 0, of: 0, paused: false }
const reading = atom({ plugin: 'claudevoice', key: 'reading' } as const, NOT_READING)
const MODE_STORE_KEY = 'callMode'
const TALK_OVER_STORE_KEY = 'talkOver'
const PATIENCE_STORE_KEY = 'patience'
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

let synced = false

/** First use on a machine: create the venv and pull the speech packages (a few minutes).
 *  After an update, bring an existing venv in line with the lock once per load (quick
 *  when nothing changed), so new engines find their packages. */
async function ensurePython($: Engine): Promise<boolean> {
  const r = root($)
  if ((await $.fs.exists(`${r}/.venv/bin/python`)) || (await $.fs.exists(`${r}/.venv/Scripts/python.exe`))) {
    if (!synced) {
      synced = true
      await update($, note, () => 'checking the voice engine…')
      // --inexact keeps optional engines (Whisper) a user has added.
      await $.process.run([await uv($), 'sync', '--project', r, '--quiet', '--inexact'], { cwd: r, timeoutMs: 600000 })
      await update($, note, () => '')
    }
    return true
  }
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

type Health = { speaking: boolean; last_error?: string; reading?: ReadingState | null }

async function health($: Engine): Promise<Health | null> {
  try {
    const r = await $.http.fetch(`${SERVER}/health`)
    return r.ok ? (JSON.parse(r.text) as Health) : null
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

/** Replace a running server with this version's (after an update). */
async function restartServer($: Engine): Promise<boolean> {
  await $.process.run([...(await python($)), '-m', 'claudevoice', 'stop-server'], { cwd: root($), timeoutMs: 30000 })
  for (let i = 0; i < 20 && (await health($)); i++) await new Promise<void>(done => $.clock.after(250, done))
  return ensureServer($)
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
  if (key === 'patience' || key === 'talkOver') await shareLiveSettings($)
  if (key === 'listening' && value.startsWith('local:')) await ensureWhisper($)
}

async function modeHere($: Engine): Promise<ListenMode> {
  const m = (await read($, chat)).listenMode ?? (await read($, listenMode))
  return m === 'hold' ? 'hold' : 'live'
}

async function talkOverHere($: Engine): Promise<boolean> {
  const c = await read($, chat)
  return c.talkOver !== undefined ? c.talkOver === 'on' : read($, talkOver)
}

const PATIENCES: readonly Patience[] = ['short', 'normal', 'long', 'long2', 'long3', 'long4', 'long5', 'long6', 'long7', 'long8', 'long9', 'long10']

function asPatience(value: unknown): Patience {
  return PATIENCES.includes(value as Patience) ? (value as Patience) : 'normal'
}

async function patienceHere($: Engine): Promise<Patience> {
  return asPatience((await read($, chat)).patience ?? (await read($, patience)))
}

/** Listener flags for this chat's listening engine and microphone, if it has its own. */
async function listenOverrides($: Engine): Promise<string[]> {
  const c = await read($, chat)
  const args: string[] = []
  if (c.listening !== undefined) {
    const [engine, model] = c.listening.split(':', 2) as [string, string]
    args.push(`--stt=${engine}`)
    if (engine === 'parakeet') args.push(`--parakeet=${model}`)
    if (engine === 'local') args.push(`--whisper=${model}`)
    if (engine === 'openai') args.push(`--stt-model=${model}`)
  }
  if (c.microphone !== undefined) args.push(`--device=${c.microphone}`)
  return args
}

/** Tell the listener this chat's pause length and talk-over. A running listener re-reads
 *  this file, so a change in the gear applies to the turn already listening. */
async function shareLiveSettings($: Engine): Promise<void> {
  await touch($, 'listen.settings', JSON.stringify({ patience: await patienceHere($), talk_over: await talkOverHere($) }))
}

async function touch($: Engine, name: string, text = ''): Promise<void> {
  try {
    await $.fs.write(await flagPath($, name), text)
  } catch {
    // no flag: the listener carries on as it was
  }
}

/** This chat's voice and speed, when it has its own; the server uses the config otherwise. */
async function voiceFields($: Engine): Promise<Record<string, unknown>> {
  const c = await read($, chat)
  const fields: Record<string, unknown> = {}
  if (c.speaking !== undefined) {
    const [engine, voice] = c.speaking.split(':', 2) as [string, string]
    fields.engine = engine
    if (voice !== '') fields.voice = voice
  }
  if (c.speed !== undefined) fields.speed = Number(c.speed)
  return fields
}

async function speak($: Engine, text: string): Promise<boolean> {
  if (!(await ensureServer($))) return false
  const body = { text, interrupt: true, ...(await voiceFields($)) }
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
// A call is a back-and-forth. Live mode (the default): the mic listens while Claude
// talks too, so you can talk over it; your turn ends when Smart Turn hears you have
// finished (or a long pause, or Done). Hold mode: the mic records until you press Done.
// Either way your words go to Claude, its reply is read aloud, and the mic stays open.

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
    $.clock.after(500, () => {
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

/** Read aloud what you point at: text you have just copied (anywhere: a file open in the
 *  app, a PDF in another app; a copied path reads that file), else the text you have
 *  selected in the chat, else a file named in the prompt box (`@notes.md`, `@guide.pdf`,
 *  a path), from its start or the lines it names, else Claude's last reply. */
async function readAloud($: Engine): Promise<void> {
  const copied = await freshCopy($)
  if (copied !== '') {
    const line = copied.trim()
    const asFile = line.includes('\n') ? null : await findFile($, line.replace(/^(["'])(.*)\1$/, '$2'))
    await readDocument($, asFile ?? { text: copied, title: 'copied text' })
    return
  }
  const selected = await $.ui.selection().catch(() => undefined)
  if (selected !== undefined && selected.text.trim() !== '') {
    await readDocument($, { text: selected.text, title: 'selection' })
    return
  }
  const file = await fileInPrompt($)
  if (file !== null) {
    await readDocument($, file)
    return
  }
  await readLastReply($)
}

async function readLastReply($: Engine): Promise<void> {
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

// ---- what you copied ------------------------------------------------------------------
// The app tells a plugin what is selected in the chat, not in its Files pane or in other
// apps. There, copy (⌘C / Ctrl+C) and press Read aloud: a copy made since the plugin last
// looked is read. One that came out of this chat is left alone, so copying a command
// from a reply doesn't stop Read aloud reading the reply.

let lastCopied: string | null = null // the clipboard when last looked at; null: not yet

/** The clipboard's text on macOS, Windows or Linux; '' when it can't be read. */
async function clipboard($: Engine): Promise<string> {
  const readers: string[][] = (await $.fs.exists('/usr/bin/pbpaste'))
    ? [['/usr/bin/pbpaste']]
    : (await $.env.get('OS')) === 'Windows_NT'
      ? [['powershell', '-NoProfile', '-Command', 'Get-Clipboard -Raw']]
      : [['wl-paste', '--no-newline'], ['xclip', '-selection', 'clipboard', '-o'], ['xsel', '--clipboard', '--output']]
  for (const argv of readers) {
    try {
      const ran = await $.process.run(argv, { timeoutMs: 5000 })
      if (ran.exitCode === 0) return ran.stdout
    } catch {
      // not installed here: try the next
    }
  }
  return ''
}

/** Text copied since the plugin last looked, and not copied out of this chat; else ''. */
async function freshCopy($: Engine): Promise<string> {
  const text = await clipboard($)
  const before = lastCopied
  lastCopied = text
  if (before === null || text === before || text.trim() === '') return ''
  const squeeze = (t: string) => t.replace(/\s+/g, ' ').trim()
  const copied = squeeze(text)
  const messages = await $.session.messages()
  return messages.some(m => squeeze(m.text).includes(copied)) ? '' : text
}

type FileToRead = { path: string; start?: number; end?: number }

/** The file the prompt box names: an `@` mention (`@notes.md`, `@"my notes.md"`,
 *  `@guide.md#L10-40`), or a path that is all the box holds (dragged or pasted in). */
async function fileInPrompt($: Engine): Promise<FileToRead | null> {
  const { text } = await $.prompt.read()
  const named = [...text.matchAll(/@(?:"([^"]+)"|(\S+))/g)].map(m => m[1] ?? m[2] ?? '')
  const whole = text.trim().replace(/^(["'])(.*)\1$/, '$2').replace(/\\ /g, ' ')
  if (named.length === 0 && whole !== '' && !whole.includes('\n')) named.push(whole)
  for (const name of named) {
    const file = await findFile($, name)
    if (file !== null) return file
  }
  return null
}

/** `name` as a file on disk, with the lines (or PDF pages) a `#L10-40` suffix asks for. */
async function findFile($: Engine, name: string): Promise<FileToRead | null> {
  const range = /#(?:L|page=|p)?(\d+)(?:-L?(\d+))?$/.exec(name)
  let path = range ? name.slice(0, range.index) : name
  if (path.startsWith('~/') || path.startsWith('~\\')) path = `${await home($)}${path.slice(1)}`
  else if (!path.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(path)) path = `${await $.session.cwd()}/${path}`
  try {
    if ((await $.fs.stat(path)).kind !== 'file') return null
  } catch {
    return null
  }
  if (!range) return { path }
  const start = Number(range[1])
  return { path, start, end: range[2] !== undefined ? Number(range[2]) : start }
}

/** Read a file or a piece of text aloud through the server, which keeps its place. */
async function readDocument($: Engine, what: FileToRead | { text: string; title: string }): Promise<void> {
  if (!(await ensureServer($))) return
  let answer: { ok?: boolean; error?: string; title?: string; pages?: number } = {}
  const body = JSON.stringify({ ...what, ...(await voiceFields($)) })
  try {
    let r = await $.http.fetch(`${SERVER}/read`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
    if (r.status === 404 && (await restartServer($))) {
      // A server started before this update has no /read yet.
      r = await $.http.fetch(`${SERVER}/read`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
    }
    answer = JSON.parse(r.text) as typeof answer
  } catch {
    answer = { ok: false, error: 'the voice server did not answer' }
  }
  if (answer.ok !== true) {
    $.ui.toast(`Can't read that aloud: ${answer.error ?? 'unknown error'}`)
    return
  }
  const where = 'start' in what && what.start !== undefined
    ? `${answer.pages ? 'page' : 'line'} ${what.start}${what.end !== undefined && what.end !== what.start ? `–${what.end}` : ''}`
    : 'the start'
  $.ui.toast('path' in what
    ? `Reading ${answer.title ?? 'the file'} from ${where}`
    : what.title === 'selection' ? 'Reading your selection' : 'Reading what you copied')
  await update($, reading, () => ({ title: answer.title ?? '', at: 0, of: 0, paused: false }))
  await setPhase($, 'speaking')
  watchPlayback($)
}

/** Pause a reading; Resume carries on from the sentence it stopped in. */
async function pauseReading($: Engine): Promise<void> {
  try {
    await $.http.fetch(`${SERVER}/pause`, { method: 'POST', body: '{}' })
  } catch {
    // server gone: nothing is playing
  }
}

async function resumeReading($: Engine): Promise<void> {
  if (!(await ensureServer($))) return
  try {
    const r = await $.http.fetch(`${SERVER}/read`, { method: 'POST', body: JSON.stringify({ resume: true }) })
    if (!r.ok) {
      await update($, reading, () => NOT_READING)
      return
    }
  } catch {
    await update($, reading, () => NOT_READING)
    return
  }
  await update($, reading, r => ({ ...r, paused: false }))
  await setPhase($, 'speaking')
  watchPlayback($)
}

/** Poll the server until playback ends; in a call the mic then opens again. */
function watchPlayback($: Engine): void {
  $.clock.after(300, () => {
    void (async () => {
      if ((await read($, phase)) !== 'speaking') return // a new turn began
      const h = await health($)
      await update($, reading, () => h?.reading ?? NOT_READING)
      if (h?.speaking) {
        watchPlayback($)
        return
      }
      if (h?.last_error) {
        await update($, note, () => h.last_error ?? '')
        $.ui.toast(h.last_error)
      }
      if (!(await read($, isCall))) {
        await setPhase($, 'idle')
        return
      }
      await setPhase($, 'listening') // your turn
      if ((await read($, take)) === '') void listenOnce($) // live mode is already listening
    })()
  })
}

/** Record until Done, transcribe, send. The listener runs detached and answers through a
 *  file, so a take is not bound by the 10-minute limit on a plugin command. */
async function listenOnce($: Engine): Promise<void> {
  if (!(await read($, isCall))) return
  if ((await read($, take)) !== '') return // one listener at a time, and it is already listening
  // While Claude is still talking (live mode), the phase stays 'speaking' until it stops.
  if ((await read($, phase)) !== 'speaking') await setPhase($, 'listening')
  const session = (await $.session.id()).replace(/[^A-Za-z0-9_-]/g, '')
  const resultPath = await flagPath($, `listen-${session}-${await $.clock.now()}.json`)
  // The heartbeat tells the listener someone is still waiting; it gives up if it goes stale.
  const heartbeat = await flagPath($, 'listen.alive')
  await touch($, 'listen.alive', String(await $.clock.now()))
  const how = (await modeHere($)) === 'live'
    ? ['--live', `--patience=${await patienceHere($)}`, (await talkOverHere($)) ? '--talk-over' : '--no-talk-over']
    : ['--manual']
  const argv = [...(await python($)), '-m', 'claudevoice.listen', ...(await listenOverrides($)),
    ...how, '--result', resultPath, '--heartbeat', heartbeat, '--detach']
  await update($, take, () => resultPath)
  await shareLiveSettings($)
  const ran = await $.process.run(argv, { cwd: root($), timeoutMs: 60000 })
  if (ran.exitCode !== 0) {
    await update($, take, () => '')
    await endCall($, `listening failed: ${ran.stderr.trim().split('\n').pop() ?? 'unknown error'}`)
    return
  }
  awaitTake($, resultPath)
}

/** Check for the listener's answer every 300 ms, then act on it. */
function awaitTake($: Engine, resultPath: string): void {
  $.clock.after(300, () => {
    void (async () => {
      if (!(await read($, isCall))) return // hung up: the listener was told to stop
      if ((await read($, take)) !== resultPath) return // a newer listener took over
      await touch($, 'listen.alive', String(await $.clock.now()))
      await followListener($)
      let raw = ''
      try {
        if (await $.fs.exists(resultPath)) raw = String(await $.fs.read(resultPath))
      } catch {
        raw = ''
      }
      if (raw === '') {
        awaitTake($, resultPath)
        return
      }
      await handleTake($, raw)
    })()
  })
}

/** Mirror what the listener hears (hearing you, writing...) in the footer. */
async function followListener($: Engine): Promise<void> {
  let state = ''
  try {
    const path = await flagPath($, 'listen.state')
    if (await $.fs.exists(path)) state = String(await $.fs.read(path)).trim()
  } catch {
    state = ''
  }
  if (state !== (await read($, hearing))) await update($, hearing, () => state)
  if (state === 'transcribing' && (await read($, phase)) === 'listening') await setPhase($, 'transcribing')
}

async function handleTake($: Engine, raw: string): Promise<void> {
  await update($, take, () => '')
  await update($, hearing, () => '')
  if (!(await read($, isCall))) return
  let parsed: { text?: string; cancelled?: boolean; silent?: boolean; busy?: boolean; device?: string; error?: string } = {}
  try {
    parsed = JSON.parse(raw.trim()) as typeof parsed
  } catch {
    parsed = {}
  }
  // A newer listener took over this take; that one carries on.
  if (parsed.cancelled === true) return
  if (parsed.busy === true) {
    // The mic could not be taken over in time: try again shortly rather than go quiet.
    $.clock.after(1000, () => void listenOnce($))
    return
  }
  if ((parsed.error ?? '') !== '') {
    await endCall($, `listening failed: ${parsed.error}`)
    $.ui.toast(`listening failed: ${parsed.error}`)
    return
  }
  if (parsed.silent === true) {
    const why = `The microphone "${parsed.device ?? ''}" is giving silence. Check it is connected and allowed, or pick another one in the voice settings.`
    await endCall($, why)
    $.ui.toast(why)
    return
  }
  const text = (parsed.text ?? '').trim()
  if (text === '') {
    if ((await modeHere($)) === 'hold') $.ui.toast("I didn't catch any words; still listening")
    void listenOnce($)
    return
  }
  await update($, heard, () => text)
  await setPhase($, 'thinking')
  await $.prompt.submit({ text })
}

async function startCall($: Engine): Promise<void> {
  if (await read($, isCall)) return
  await update($, isCall, () => true)
  await update($, muted, () => false)
  await touch($, 'listen.mute', '0')
  await update($, note, () => '')
  await setPhase($, 'loading')
  if (!(await ensureServer($))) {
    await endCall($, '')
    return
  }
  await ensurePython($)
  // Loads the listening engine (downloads its model the first time, ~650 MB for Parakeet).
  await update($, note, () => 'loading the listening model (a one-time download on first use)…')
  const warm = await $.process.run([...(await python($)), '-m', 'claudevoice.listen', ...(await listenOverrides($)), '--warm'], { cwd: root($), timeoutMs: 600000 })
  await update($, note, () => '')
  if (warm.exitCode !== 0) {
    await endCall($, `could not start listening: ${warm.stderr.trim().split('\n').pop() ?? ''}`)
    return
  }
  const live = (await modeHere($)) === 'live'
  const greeting = live
    ? "I'm listening. Just talk, and I'll answer when you've finished."
    : "I'm listening. Take your time, and press Done when you've finished."
  if (await speak($, greeting)) {
    await setPhase($, 'speaking')
    watchPlayback($)
    if (live) void listenOnce($) // live: the mic is open while Claude talks, for talk-over
  } else {
    void listenOnce($)
  }
}

/** Hang up: stop the mic and the voice. Never touches the work Claude is doing. */
async function endCall($: Engine, reason: string): Promise<void> {
  await update($, isCall, () => false)
  await update($, take, () => '')
  await update($, hearing, () => '')
  await update($, muted, () => false)
  await touch($, 'listen.mute', '0')
  await setPhase($, 'idle')
  await update($, note, () => reason)
  await touch($, 'listen.stop')
  await stopSpeaking($)
}

/** Finish talking: what you said goes to Claude. */
async function done($: Engine): Promise<void> {
  if ((await read($, phase)) !== 'listening') return
  await touch($, 'listen.done')
  await setPhase($, 'transcribing') // the listener now turns what you said into text
}

/** Mute pauses the mic: nothing is recorded while muted, what came before is kept,
 *  Unmute carries on the same take, and Done still sends it. */
async function toggleMute($: Engine): Promise<void> {
  const next = !(await read($, muted))
  await update($, muted, () => next)
  await touch($, 'listen.mute', next ? '1' : '0')
}

/** Stop Claude's voice, and forget any reading; in a call the mic opens right away. */
async function stop($: Engine): Promise<void> {
  await stopSpeaking($) // in a call, watchPlayback notices and listens again
  await update($, reading, () => NOT_READING)
  if (!(await read($, isCall))) await setPhase($, 'idle')
}

// ---- settings ----------------------------------------------------------------------

const LISTENING: readonly [string, string][] = [
  ['parakeet:v2', 'Parakeet, English (local, most accurate; default)'],
  ['parakeet:v3', 'Parakeet, 25 European languages (local)'],
  ['apple:', "Mac's own speech recognition, on-device (macOS only)"],
  ['windows:', "Windows' own speech recognition, as in voice typing (Windows only)"],
  ['local:small', 'Local Whisper small (fast)'],
  ['local:medium', 'Local Whisper medium'],
  ['local:large-v3-turbo', 'Local Whisper large v3 turbo'],
  ['local:large-v3', 'Local Whisper large v3 (most accurate Whisper)'],
  ['openai:gpt-4o-transcribe', 'Cloud: OpenAI gpt-4o-transcribe'],
  ['openai:gpt-4o-mini-transcribe', 'Cloud: OpenAI gpt-4o-mini-transcribe'],
]
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
  // The microphones on this machine, newline-separated; the default one tagged.
  const mics = await $.process.run([...py, '-m', 'claudevoice.listen', '--devices'], { cwd: root($), timeoutMs: 60000 })
  try {
    const list = JSON.parse(mics.stdout.trim().split('\n').pop() ?? '[]') as { name: string; default: boolean }[]
    flat._microphones = list.map(d => (d.default ? `${d.name}\t(system default)` : d.name)).join('\n')
  } catch {
    flat._microphones = ''
  }
  const platform = await $.process.run([...py, '-c', 'import sys; print(sys.platform)'], { cwd: root($), timeoutMs: 60000 })
  flat._platform = platform.stdout.trim()
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
  await $.ui.open({ id: SETTINGS_PANE, title: 'Voice settings', focus: true, closeOnEscape: true, rows: 40 })
}

/** "parakeet:<v>", "apple:", "windows:", "local:<size>" or "openai:<model>" from the config. */
function listeningValue(s: VoiceSettings): string {
  const engine = s.stt_engine === 'auto' || s.stt_engine === undefined ? 'parakeet' : s.stt_engine
  if (engine === 'parakeet') return `parakeet:${s.parakeet_model ?? 'v2'}`
  if (engine === 'apple') return 'apple:'
  if (engine === 'windows') return 'windows:'
  if (engine === 'openai') return `openai:${s.stt_model ?? ''}`
  return `local:${(s.whisper_model ?? 'large-v3-turbo').replace(/^.*\/whisper-/, '').replace(/-mlx$/, '')}`
}
/** Whisper is an optional engine: install it the first time someone picks it. */
async function ensureWhisper($: Engine): Promise<boolean> {
  const r = root($)
  await update($, note, () => 'installing Whisper (a few minutes the first time)…')
  $.ui.toast('Installing Whisper, the optional listening engine…')
  const ran = await $.process.run([await uv($), 'sync', '--project', r, '--quiet', '--inexact', '--extra', 'whisper'], { cwd: r, timeoutMs: 600000 })
  await update($, note, () => '')
  if (ran.exitCode !== 0) {
    $.ui.toast(`Whisper could not be installed: ${ran.stderr.trim().split('\n').pop() ?? ''}`)
    return false
  }
  return true
}

async function pickListening($: Engine, value: string): Promise<void> {
  const [engine, model] = value.split(':', 2) as [string, string]
  if (engine === 'local' && !(await ensureWhisper($))) return
  await setSetting($, 'stt_engine', engine)
  if (engine === 'parakeet') await setSetting($, 'parakeet_model', model)
  if (engine === 'local') await setSetting($, 'whisper_model', model)
  if (engine === 'openai') await setSetting($, 'stt_model', model)
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
  else await stop($)
  await setPhase($, 'idle')
}

// ---- drawing -------------------------------------------------------------------------

type Surface = Parameters<Engine['ui']['resolve']>[0]

const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] as const
const WAVE = ['▁▃▅▃', '▃▅▇▅', '▅▇▅▃', '▇▅▃▁', '▅▃▁▃', '▃▁▃▅'] as const
// The app's footer font draws block glyphs wide: one bar there, rising and falling.
const BAR = ['▁', '▃', '▅', '▇', '▅', '▃'] as const

/** How far a reading has got, as a percentage; empty before the server has said. */
function progress(r: ReadingState): string {
  return r.of > 0 ? `${Math.floor((100 * r.at) / r.of)}%` : ''
}

type Status = { glyph: string; color: string; word: string; progress: string }

/** What is happening now: a glyph, its color, one word, and how far a reading has got.
 *  `narrow`: one character of wave instead of four. */
async function status($: Engine, narrow = false): Promise<Status | null> {
  const current = await read($, phase)
  const t = await read($, tick)
  const isMuted = await read($, muted)
  const heardNow = await read($, hearing)
  const r = await read($, reading)
  if (current === 'idle') {
    return r.title !== '' && r.paused ? { glyph: '‖', color: 'warning', word: 'paused', progress: progress(r) } : null
  }
  const spin = SPINNER[t % SPINNER.length] ?? ''
  const looks: Record<Exclude<VoicePhase, 'idle'>, [string, string, string]> = {
    loading: [spin, 'claude', 'starting'],
    listening: isMuted
      ? ['◌', 'warning', 'muted']
      : heardNow === 'hearing'
        ? ['◉', 'success', 'hearing you']
        : [t % 2 === 0 ? '●' : '○', 'error', 'listening'],
    transcribing: [spin, 'claude', 'writing'],
    thinking: [spin, 'claude', 'thinking'],
    speaking: [(narrow ? BAR[t % BAR.length] : WAVE[t % WAVE.length]) ?? '', 'claude', 'speaking'],
  }
  const [glyph, color, word] = looks[current]
  const isReading = current === 'speaking' && r.title !== '' && !r.paused
  return { glyph, color, word: isReading ? 'reading' : word, progress: isReading ? progress(r) : '' }
}

/** A small live indicator: a glyph and one word, set beside the gear. */
async function indicator($: Engine, e: Surface) {
  const { Box, Text } = $.ui.resolve(e)
  const s = await status($)
  if (s === null) return null
  return (
    <Box key="indicator">
      <Text color={s.color}>{s.glyph}</Text>
      <Text dimColor> {`${s.word} ${s.progress}`.trim()}</Text>
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
  const r = await read($, reading)
  const isReading = !inCall && r.title !== ''
  await read($, chat) // redraw when this chat's settings change

  return (
    <Box flexDirection="column">
      <Box>
        {await indicator($, e)}
        <Box flexGrow={1} />
        {!inCall && <Button key="read" label="Read aloud" hotkey="r" plain onPress={() => void readAloud($)} />}
        {!inCall && <Text> </Text>}
        {isReading && current === 'speaking' && !r.paused && <Button key="pause" label="Pause" hotkey="p" plain onPress={() => void pauseReading($)} />}
        {isReading && r.paused && current !== 'speaking' && <Button key="resume" label="Resume" hotkey="p" onPress={() => void resumeReading($)} />}
        {isReading && <Text> </Text>}
        {inCall && current === 'listening' && <Button key="done" label="Done" hotkey="d" variant="primary" onPress={() => void done($)} />}
        {inCall && current === 'listening' && <Text> </Text>}
        {inCall && current === 'listening' && <Button key="mute" label={(await read($, muted)) ? 'Unmute' : 'Mute'} hotkey="u" plain onPress={() => void toggleMute($)} />}
        {inCall && current === 'listening' && <Text> </Text>}
        {(current === 'speaking' || (isReading && r.paused)) && <Button key="stop" label="Stop" hotkey="s" plain onPress={() => void stop($)} />}
        {(current === 'speaking' || (isReading && r.paused)) && <Text> </Text>}
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

/** The controls drawn inside the prompt footer, under the mic, styled like the footer's
 *  own items. Only what can be used right now is shown; a small indicator sits by the gear.
 *
 *  The app gives the footer about 220 pixels and cuts whatever passes them to "…",
 *  buttons included, and draws plain text after the buttons. So on the app every state
 *  is kept to about 200 pixels: the live glyph rides in the gear button's label (one
 *  bar of wave, not four), a word only where few buttons leave room (starting, writing,
 *  thinking), "End" for "End call", no Call button while a file is read, nothing
 *  that changes width on hover, and no glyph while muted (Unmute says it). */
async function footer($: Engine, e: Surface, modes: readonly string[]) {
  const { Box, Button, Text } = $.ui.resolve(e)
  const isOn = await isEnabled($)
  const current = await read($, phase)
  const inCall = await read($, isCall)
  const isMuted = await read($, muted)
  const micOpen = inCall && (await read($, take)) !== ''
  const r = await read($, reading)
  const isReading = !inCall && r.title !== ''
  // The terminal separates footer items with dots; the app spaces its buttons itself.
  const isTerminal = e.surface === 'terminal'
  const quiet = { dimColor: true, hover: { dimColor: false, scope: 'voice' } } as const
  // Bold on hover widens a button, which on the app can push the row past its width.
  const lit = { hover: isTerminal ? { bold: true, scope: 'voice' } : { underline: true, scope: 'voice' } } as const
  const playing = current === 'speaking' || (isReading && r.paused)
  const items: JSX.Element[] = []
  const add = (el: JSX.Element) => {
    if (isTerminal && items.length > 0) items.push(<Text key={`sep-${items.length}`} dimColor> · </Text>)
    items.push(el)
  }

  if (modes.length > 0) add(<Text key="modes" dimColor>{modes.join(' & ')}</Text>)
  if (isOn) {
    // While something plays, Pause/Resume and Stop take Read aloud's place.
    if (!inCall && !playing) add(<Button key="read" label="Read aloud" plain {...quiet} onPress={() => void readAloud($)} />)
    if (inCall && current === 'listening') add(<Button key="done" label="Done" plain {...lit} onPress={() => void done($)} />)
    if (isReading && current === 'speaking' && !r.paused) add(<Button key="pause" label="Pause" plain {...lit} onPress={() => void pauseReading($)} />)
    if (isReading && r.paused && current !== 'speaking') add(<Button key="resume" label="Resume" plain {...lit} onPress={() => void resumeReading($)} />)
    if (playing) add(<Button key="stop" label="Stop" plain {...lit} onPress={() => void stop($)} />)
    // Mute while the mic can hear you: your turn, or Claude speaking with talk-over on.
    if (inCall && (current === 'listening' || (micOpen && current === 'speaking'))) {
      add(<Button key="mute" label={isMuted ? 'Unmute' : 'Mute'} plain {...(isMuted ? lit : quiet)} onPress={() => void toggleMute($)} />)
    }
  }
  let trailing: JSX.Element | null = null // a word the app draws after the buttons
  if (isTerminal) {
    const live = isOn ? await indicator($, e) : null
    add(
      <Box key="gear-box">
        {live}
        {live && <Text> </Text>}
        <Button key="settings" label="⚙" plain {...quiet} onPress={() => void openSettings($)} />
      </Box>,
    )
  } else {
    const s = isOn ? await status($, true) : null
    const worded = s !== null && (current === 'loading' || current === 'transcribing' || current === 'thinking')
    // A button's label is plain text: the glyph (and a reading's progress) go in it, lit.
    const inGear = s !== null && !worded && !(inCall && isMuted)
      ? `${s.glyph}${s.progress !== '' ? ` ${s.progress}` : ''} `
      : ''
    add(<Button key="settings" label={`${inGear}⚙`} plain {...(inGear !== '' ? lit : quiet)} onPress={() => void openSettings($)} />)
    if (worded) trailing = await indicator($, e)
  }
  if (isOn) {
    if (inCall) add(<Button key="end" label={isTerminal ? 'End call' : 'End'} plain {...lit} onPress={() => void endCall($, '')} />)
    else if (isTerminal || !isReading) add(<Button key="call" label="● Call" plain {...lit} onPress={() => void startCall($)} />)
  }
  if (trailing) items.push(trailing)
  return <Box flexShrink={0} alignItems="center">{items}</Box>
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
  const listening: Option[] = LISTENING.map(([value, label]) => ({ value, label }))
  const microphones: Option[] = [
    { value: '', label: 'System default (follows your system)' },
    ...(s._microphones ?? '').split('\n').filter(x => x !== '').map(line => {
      const [name, tag] = line.split('\t') as [string, string | undefined]
      return { value: name, label: tag ? `${name} ${tag}` : name }
    }),
  ]
  const speaking: Option[] = [
    ...KOKORO_VOICES.map(([v, label]) => ({ value: `kokoro:${v}`, label: `Local Kokoro: ${label}` })),
    ...OPENAI_VOICES.map(v => ({ value: `openai:${v}`, label: `Cloud OpenAI: ${v}` })),
    { value: 'elevenlabs:', label: `Cloud ElevenLabs: voice ${s.tts_voice ?? ''}` },
  ]
  const speeds: Option[] = SPEEDS.map(x => ({ value: x, label: x === '1.0' ? '1.0 (natural)' : x }))
  const modes: Option[] = [
    { value: 'live', label: 'Live: Claude answers when I have finished talking' },
    { value: 'hold', label: 'Hold: Claude answers when I press Done' },
  ]
  const pauses: Option[] = [
    { value: 'short', label: 'Short: 0.3 s after a finished sentence, 3 s mid-thought' },
    { value: 'normal', label: 'Normal: 0.6 s after a finished sentence, 5 s mid-thought' },
    { value: 'long', label: 'Long: 1.2 s after a finished sentence, 9 s mid-thought' },
    { value: 'long2', label: 'Long ×2: 12 s after a finished sentence, 18 s mid-thought' },
    { value: 'long3', label: 'Long ×3: 20 s after a finished sentence, 27 s mid-thought' },
    { value: 'long4', label: 'Long ×4: 30 s after a finished sentence, 36 s mid-thought' },
    { value: 'long5', label: 'Long ×5: waits 1 minute of silence' },
    { value: 'long6', label: 'Long ×6: waits 1.5 minutes of silence' },
    { value: 'long7', label: 'Long ×7: waits 2 minutes of silence' },
    { value: 'long8', label: 'Long ×8: waits 3 minutes of silence' },
    { value: 'long9', label: 'Long ×9: waits 4 minutes of silence' },
    { value: 'long10', label: 'Long ×10: waits 5 minutes of silence' },
  ]
  const onOff: Option[] = [
    { value: 'on', label: 'On: talking while Claude speaks stops its voice' },
    { value: 'off', label: 'Off (better without headphones)' },
  ]
  const mode = await read($, listenMode)
  const pause = await read($, patience)
  const over = await read($, talkOver)
  const remember = (key: string, value: unknown) => void $.store.set(key, value)
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
        {row('listenMode', 'When you talk', modes, mode, (v: string) => {
          void update($, listenMode, () => (v === 'hold' ? 'hold' : 'live'))
          remember(MODE_STORE_KEY, v)
        })}
        {row('patience', 'Pause before Claude answers (live)', pauses, pause, (v: string) => {
          void (async () => {
            await update($, patience, () => asPatience(v))
            await shareLiveSettings($)
          })()
          remember(PATIENCE_STORE_KEY, v)
        })}
        {row('talkOver', 'Talk over Claude (live)', onOff, over ? 'on' : 'off', (v: string) => {
          void (async () => {
            await update($, talkOver, () => v === 'on')
            await shareLiveSettings($)
          })()
          remember(TALK_OVER_STORE_KEY, v === 'on')
        })}
        {row('listening', 'Listening (speech to text)', listening, listeningValue(s), (v: string) => void pickListening($, v))}
        {row('microphone', 'Microphone', microphones, s.input_device ?? '', (v: string) => void setSetting($, 'input_device', v))}
        {row('speaking', 'Speaking (text to speech)', speaking, speakingValue(s), (v: string) => void pickSpeaking($, v))}
        {row('speed', 'Speed', speeds, String(Number(s.speed ?? '1.05')), (v: string) => void setSetting($, 'speed', v))}
      </Box>
      <Box marginTop={1}>
        <Text dimColor>Parakeet runs on this computer and downloads about 650 MB once. Cloud engines read OPENAI_API_KEY and ELEVENLABS_API_KEY from your environment. Set the ElevenLabs voice id with: claudevoice config tts_voice ID</Text>
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
    await $.command.register({
      name: 'read',
      description: 'Read a file aloud (markdown, text, PDF); a number starts from that line or page',
      argumentHint: '[file] [line or page]',
    })
    await update($, isCall, () => false)
    await update($, phase, () => 'idle')
    await update($, take, () => '')
    await update($, hearing, () => '')
    const savedEnabled = await $.store.get(ENABLED_STORE_KEY)
    await update($, enabled, () => savedEnabled !== false)
    const savedMode = await $.store.get(MODE_STORE_KEY)
    await update($, listenMode, () => (savedMode === 'hold' ? 'hold' : 'live'))
    const savedPatience = await $.store.get(PATIENCE_STORE_KEY)
    await update($, patience, () => asPatience(savedPatience))
    const savedTalkOver = await $.store.get(TALK_OVER_STORE_KEY)
    await update($, talkOver, () => savedTalkOver !== false)
    const savedChat = await $.store.get(await chatStoreKey($))
    await update($, chat, () => (savedChat !== null && typeof savedChat === 'object' ? (savedChat as ChatSettings) : {}))
    // Only what is copied from now on counts as "just copied".
    void clipboard($).then(text => {
      if (lastCopied === null) lastCopied = text
    })
    return next(e)
  })

  // /read notes.md, /read guide.pdf 12 (from page 12); /read alone is the Read aloud button.
  on('command.run', { command: 'read' }, async ($, e) => {
    const args = e.args.trim()
    if (args === '') {
      await readAloud($)
      return { text: '' }
    }
    const from = /^(.*?)\s+(\d+)$/.exec(args)
    const name = (from?.[1] ?? args).replace(/^@/, '').replace(/^(["'])(.*)\1$/, '$2')
    const file = await findFile($, name)
    if (file === null) return { text: `No file called ${name} here.` }
    await readDocument($, from ? { path: file.path, start: Number(from[2]) } : file)
    return { text: '' }
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

  // In a call, every finished reply is spoken, and the mic opens for your turn.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined || !(await read($, isCall))) return result
    const answer = e.answer.trim()
    $.clock.after(0, () => {
      void (async () => {
        if (answer !== '' && !e.isAborted && (await speak($, answer))) {
          await setPhase($, 'speaking')
          watchPlayback($)
          // Live: the mic opens while Claude talks, so you can talk over it.
          if ((await modeHere($)) === 'live') void listenOnce($)
        } else {
          void listenOnce($)
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

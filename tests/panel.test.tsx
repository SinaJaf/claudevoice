import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const BAND = {
  plugin: 'claudevoice',
  component: 'SessionMode',
  requestId: 'session-mode',
  props: { modes: ['Auto'] },
} as const

type Fetch = { url: string; body: string }

/** The gear's label on the app: the live glyph, and a reading's progress, before the ⚙. */
async function gear(ui: { find: (q: { key: string }) => Promise<{ text?: string } | undefined> }): Promise<string> {
  return (await ui.find({ key: 'settings' }))?.text ?? ''
}
const WAVE = /[▁▃▅▇]/

type Reading = { title: string; at: number; of: number; paused: boolean } | null

/** The world beneath the mod: a running Kokoro server, a session with one reply. */
function world(on: On, state: { speaking: boolean; reading?: Reading }, opts: { mac?: boolean; fresh?: boolean; latest?: string; nvidia?: boolean } = {}) {
  // A fresh machine: no uv and no venv until the plugin installs them.
  const machine = { uv: opts.fresh !== true, venv: opts.fresh !== true }
  // The voice server: started by the plugin from its folder, which /health then reports. One
  // already running at first says no folder, as one an earlier version started would.
  const server = { root: '', down: false, held: false, old: false }
  // Which windows show this chat: none while you are in another chat.
  const presence = { surfaces: ['desktop'] as ('desktop' | 'terminal')[] }
  on('session.surfaces', () => ({ value: presence.surfaces }))
  on('classic.PermissionRequest', () => ({}))
  on('session.attach', (_, e) => {
    presence.surfaces = [e.surface as 'desktop']
    return { clientId: e.clientId }
  })
  on('session.detach', (_, e) => {
    presence.surfaces = []
    return { clientId: e.clientId }
  })
  const fetches: Fetch[] = []
  const runs: string[][] = []
  const submitted: string[] = []
  const written: string[] = []
  const writes: { path: string; content: string }[] = []
  const opened: string[] = []
  const shown = new Set<string>()
  on('command.register', () => ({ value: { command: 'voice-panel' } }))
  on('ui.open', (_, e) => {
    opened.push(e.id)
    shown.add(e.id)
    return { value: { isPlaced: true } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  const messages = [
    { role: 'user' as const, text: 'say hi', toolUses: [] },
    { role: 'assistant' as const, text: 'Hello **there**, friend.', toolUses: [] },
  ]
  on('session.messages', () => ({ value: [...messages] }))
  on('http.fetch', (_, e) => {
    const body = e.init?.body ?? ''
    fetches.push({ url: e.url, body })
    if (e.url.startsWith('https://raw.githubusercontent.com/SinaJaf/claudevoice/')) {
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ version: opts.latest ?? '0.4.4' }) } }
    }
    if (server.down) return { value: { status: 503, ok: false, headers: {}, text: '{}' } }
    if (server.old && e.url.includes('/health?')) return { value: { status: 404, ok: false, headers: {}, text: '{"error":"not found"}' } }
    let answer: Record<string, unknown> = { ok: true }
    if (e.url.endsWith('/speak')) {
      state.speaking = true
      server.held = false
      if (state.reading) state.reading = { ...state.reading, paused: true } // a reply cuts in
    }
    if (e.url.endsWith('/stop')) {
      state.speaking = false
      server.held = false
      state.reading = null
    }
    // The server reads a file or a selection, and keeps its place.
    if (e.url.endsWith('/read')) {
      const asked = JSON.parse(body) as { path?: string; resume?: boolean }
      if (asked.resume) {
        if (!state.reading && !server.held) return { value: { status: 409, ok: false, headers: {}, text: JSON.stringify({ ok: false, error: 'nothing to resume' }) } }
        if (state.reading) state.reading = { ...state.reading, paused: false }
        server.held = false
      } else {
        const title = asked.path?.split('/').pop() ?? 'selection'
        state.reading = { title, at: 0, of: 40, paused: false }
        answer = { ok: true, title, pieces: 40, pages: title.endsWith('.pdf') ? 12 : 0 }
      }
      state.speaking = true
    }
    if (e.url.endsWith('/pause')) {
      server.held = state.speaking
      state.speaking = false
      if (state.reading) state.reading = { ...state.reading, at: 10, paused: true }
    }
    if (e.url.includes('/health?owner=chat-1')) answer = { ok: true, speaking: state.speaking, held: server.held, reading: state.reading ?? null, ...(server.root ? { root: server.root } : {}) }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(answer) } }
  })
  const results = new Map<string, string>()
  // The listener answers through its --result file: at once ('now', as when Smart Turn
  // hears you finish), or only after Done ('on-done').
  const listener = { answer: 'now' as 'now' | 'on-done', done: false }
  // The microphones the listener reports.
  const mics = { list: [{ name: 'Built-in Microphone', default: true }] as { name: string; default: boolean }[] }
  // The clipboard, as wl-paste reads it (this test machine is neither a Mac nor Windows).
  const clip = { text: 'something copied long ago' }
  // On a Mac, the helper reports the clipboard (with its change count) and, asked about the
  // app, what is highlighted in it.
  const mac = { changeCount: 5, highlighted: '', openFile: [] as string[], asked: [] as string[][] }
  on('process.run', (_, e) => {
    if (e.argv[0] === 'wl-paste') return { value: { exitCode: 0, stdout: clip.text, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (e.argv[0] === 'git') {
      // The project's tracked files, for a bare name the Files pane shows.
      const tracked = ['README.md', 'docs/guide.md', 'docs/todo.md', 'old/todo.md']
      const name = (e.argv[e.argv.length - 1] ?? '').replace(':(glob)**/', '')
      const stdout = tracked.filter(f => f === name || f.endsWith(`/${name}`)).join('\n')
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[0] === '/usr/bin/osascript') {
      mac.asked.push([...e.argv])
      const inApp = e.argv.includes('app')
      const stdout = JSON.stringify({
        changeCount: mac.changeCount, clipboard: clip.text, selection: inApp ? mac.highlighted : '',
        areas: inApp ? 2 : 0, lengths: inApp ? [0, mac.highlighted.length] : [], openFile: inApp ? mac.openFile : [],
      })
      return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    runs.push([...e.argv])
    if (e.argv[0] === 'uv' && e.argv[1] === '--version') {
      return { value: { exitCode: machine.uv ? 0 : 127, stdout: '', stderr: 'not found', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[0] === 'sh' && (e.argv[2] ?? '').includes('astral.sh/uv/install.sh')) machine.uv = true
    if (e.argv.includes('sync')) machine.venv = true
    if (e.argv.includes('stop-server')) {
      server.down = true
      server.old = false
    }
    if (e.argv.slice(-2).join(' ') === 'claudevoice start') {
      server.down = false
      server.root = (e.argv[0] ?? '').replace(/\/\.venv\/bin\/python$/, '')
    }
    const at = e.argv.indexOf('--result')
    if (at >= 0) {
      listener.done = false
      results.set(e.argv[at + 1] ?? '', JSON.stringify({ text: 'what time is it', seconds: 2.1 }))
    }
    const isListen = e.argv.includes('claudevoice.listen') && !e.argv.includes('--warm')
    const isConfigRead = e.argv.includes('config') && e.argv.length === 4
    const isQwenCheck = e.argv.includes('qwen-check')
    const qwenFound = opts.mac === false
      ? (opts.nvidia === true ? { can: true, why: '', light: false } : { can: false, why: 'it needs a newer card', light: false })
      : { can: true, why: '', light: true }
    const stdout = isQwenCheck ? JSON.stringify(qwenFound) : e.argv.includes('--devices')
      ? JSON.stringify(mics.list)
      : isListen
        ? JSON.stringify({ text: 'what time is it', seconds: 2.1 })
        : isConfigRead
          ? JSON.stringify({ voice: 'af_heart', tts_engine: 'kokoro', stt_engine: 'local', whisper_model: 'mlx-community/whisper-large-v3-turbo', speed: 1.05, tts_voice: 'nova' })
          : JSON.stringify({ ok: true })
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.close', (_, e) => {
    shown.delete(e.id)
    return { value: undefined }
  })
  on('ui.panes', () => ({ value: [...shown].map(id => ({ id, title: id, isShown: true, isFocused: false, isPlaced: true })) }))
  on('session.id', () => ({ value: 'chat-1' }))
  const box = { text: '' }
  on('prompt.read', () => ({ value: { text: box.text, cursor: box.text.length } }))
  on('prompt.fill', (_, e) => {
    box.text = e.text
    return { isFilled: true }
  })
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('turn.start', () => ({ turnId: 't0' }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  const toasts: string[] = []
  on('ui.toast', (_, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  // The machine beneath: a venv already set up, a home folder.
  const answered = (path: string) => results.has(path) && (listener.answer === 'now' || listener.done)
  // Who holds the one microphone: a file every chat can read.
  const mic = { owner: '' }
  on('fs.exists', (_, e) => ({
    value: (machine.venv && e.path.endsWith('/.venv/bin/python')) || answered(e.path) || (opts.mac === true && e.path === '/usr/bin/osascript')
      || (machine.uv && e.path === '/home/tester/.local/bin/uv') || (e.path.endsWith('/mic.owner') && mic.owner !== ''),
  }))
  // The project's files, and what is selected on screen.
  const files = new Set(['/proj/notes.md', '/proj/docs/guide.pdf', '/home/tester/my notes.md', '/proj/README.md', '/proj/docs/guide.md', '/apps/claude.app/Contents/MacOS/claude'])
  on('fs.stat', (_, e) => {
    if (!files.has(e.path)) throw Object.assign(new Error(`ENOENT: ${e.path}`), { code: 'ENOENT' })
    return { value: { kind: 'file' as const, size: 100, mtimeMs: 0, isLink: false } }
  })
  on('session.cwd', () => ({ value: '/proj' }))
  const selection = { text: '' }
  on('ui.selection', () => ({ value: selection.text === '' ? undefined : { text: selection.text } }))
  const contents = new Map<string, string>()
  on('fs.read', (_, e) => ({
    value: e.path.endsWith('/.claude-plugin/plugin.json')
      ? JSON.stringify({ version: '0.4.4' })
      : e.path.endsWith('/mic.owner') ? mic.owner : (contents.get(e.path) ?? results.get(e.path) ?? ''),
  }))
  // The desktop app names the claude it runs: its folder, with the command in MacOS/.
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/tester' : e.name === 'CLAUDE_CODE_EXECPATH' ? '/apps/claude.app/Contents/' : undefined }))
  on('fs.write', (_, e) => {
    if (e.path.endsWith('listen.done')) listener.done = true
    if (e.path.endsWith('/mic.owner')) mic.owner = e.text
    written.push(e.path)
    writes.push({ path: e.path, content: e.text })
    return { value: undefined }
  })
  return { fetches, runs, submitted, written, writes, opened, box, listener, selection, clip, mac, mics, toasts, presence, mic, server, messages, contents }
}

test('Read aloud speaks the last reply through the Kokoro server', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Button', text: /Read aloud/ })).toBeDefined()
    expect(await ui.find({ type: 'Button', text: /Call/ })).toBeDefined()
    await ui.press({ key: 'read' })
    expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello **there**, friend.')
    if (surface === 'terminal') expect((await ui.find({ type: 'Text', text: /speaking/ }))?.text).toContain('speaking')
    else expect(await gear(ui)).toMatch(WAVE)
    await ui.press({ key: 'stop' })
    expect(w.fetches.some(f => f.url.endsWith('/stop'))).toBe(true)
    await clock.advance(400)
    expect(await ui.find({ type: 'Text', text: /speaking/ })).toBeUndefined()
    if (surface === 'desktop') expect(await gear(ui)).toBe('⚙')
    await ui.unmount()
    w.fetches.length = 0
  }
})

const isListen = (argv: string[]) => argv.includes('claudevoice.listen') && !argv.includes('--warm')

test('A live call: the mic opens while Claude talks, the turn ends when you finish, the answer is spoken', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /Auto/ })).toBeDefined()

  await ui.press({ key: 'call' })
  expect(w.runs.some(argv => argv.includes('--warm'))).toBe(true)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain("answer when you've finished")
  // Live: the listener starts during the greeting, so you can talk over it.
  const listen = w.runs.find(isListen)
  expect(listen).toContain('--live')
  expect(listen).toContain('--talk-over')
  expect(listen).toContain('--patience=normal')
  expect(listen).toContain('--detach')
  expect(listen).toContain('--heartbeat')
  // While Claude speaks: Stop and Mute, no Done.
  expect(await ui.find({ key: 'stop' })).toBeDefined()
  expect(await ui.find({ key: 'mute' })).toBeDefined()
  expect(await ui.find({ key: 'done' })).toBeUndefined()

  // Greeting ends: the same listener carries on; Done is there as a fallback.
  state.speaking = false
  await clock.advance(400)
  expect(w.runs.filter(isListen).length).toBe(1)
  expect(await gear(ui)).toMatch(/[●○] ⚙/)
  expect(await ui.find({ key: 'done' })).toBeDefined()
  expect(w.submitted).toEqual([])

  // Smart Turn hears you finish: your words are sent.
  w.listener.answer = 'now'
  await clock.advance(400)
  expect(w.submitted).toEqual(['what time is it'])

  // Claude answers: the reply is spoken and the mic opens at once, for talk-over.
  w.fetches.length = 0
  w.runs.length = 0
  await $.turn.complete({ answer: 'It is about noon.', reason: 'answer', durationMs: 50, isAborted: false, turnId: 't1' })
  await clock.advance(10)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('It is about noon.')
  expect(w.runs.filter(isListen).length).toBe(1)

  // Hang up: the listener is told to stop and the footer offers Call again.
  await ui.press({ key: 'end' })
  expect(w.written.some(p => p.endsWith('listen.stop'))).toBe(true)
  expect(await ui.find({ type: 'Button', text: /Call/ })).toBeDefined()
  await ui.unmount()
})

test('The pause setting reaches the listener, even one already running', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'patience', value: 'long4' })
  await pane.unmount()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  expect(w.runs.find(isListen)).toContain('--patience=long4')
  expect(w.writes.filter(x => x.path.endsWith('listen.settings')).pop()?.content).toContain('"patience":"long4"')

  // Changed while the listener is already running: the running listener is told at once.
  const pane2 = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane2.select({ key: 'patience', value: 'long2' })
  await pane2.unmount()
  expect(w.writes.filter(x => x.path.endsWith('listen.settings')).pop()?.content).toContain('"patience":"long2"')
  expect(w.runs.filter(isListen).length).toBe(1)
  await ui.press({ key: 'end' })
  await ui.unmount()
})

test('Hold mode: the mic opens after Claude speaks and records until Done', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'listenMode', value: 'hold' })
  await pane.unmount()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('press Done')
  expect(w.runs.some(isListen)).toBe(false)
  state.speaking = false
  await clock.advance(400)
  const listen = w.runs.find(isListen)
  expect(listen).toContain('--manual')
  expect(listen).not.toContain('--live')
  await ui.press({ key: 'done' })
  expect(w.written.some(p => p.endsWith('listen.done'))).toBe(true)
  expect(await ui.find({ type: 'Text', text: /writing/ })).toBeDefined()
  await clock.advance(400)
  expect(w.submitted).toEqual(['what time is it'])
  await ui.unmount()
})

const SETTINGS = {
  plugin: 'claudevoice',
  component: 'Pane',
  requestId: 'claudevoice-settings',
  props: {
    title: 'Voice settings',
    isFocused: true,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

test('The gear opens settings; picking a cloud voice writes the config', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  const bar = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await bar.find({ type: 'Button', text: /⚙/ })).toBeDefined()
  await bar.press({ key: 'settings' })
  expect(w.opened).toContain('claudevoice-settings')
  expect(w.runs.some(argv => argv.join(' ').endsWith('claudevoice config'))).toBe(true)
  await bar.unmount()

  for (const surface of ['terminal', 'desktop'] as const) {
    const pane = await $.ui.mount({ ...SETTINGS, surface })
    expect(await pane.find({ type: 'Text', text: /Speaking/ })).toBeDefined()
    await pane.select({ key: 'speaking', value: 'openai:nova' })
    expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config tts_engine openai')).toBe(true)
    expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config tts_voice nova')).toBe(true)
    await pane.select({ key: 'listening', value: 'parakeet:v3' })
    expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config stt_engine parakeet')).toBe(true)
    expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config parakeet_model v3')).toBe(true)
    await pane.select({ key: 'speed', value: '1.15' })
    expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config speed 1.15')).toBe(true)
    await pane.unmount()
  }
})

test('Switched off, only the gear stays; switched on, the controls return', async ($, on) => {
  const state = { speaking: false }
  world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'plugin', value: 'off' })
  await pane.unmount()
  for (const surface of ['terminal', 'desktop'] as const) {
    const footer = await $.ui.mount({ ...BAND, surface })
    expect(await footer.find({ type: 'Button', text: /⚙/ })).toBeDefined()
    expect(await footer.find({ type: 'Button', text: /Call/ })).toBeUndefined()
    expect(await footer.find({ type: 'Button', text: /Read aloud/ })).toBeUndefined()
    await footer.unmount()
  }
  const again = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await again.select({ key: 'plugin', value: 'on' })
  await again.unmount()
  const footer = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await footer.find({ type: 'Button', text: /Call/ })).toBeDefined()
  await footer.unmount()
})

test('Stop cuts the voice and hands you the turn; Mute pauses the mic without losing the take', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  expect(await gear(ui)).toMatch(WAVE)
  await ui.press({ key: 'stop' })
  expect(w.fetches.some(f => f.url.endsWith('/stop'))).toBe(true)
  await clock.advance(400)
  expect(await gear(ui)).toMatch(/[●○] ⚙/)

  await ui.press({ key: 'mute' })
  expect(w.writes.filter(x => x.path.endsWith('listen.mute')).pop()?.content).toBe('1')
  expect(w.written.some(p => p.endsWith('listen.stop'))).toBe(false)
  // Muted: Unmute says so, and the gear carries no glyph (room for End on the app).
  expect(await ui.find({ type: 'Button', text: /Unmute/ })).toBeDefined()
  expect(await gear(ui)).toBe('⚙')
  await ui.press({ key: 'mute' })
  expect(w.writes.filter(x => x.path.endsWith('listen.mute')).pop()?.content).toBe('0')
  await ui.unmount()
})

test('A setting chosen for this chat only overrides the all-chats one here', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  // The dropdown next to the title opens this chat's own choice.
  await pane.press({ key: 'chat-speaking' })
  expect(await pane.find({ type: 'Text', text: /This chat only/ })).toBeDefined()
  await pane.select({ key: 'chat-speaking-select', value: 'kokoro:bf_emma' })
  await pane.press({ key: 'chat-speed' })
  await pane.select({ key: 'chat-speed-select', value: '1.25' })
  // Nothing was written to the all-chats config.
  expect(w.runs.some(argv => argv.includes('config') && argv.length > 4)).toBe(false)
  await pane.unmount()

  // Speech in this chat uses its own voice and speed.
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  const greet = JSON.parse(w.fetches.find(f => f.url.endsWith('/speak'))?.body ?? '{}') as Record<string, unknown>
  expect(greet.voice).toBe('bf_emma')
  expect(greet.engine).toBe('kokoro')
  expect(greet.speed).toBe(1.25)
  state.speaking = false
  await clock.advance(400)
  await ui.press({ key: 'end' })
  await ui.unmount()

  // Back to "Same as all chats": the override is gone.
  const again = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await again.select({ key: 'chat-speaking-select', value: '__all__' })
  w.fetches.length = 0
  const bar = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await bar.press({ key: 'read' })
  const read = JSON.parse(w.fetches.find(f => f.url.endsWith('/speak'))?.body ?? '{}') as Record<string, unknown>
  expect(read.voice).toBeUndefined()
  expect(read.speed).toBe(1.25)
  await bar.unmount()
  await again.unmount()
})

/** The last /read request, without the chat it names, which every request carries. */
const readBody = (w: { fetches: Fetch[] }) => {
  const { owner: _owner, front: _front, ...rest } = JSON.parse(w.fetches.filter(f => f.url.endsWith('/read')).pop()?.body ?? '{}') as Record<string, unknown>
  return rest
}

test('Read aloud reads your selection, else a file named in the prompt box, else the last reply', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })

  w.messages.splice(1, 0, { role: 'assistant', text: 'First this. **The second section**, as selected. Then more.', toolUses: [] })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    // Text selected in the chat: that is what is read.
    w.selection.text = 'The second section, as selected.'
    await ui.press({ key: 'read' })
    expect(readBody(w)).toEqual({ text: 'The second section, as selected.', title: 'selection' })
    await ui.press({ key: 'stop' })
    await clock.advance(400)

    // Nothing selected, a file mentioned in the box: the file from its start.
    w.selection.text = ''
    w.box.text = 'read this @notes.md please'
    await ui.press({ key: 'read' })
    expect(readBody(w)).toEqual({ path: '/proj/notes.md' })
    await ui.press({ key: 'stop' })
    await clock.advance(400)

    // A mention with lines, a quoted one with spaces, a dragged-in path.
    w.box.text = '@docs/guide.pdf#L3-5'
    await ui.press({ key: 'read' })
    expect(readBody(w)).toEqual({ path: '/proj/docs/guide.pdf', start: 3, end: 5 })
    await ui.press({ key: 'stop' })
    w.box.text = '@"~/my notes.md"'
    await ui.press({ key: 'read' })
    expect(readBody(w).path).toBe('/home/tester/my notes.md')
    await ui.press({ key: 'stop' })
    w.box.text = '/proj/notes.md'
    await ui.press({ key: 'read' })
    expect(readBody(w).path).toBe('/proj/notes.md')
    await ui.press({ key: 'stop' })
    await clock.advance(400)

    // A prompt that names no file: the last reply, as before.
    w.box.text = 'fix the failing test'
    w.fetches.length = 0
    await ui.press({ key: 'read' })
    expect(w.fetches.some(f => f.url.endsWith('/read'))).toBe(false)
    expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello **there**, friend.')
    await ui.press({ key: 'stop' })
    await clock.advance(400)
    w.box.text = ''
    await ui.unmount()
  }
})

test('A reading can be paused and resumed where it stopped; Stop forgets it', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  w.box.text = '@notes.md'
  await ui.press({ key: 'read' })
  await clock.advance(400)
  expect(await gear(ui)).toMatch(/^[▁▃▅▇] 0% ⚙$/)
  expect(await ui.find({ key: 'pause' })).toBeDefined()
  expect(await ui.find({ key: 'resume' })).toBeUndefined()
  // On the app, Call makes room while a file is read.
  expect(await ui.find({ key: 'call' })).toBeUndefined()

  // Pause: the voice stops, the place is kept, Resume and Stop are offered.
  await ui.press({ key: 'pause' })
  await clock.advance(400)
  expect(await gear(ui)).toBe('‖ 25% ⚙')
  expect(await ui.find({ key: 'pause' })).toBeUndefined()
  expect(await ui.find({ key: 'resume' })).toBeDefined()
  expect(await ui.find({ key: 'stop' })).toBeDefined()

  // Resume: the server carries on from its place.
  await ui.press({ key: 'resume' })
  expect(readBody(w)).toEqual({ resume: true })
  await clock.advance(400)
  expect(await gear(ui)).toMatch(/^[▁▃▅▇] 25% ⚙$/)

  // Stop: the reading is gone; Read aloud and Call are back.
  await ui.press({ key: 'stop' })
  await clock.advance(400)
  expect(await ui.find({ key: 'call' })).toBeDefined()
  expect(await ui.find({ key: 'resume' })).toBeUndefined()
  expect(await gear(ui)).toBe('⚙')
  expect(await ui.find({ key: 'read' })).toBeDefined()
  await ui.unmount()
})

const TYPED = { origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 100 } }

test('/read names a file, and a number starts from that line or page', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  await $.command.run({ ...TYPED, command: 'read', args: 'docs/guide.pdf 7' })
  expect(readBody(w)).toEqual({ path: '/proj/docs/guide.pdf', start: 7 })
  await $.command.run({ ...TYPED, command: 'read', args: '@notes.md' })
  expect(readBody(w)).toEqual({ path: '/proj/notes.md' })
  const missing = await $.command.run({ ...TYPED, command: 'read', args: 'nowhere.md' })
  expect(missing.text).toContain('No file called nowhere.md')
})

test('Text copied in the Files pane or another app is read; a copy out of the chat is not', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const spoke = () => w.fetches.find(f => f.url.endsWith('/speak'))?.body ?? ''
  const askedToRead = () => w.fetches.some(f => f.url.endsWith('/read'))

  // On the clipboard before the session began: not "just copied", so the reply is read.
  await ui.press({ key: 'read' })
  expect(askedToRead()).toBe(false)
  expect(spoke()).toContain('Hello **there**, friend.')
  await ui.press({ key: 'stop' })

  // Copied from a file open in the app: that is read.
  w.clip.text = 'A paragraph copied from the Files pane.'
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ text: 'A paragraph copied from the Files pane.', title: 'copied text' })
  await ui.press({ key: 'stop' })

  // Pressed again with nothing new copied: the last reply.
  w.fetches.length = 0
  await ui.press({ key: 'read' })
  expect(askedToRead()).toBe(false)
  expect(spoke()).toContain('Hello')
  await ui.press({ key: 'stop' })

  // A copied path: that file, from its start.
  w.clip.text = '/proj/notes.md\n'
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ path: '/proj/notes.md' })
  await ui.press({ key: 'stop' })

  // Copied out of this chat (a command from a reply, say): the reply is read instead.
  w.clip.text = 'Hello **there**,\n  friend.'
  w.fetches.length = 0
  await ui.press({ key: 'read' })
  expect(askedToRead()).toBe(false)
  expect(spoke()).toContain('Hello')
  await ui.unmount()
})

test('The footer: dots only on the terminal; while something plays, Read aloud makes room', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const term = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await term.find({ type: 'Text', text: /·/ })).toBeDefined()
  await term.unmount()

  const desk = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await desk.find({ type: 'Text', text: /·/ })).toBeUndefined()
  await desk.press({ key: 'read' })
  // Nothing but buttons (and the footer's own mode label) while something plays: no text
  // of ours for the app to cut to "…".
  expect(await desk.find({ type: 'Text', text: /^(?!Auto$)/ })).toBeUndefined()
  expect(await desk.find({ key: 'read' })).toBeUndefined()
  expect(await desk.find({ key: 'stop' })).toBeDefined()
  await desk.press({ key: 'stop' })
  expect(await desk.find({ key: 'read' })).toBeDefined()
  await desk.unmount()
})

test('In a call the app footer stays narrow: End for End call, and nothing widens on hover', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const desk = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await desk.press({ key: 'call' })
  expect((await desk.find({ key: 'end' }))?.text).toBe('End')
  // The greeting plays with the mic open: Stop, Mute, the gear with one bar of wave, End.
  expect(await gear(desk)).toMatch(/^[▁▃▅▇] ⚙$/)
  await desk.press({ key: 'end' })
  await desk.unmount()
  const term = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await term.press({ key: 'call' })
  expect((await term.find({ key: 'end' }))?.text).toBe('End call')
  await term.press({ key: 'end' })
  await term.unmount()
})

test('On a Mac, text highlighted in the Files pane is read, and copying the same text again counts', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state, { mac: true })
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  // Highlighted in a file open in the app (the app itself tells plugins only about the chat).
  w.mac.highlighted = 'A section of the README, highlighted in the Files pane.'
  w.mac.openFile = ['README.md']
  w.contents.set('/proj/README.md', '# Title\n\nA section of the [README](https://example.com), highlighted in the Files pane.\n')
  await ui.press({ key: 'read' })
  expect(w.mac.asked.pop()).toContain('app')
  expect(readBody(w)).toEqual({ text: 'A section of the README, highlighted in the Files pane.', title: 'selection' })
  expect(w.writes.filter(x => x.path.endsWith('last-read.json')).pop()?.content).toContain('"source":"highlighted in the app"')
  // What was read is not written down, only how long it was.
  expect(w.writes.filter(x => x.path.endsWith('last-read.json')).pop()?.content).not.toContain('README')
  await ui.press({ key: 'stop' })
  w.mac.highlighted = ''
  w.mac.openFile = []

  // Copied: read. Copied again, the very same text: read again (the copy counter moved).
  w.clip.text = 'Copied from a PDF in Preview.'
  w.mac.changeCount = 6
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ text: 'Copied from a PDF in Preview.', title: 'copied text' })
  await ui.press({ key: 'stop' })
  w.mac.changeCount = 7
  w.fetches.length = 0
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ text: 'Copied from a PDF in Preview.', title: 'copied text' })
  await ui.press({ key: 'stop' })

  // Nothing new: the last reply.
  w.fetches.length = 0
  await ui.press({ key: 'read' })
  expect(w.fetches.some(f => f.url.endsWith('/read'))).toBe(false)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello')
  await ui.press({ key: 'stop' })
  await ui.unmount()

  // In the terminal, the app is not asked about.
  const term = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await term.press({ key: 'read' })
  expect(w.mac.asked.pop()).not.toContain('app')
  await term.unmount()
})

test('On a Mac, with nothing highlighted, Read aloud reads the whole file open in the Files pane', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state, { mac: true })
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  // The pane's header shows the path in parts, starting above the project folder.
  w.mac.openFile = ['proj', 'docs', 'guide.md']
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ path: '/proj/docs/guide.md' })
  expect(w.writes.filter(x => x.path.endsWith('last-read.json')).pop()?.content).toContain('"source":"file open in the Files pane"')
  await ui.press({ key: 'stop' })

  // Only the name: found at the top of the project, or once among its tracked files.
  w.mac.openFile = ['README.md']
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ path: '/proj/README.md' })
  await ui.press({ key: 'stop' })
  w.mac.openFile = ['guide.md']
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ path: '/proj/docs/guide.md' })
  await ui.press({ key: 'stop' })

  // A name two tracked files share, or a tab that is no file (a web page): the last reply.
  for (const tab of [['todo.md'], ['GitHub - a page']]) {
    w.mac.openFile = tab
    w.fetches.length = 0
    await ui.press({ key: 'read' })
    expect(w.fetches.some(f => f.url.endsWith('/read'))).toBe(false)
    expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello')
    await ui.press({ key: 'stop' })
  }

  // A highlight in that file still wins: just that part is read.
  w.mac.openFile = ['README.md']
  w.contents.set('/proj/README.md', 'An introduction.\n\nOnly this paragraph.\n')
  w.mac.highlighted = 'Only this paragraph.'
  await ui.press({ key: 'read' })
  expect(readBody(w)).toEqual({ text: 'Only this paragraph.', title: 'selection' })
  await ui.unmount()
})

test('Settings draw when a microphone is listed twice (Windows lists one per audio system)', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  mock.clock(on)
  mock.store(on)
  w.mics.list = [
    { name: 'Microphone (Realtek(R) Audio)', default: true },
    { name: 'Microphone (Realtek(R) Audio)', default: false },
    { name: 'Headset Microphone (HyperX Clo', default: false },
  ]
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const bar = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await bar.press({ key: 'settings' })
  await bar.unmount()
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  expect(await pane.find({ key: 'microphone' })).toBeDefined()
  await pane.select({ key: 'microphone', value: 'Headset Microphone (HyperX Clo' })
  // Listed once each, the system default first.
  const mic = await pane.find({ key: 'microphone' }) as { props?: { options?: { value: string }[] } } | undefined
  expect(mic?.props?.options?.map(o => o.value)).toEqual(['', 'Microphone (Realtek(R) Audio)', 'Headset Microphone (HyperX Clo'])
  expect(w.runs.some(argv => argv.slice(-3).join(' ') === 'config input_device Headset Microphone (HyperX Clo')).toBe(true)
  await pane.unmount()
})

test('A voice server an earlier version started is replaced by this one, once', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'read' })
  const stops = () => w.runs.filter(argv => argv.includes('stop-server')).length
  const starts = () => w.runs.filter(argv => argv.slice(-2).join(' ') === 'claudevoice start').length
  expect(stops()).toBe(1)
  expect(starts()).toBe(1)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello')
  await ui.press({ key: 'stop' })
  // Now it is this version's own: used as it is.
  await ui.press({ key: 'read' })
  expect(stops()).toBe(1)
  expect(starts()).toBe(1)
  await ui.unmount()
})

test('On a fresh machine, Read aloud installs uv and the voice itself, saying so as it goes', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state, { fresh: true })
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'read' })
  const installer = w.runs.findIndex(argv => argv[0] === 'sh' && (argv[2] ?? '').includes('astral.sh/uv/install.sh'))
  const sync = w.runs.findIndex(argv => argv[0] === '/home/tester/.local/bin/uv' && argv[1] === 'sync')
  expect(installer).toBeGreaterThanOrEqual(0)
  expect(sync).toBeGreaterThan(installer)
  expect(w.toasts.some(text => text.includes('Installing uv'))).toBe(true)
  expect(w.toasts.some(text => text.includes('Setting up the voice'))).toBe(true)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('Hello')
  await ui.unmount()
})

test('A newer version on GitHub is announced once, and Update now installs it', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state, { latest: '0.5.0' })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(10)
  expect(w.toasts.filter(text => text.includes('0.5.0 is out')).length).toBe(1)

  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  expect(await pane.find({ type: 'Text', text: /0\.5\.0 is out \(you have 0\.4\.4\)/ })).toBeDefined()
  await pane.press({ key: 'update-now' })
  const claude = w.runs.filter(argv => argv[0] === '/apps/claude.app/Contents/MacOS/claude').map(argv => argv.slice(1).join(' '))
  expect(claude).toEqual(['plugin marketplace update claudevoice', 'plugin update claudevoice@claudevoice'])
  expect(w.toasts.some(text => text.includes('/reload-plugins'))).toBe(true)
  expect(await pane.find({ key: 'update-now' })).toBeUndefined()
  await pane.unmount()

  // Looked again within a few hours: GitHub is not asked again.
  const asked = () => w.fetches.filter(f => f.url.includes('raw.githubusercontent.com')).length
  const before = asked()
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(10)
  expect(asked()).toBe(before)
})

// ---- each chat on its own ------------------------------------------------------------------

const CLIENT = { surface: 'desktop', clientId: 'desktop-1' } as const
const THREE_TURNS = { reason: 'answer', durationMs: 50, isAborted: false, turnId: 't1' } as const
const said = (w: { fetches: Fetch[] }) => w.fetches.filter(f => f.url.endsWith('/speak')).map(f => (JSON.parse(f.body) as { text: string }).text)
const muteFlag = (w: { writes: { path: string; content: string }[] }) => w.writes.filter(x => x.path.endsWith('listen.mute')).pop()?.content
const sounded = (w: { runs: string[][] }) => w.runs.filter(argv => argv[0] === 'paplay').map(argv => (argv[1] ?? '').split('/').pop())

test('A call is its own: its listener, its flags and its voice all name this chat', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  const listen = w.runs.find(isListen) ?? []
  expect(listen[listen.indexOf('--chat') + 1]).toBe('chat-1')
  expect(listen[listen.indexOf('--heartbeat') + 1]).toContain('/chats/chat-1/listen.alive')
  expect(w.writes.some(x => x.path.endsWith('/chats/chat-1/listen.mute'))).toBe(true)
  expect(JSON.parse(w.fetches.find(f => f.url.endsWith('/speak'))?.body ?? '{}').owner).toBe('chat-1')
  await ui.press({ key: 'end' })
  // Ending it reaches only this chat: its own stop flag, and a stop that names it.
  expect(w.written.some(p => p.endsWith('/chats/chat-1/listen.stop'))).toBe(true)
  expect(w.written.some(p => p.endsWith('/.claudevoice/listen.stop'))).toBe(false)
  expect(JSON.parse(w.fetches.filter(f => f.url.endsWith('/stop')).pop()?.body ?? '{}')).toEqual({ owner: 'chat-1' })
  await ui.unmount()
})

test('In a call, leaving the chat mutes its mic and holds its voice; coming back picks up', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  expect(muteFlag(w)).toBe('0')
  expect(w.mic.owner).toBe('chat-1')

  // You go to another chat while Claude is speaking: the mic mutes, the voice pauses in place.
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  expect(muteFlag(w)).toBe('1')
  expect(w.fetches.some(f => f.url.endsWith('/pause'))).toBe(true)
  expect(w.mic.owner).toBe('')
  await clock.advance(400)

  // You come back: the voice carries on from there, the mic opens again.
  await $.session.attach(CLIENT)
  await clock.advance(10)
  expect(readBody(w)).toEqual({ resume: true })
  expect(muteFlag(w)).toBe('0')
  expect(w.mic.owner).toBe('chat-1')
  state.speaking = false
  await clock.advance(400)

  // A reply that finishes while you are away is not spoken...
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  w.fetches.length = 0
  await $.turn.complete({ answer: 'It is about noon.', ...THREE_TURNS })
  await clock.advance(10)
  expect(said(w)).toEqual([])

  // ...until you come back to this chat.
  await $.session.attach(CLIENT)
  await clock.advance(10)
  expect(said(w)).toEqual(['It is about noon.'])
  await ui.press({ key: 'end' })
  await ui.unmount()
})

test('Read aloud pauses when you leave and carries on when you return, unless you paused it', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  w.box.text = '@notes.md'
  await ui.press({ key: 'read' })
  await clock.advance(400)
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(400)
  expect(state.reading?.paused).toBe(true)
  await $.session.attach(CLIENT)
  await clock.advance(10)
  expect(state.reading?.paused).toBe(false)
  expect(readBody(w)).toEqual({ resume: true })

  // Paused by you: it stays paused when you come back.
  await ui.press({ key: 'pause' })
  await clock.advance(400)
  w.fetches.length = 0
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  await $.session.attach(CLIENT)
  await clock.advance(10)
  expect(w.fetches.some(f => f.url.endsWith('/read'))).toBe(false)
  expect(state.reading?.paused).toBe(true)
  await ui.unmount()
})

test('Sounds are off until switched on; then one for a ready result, another when it needs you', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  await $.turn.complete({ answer: 'All done.', ...THREE_TURNS })
  await clock.advance(10)
  expect(sounded(w)).toEqual([])

  // Switched on: you hear both, so you know what each means.
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'sounds', value: 'on' })
  await pane.unmount()
  expect(sounded(w)).toEqual(['done.wav', 'attention.wav'])
  w.runs.length = 0

  // A result is ready, in this chat or another: the ready sound, and no voice.
  await $.turn.complete({ answer: 'All done.', ...THREE_TURNS })
  await clock.advance(10)
  expect(sounded(w)).toEqual(['done.wav'])
  expect(said(w)).toEqual([])
  w.runs.length = 0

  // An aborted turn or an empty answer is no result.
  await $.turn.complete({ answer: 'Stopped.', ...THREE_TURNS, isAborted: true })
  await clock.advance(10)
  await $.turn.complete({ answer: '   ', ...THREE_TURNS })
  await clock.advance(10)
  expect(sounded(w)).toEqual([])

  // A permission prompt: the other sound, once, even if a second event says the same.
  await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: {} })
  await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: {} })
  await clock.advance(10)
  expect(sounded(w)).toEqual(['attention.wav'])
})

test('In a call, a reply being spoken needs no ready sound; one held while you are away gets it', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'sounds', value: 'on' })
  await pane.unmount()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  w.runs.length = 0
  await $.turn.complete({ answer: 'Spoken to you here.', ...THREE_TURNS })
  await clock.advance(10)
  expect(sounded(w)).toEqual([])
  expect(said(w)).toContain('Spoken to you here.')
  state.speaking = false
  await clock.advance(400)
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  await $.turn.complete({ answer: 'Ready while you were away.', ...THREE_TURNS })
  await clock.advance(10)
  expect(sounded(w)).toEqual(['done.wav'])
  expect(said(w)).not.toContain('Ready while you were away.')
  await $.session.attach(CLIENT)
  await clock.advance(10)
  await ui.press({ key: 'end' })
  await ui.unmount()
})

test('A chat set to always notify reads its results and says when it needs you, wherever you are', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  await $.turn.complete({ answer: 'Off by default: this is not read.', ...THREE_TURNS })
  await clock.advance(10)
  expect(said(w)).toEqual([])

  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'alwaysNotify', value: 'on' })
  await pane.unmount()

  // In this chat: read as it is.
  await $.turn.complete({ answer: 'Read to you here.', ...THREE_TURNS })
  await clock.advance(10)
  expect(said(w)).toEqual(['Read to you here.'])
  state.speaking = false
  await clock.advance(400)

  // In another chat: read anyway, saying where it is from, and not at the front of the line.
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  w.fetches.length = 0
  await $.turn.complete({ answer: 'The build passed.', ...THREE_TURNS })
  await clock.advance(10)
  expect(said(w)).toEqual(['From proj. The build passed.'])
  expect(JSON.parse(w.fetches.find(f => f.url.endsWith('/speak'))?.body ?? '{}').front).toBe(false)
  state.speaking = false
  await clock.advance(400)

  // When it needs you.
  w.fetches.length = 0
  await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: {} })
  await clock.advance(10)
  expect(said(w)).toEqual(['In proj, Claude needs your permission to use Bash.'])
  state.speaking = false
  await clock.advance(400)

  // Leaving while it reads does not pause it: only moves it behind the chat you are in.
  await $.session.attach(CLIENT)
  await clock.advance(10)
  await $.turn.complete({ answer: 'A long read in this chat.', ...THREE_TURNS })
  await clock.advance(10)
  w.fetches.length = 0
  await $.session.detach({ ...CLIENT, reason: 'detach' })
  await clock.advance(10)
  expect(w.fetches.some(f => f.url.endsWith('/pause'))).toBe(false)
  expect(JSON.parse(w.fetches.find(f => f.url.endsWith('/front'))?.body ?? '{}').front).toBe(false)
})

test('Two calls side by side: the mic stays with the one you last used, and Unmute brings it here', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  state.speaking = false
  await clock.advance(400)
  expect(muteFlag(w)).toBe('0')

  // The other chat on screen starts a call: it takes the mic, this one shows as muted.
  w.mic.owner = 'chat-2'
  await clock.advance(400)
  expect(muteFlag(w)).toBe('1')
  expect((await ui.find({ key: 'mute' }))?.text).toBe('Unmute')

  await ui.press({ key: 'mute' })
  expect(w.mic.owner).toBe('chat-1')
  expect(muteFlag(w)).toBe('0')
  expect((await ui.find({ key: 'mute' }))?.text).toBe('Mute')
  await ui.press({ key: 'end' })
  expect(w.mic.owner).toBe('')
  await ui.unmount()
})

test('A terminal is always here', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.presence.surfaces = ['terminal']
  w.listener.answer = 'on-done'
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'call' })
  w.fetches.length = 0
  await $.turn.complete({ answer: 'Spoken as ever.', ...THREE_TURNS })
  await clock.advance(10)
  expect(said(w)).toEqual(['Spoken as ever.'])
  await ui.press({ key: 'end' })
  await ui.unmount()
})

test('A voice server from before chats had their own voice is replaced, not mistaken for none', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  w.server.old = true
  mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'read' })
  expect(w.runs.filter(argv => argv.includes('stop-server')).length).toBe(1)
  expect(said(w)).toEqual(['Hello **there**, friend.'])
  expect(w.toasts.some(text => text.includes('failed to start'))).toBe(false)
  await ui.unmount()
})

test('A question or a plan waiting on you gets the attention sound; other tools do not', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  on('tool.check', () => ({ decision: 'allow' as const }))
  on('classic.Notification', () => ({}))
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'sounds', value: 'on' })
  await pane.unmount()
  const check = (tool: string, id?: string) => $.tool.check({ tool, input: {}, ...(id ? { tool_use_id: id } : {}) } as never)

  w.runs.length = 0
  await check('Read', 'u0')
  await check('AskUserQuestion') // a query, not a real call
  await clock.advance(10)
  expect(sounded(w)).toEqual([])
  await check('AskUserQuestion', 'u1')
  await clock.advance(10)
  expect(sounded(w)).toEqual(['attention.wav'])

  // The same wait announced by the other events is one sound, however it comes.
  w.runs.length = 0
  await $.classic.PermissionRequest({ tool_name: 'AskUserQuestion', tool_input: {} })
  await $.classic.Notification({ message: 'Claude needs your attention', notification_type: 'permission_prompt' })
  await clock.advance(10)
  expect(sounded(w)).toEqual([])

  // Later, another kind of notification is not a wait on you; a permission one is.
  await clock.advance(4000)
  await $.classic.Notification({ message: 'Waiting for your input', notification_type: 'idle_prompt' })
  await clock.advance(10)
  expect(sounded(w)).toEqual([])
  await $.classic.Notification({ message: 'Claude needs your permission', notification_type: 'permission_prompt' })
  await clock.advance(10)
  expect(sounded(w)).toEqual(['attention.wav'])
})

test('With always notify on, a question is announced in words, not as a permission', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'alwaysNotify', value: 'on' })
  await pane.unmount()
  await $.classic.PermissionRequest({ tool_name: 'AskUserQuestion', tool_input: {} })
  await clock.advance(10)
  expect(said(w)).toEqual(['Claude needs your answer to a question.'])
})

test('Picking Qwen3-TTS installs it and fetches the voice before switching to it', async ($, on) => {
  const w = world(on, { speaking: false }, { mac: true })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'settings' })
  await clock.advance(10)
  await ui.unmount()
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  await pane.select({ key: 'speaking', value: 'qwen:Aiden' })
  await clock.advance(10)
  const install = w.runs.findIndex(argv => argv.includes('sync') && argv.includes('qwen'))
  const fetch = w.runs.findIndex(argv => argv.includes('fetch-voice'))
  const saved = w.runs.findIndex(argv => argv.includes('config') && argv.includes('tts_engine') && argv.includes('qwen'))
  expect(install).toBeGreaterThan(-1)
  expect(fetch).toBeGreaterThan(install)
  expect(saved).toBeGreaterThan(fetch)
  await pane.unmount()
})

test('Qwen3-TTS is offered only where it can run, and says why not', async ($, on) => {
  world(on, { speaking: false }, { mac: false })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'settings' })
  await clock.advance(10)
  await ui.unmount()
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  expect(await pane.find({ type: 'Text', text: /Speaking/ })).toBeDefined()
  const options = (await pane.find({ type: 'Select', key: 'speaking' }))?.props.options as { value: string }[]
  expect(options.some(o => o.value.startsWith('kokoro:'))).toBe(true)
  expect(options.some(o => o.value.startsWith('qwen:'))).toBe(false)
  expect(await pane.find({ type: 'Text', text: /it needs a newer card/ })).toBeDefined()
  await pane.unmount()
})

test('On a Mac the lighter engine is a choice of its own, with its own install', async ($, on) => {
  const w = world(on, { speaking: false }, { mac: true })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'settings' })
  await clock.advance(10)
  await ui.unmount()
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  const options = (await pane.find({ type: 'Select', key: 'speaking' }))?.props.options as { value: string }[]
  expect(options.some(o => o.value === 'qwen:Aiden')).toBe(true)
  expect(options.some(o => o.value === 'qwen_light:Aiden')).toBe(true)
  await pane.select({ key: 'speaking', value: 'qwen_light:Aiden' })
  await clock.advance(10)
  const install = w.runs.findIndex(argv => argv.includes('sync') && argv.includes('qwen-light'))
  const fetch = w.runs.findIndex(argv => argv.includes('fetch-voice') && argv.includes('light'))
  const saved = w.runs.findIndex(argv => argv.includes('config') && argv.includes('tts_engine') && argv.includes('qwen_light'))
  expect(install).toBeGreaterThan(-1)
  expect(fetch).toBeGreaterThan(install)
  expect(saved).toBeGreaterThan(fetch)
  expect(w.runs.some(argv => argv.includes('sync') && argv.includes('qwen'))).toBe(false)
  await pane.unmount()
})

test('With an NVIDIA card there is one Qwen3-TTS engine to choose', async ($, on) => {
  world(on, { speaking: false }, { mac: false, nvidia: true })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'settings' })
  await clock.advance(10)
  await ui.unmount()
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  const options = (await pane.find({ type: 'Select', key: 'speaking' }))?.props.options as { value: string }[]
  expect(options.some(o => o.value === 'qwen:Aiden')).toBe(true)
  expect(options.some(o => o.value.startsWith('qwen_light:'))).toBe(false)
  await pane.unmount()
})

test('With settings open, a reading that ends still clears from the footer', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading | null } = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const bar = await $.ui.mount({ ...BAND, surface: 'desktop' })
  w.box.text = 'Read @notes.md'
  await bar.press({ key: 'read' })
  await clock.advance(400)
  expect(await bar.find({ type: 'Button', text: /Pause/ })).toBeDefined()
  await bar.press({ key: 'settings' })
  await clock.advance(10)
  state.speaking = false
  state.reading = null
  await clock.advance(700)
  expect(await bar.find({ type: 'Button', text: /Pause/ })).toBeUndefined()
  expect(await bar.find({ type: 'Button', text: /Read aloud/ })).toBeDefined()
  await bar.unmount()
})

test('A highlight left in another chat is not read here; this chat\u2019s reply is', async ($, on) => {
  const state: { speaking: boolean; reading?: Reading } = { speaking: false }
  const w = world(on, state, { mac: true })
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const speaks = () => w.fetches.filter(f => f.url.endsWith('/speak')).length
  const reads = () => w.fetches.filter(f => f.url.endsWith('/read')).length

  // The app hands over a selection made in the chat you came from.
  w.selection.text = 'Words someone highlighted in another chat, earlier today.'
  let before = [speaks(), reads()]
  await ui.press({ key: 'read' })
  expect([speaks(), reads()]).toEqual([before[0]! + 1, before[1]])
  expect(w.fetches.filter(f => f.url.endsWith('/speak')).pop()?.body).toContain('Hello **there**, friend.')
  expect(w.writes.filter(x => x.path.endsWith('last-read.json')).pop()?.content).toContain('"source":"last reply"')
  await ui.press({ key: 'stop' })
  await clock.advance(400)

  // The same for a highlight the Mac sees in another chat's view.
  w.selection.text = ''
  w.mac.highlighted = 'Another chat still shows this highlighted passage on its page.'
  before = [speaks(), reads()]
  await ui.press({ key: 'read' })
  expect([speaks(), reads()]).toEqual([before[0]! + 1, before[1]])
  await ui.press({ key: 'stop' })
  await ui.unmount()

  // On the terminal there is no other chat: a selection in a file you opened is read.
  const term = await $.ui.mount({ ...BAND, surface: 'terminal' })
  w.mac.highlighted = ''
  w.selection.text = 'A paragraph from a file opened in the terminal.'
  await term.press({ key: 'read' })
  expect(readBody(w)).toEqual({ text: 'A paragraph from a file opened in the terminal.', title: 'selection' })
  await term.unmount()
})

test('A scroll that cannot move the settings pane draws nothing, so an open dropdown keeps its place', async ($, on) => {
  world(on, { speaking: false })
  let moves = 0
  on('ui.scroll', () => (moves++, {})) // beneath the plugin: the engine moving the window and drawing
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const bar = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await bar.press({ key: 'settings' })
  await clock.advance(10)
  const pane = await $.ui.mount({ ...SETTINGS, surface: 'desktop' })
  const wheel = (offset: number, contentRows: number) => $.ui.scroll({
    component: 'Pane', requestId: 'claudevoice-settings', offset, by: 1, bodyRows: 30, contentRows, origin: { kind: 'person' },
  })
  // The settings fit: a wheel over an open dropdown moves nothing and is let go.
  expect((await wheel(0, 20)).deny).toBeDefined()
  expect(moves).toBe(0)
  // A pane taller than its window still scrolls, once per row; the same row again is let go.
  expect((await wheel(3, 60)).deny).toBeUndefined()
  expect(moves).toBe(1)
  expect((await wheel(3, 60)).deny).toBeDefined()
  expect(moves).toBe(1)
  expect(await pane.find({ type: 'Select', key: 'speaking' })).toBeDefined()
  await pane.unmount()
  await bar.unmount()
})

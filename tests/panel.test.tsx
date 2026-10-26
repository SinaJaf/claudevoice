import { expect, mock, test } from 'claude-code/testing'

const BAND = {
  plugin: 'claudevoice',
  component: 'SessionMode',
  requestId: 'session-mode',
  props: { modes: ['Auto'] },
} as const

type Fetch = { url: string; body: string }

/** The world beneath the mod: a running Kokoro server, a session with one reply. */
function world(on: Parameters<Parameters<typeof test>[1]>[1], state: { speaking: boolean }) {
  const fetches: Fetch[] = []
  const runs: string[][] = []
  const submitted: string[] = []
  const written: string[] = []
  const writes: { path: string; content: string }[] = []
  const opened: string[] = []
  on('command.register', () => ({ value: { command: 'voice-panel' } }))
  on('ui.open', (_, e) => {
    opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.messages', () => ({
    value: [
      { role: 'user', text: 'say hi', toolUses: [] },
      { role: 'assistant', text: 'Hello **there**, friend.', toolUses: [] },
    ],
  }))
  on('http.fetch', (_, e) => {
    fetches.push({ url: e.url, body: e.init?.body ?? '' })
    if (e.url.endsWith('/speak')) state.speaking = true
    if (e.url.endsWith('/stop')) state.speaking = false
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ok: true, speaking: state.speaking }) } }
  })
  const results = new Map<string, string>()
  on('process.run', (_, e) => {
    runs.push([...e.argv])
    // A detached listener answers later, through its --result file.
    const at = e.argv.indexOf('--result')
    if (at >= 0) results.set(e.argv[at + 1] ?? '', JSON.stringify({ text: 'what time is it', seconds: 2.1 }))
    const isListen = e.argv.includes('claudevoice.listen') && !e.argv.includes('--warm')
    const isConfigRead = e.argv.includes('config') && e.argv.length === 4
    const stdout = isListen
      ? JSON.stringify({ text: 'what time is it', seconds: 2.1 })
      : isConfigRead
        ? JSON.stringify({ voice: 'af_heart', tts_engine: 'kokoro', stt_engine: 'local', whisper_model: 'mlx-community/whisper-large-v3-turbo', speed: 1.05, tts_voice: 'nova' })
        : JSON.stringify({ ok: true })
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.close', () => ({ value: undefined }))
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
  on('turn.start', () => ({}))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('ui.toast', () => ({ value: undefined }))
  // The machine beneath: a venv already set up, a home folder.
  on('fs.exists', (_, e) => ({ value: e.path.endsWith('/.venv/bin/python') || results.has(e.path) }))
  on('fs.read', (_, e) => ({ value: results.get(e.path) ?? '' }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/tester' : undefined }))
  on('fs.write', (_, e) => {
    written.push(e.path)
    writes.push({ path: e.path, content: e.text })
    return { value: undefined }
  })
  return { fetches, runs, submitted, written, writes, opened, box }
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
    expect((await ui.find({ type: 'Text', text: /speaking/ }))?.text).toContain('speaking')
    await ui.press({ key: 'stop' })
    expect(w.fetches.some(f => f.url.endsWith('/stop'))).toBe(true)
    await clock.advance(400)
    expect(await ui.find({ type: 'Text', text: /speaking/ })).toBeUndefined()
    await ui.unmount()
    w.fetches.length = 0
  }
})

test('A call: you talk with the mic, Done sends the prompt box, the answer is spoken', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /Auto/ })).toBeDefined()

  await ui.press({ key: 'call' })
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('press Done')
  // No speech-to-text of our own: nothing records the microphone.
  expect(w.runs.some(argv => argv.includes('claudevoice.listen'))).toBe(false)
  // The controls stand in place for the whole call.
  for (const key of ['done', 'stop', 'settings', 'end']) expect(await ui.find({ key })).toBeDefined()

  // Greeting ends: your turn.
  state.speaking = false
  await clock.advance(400)
  expect(await ui.find({ type: 'Text', text: /your turn/ })).toBeDefined()

  // Done with an empty box sends nothing.
  await ui.press({ key: 'done' })
  expect(w.submitted).toEqual([])

  // Claude's dictation filled the box; Done sends it and clears the box.
  w.box.text = 'what time is it'
  await ui.press({ key: 'done' })
  expect(w.submitted).toEqual(['what time is it'])
  expect(w.box.text).toBe('')

  // Claude answers: the reply is spoken.
  w.fetches.length = 0
  await $.turn.complete({ answer: 'It is about noon.', reason: 'answer', durationMs: 50, isAborted: false, turnId: 't1' })
  await clock.advance(10)
  expect(w.fetches.find(f => f.url.endsWith('/speak'))?.body).toContain('It is about noon.')

  // Hang up: the voice stops and the footer offers Call again.
  await ui.press({ key: 'end' })
  expect(w.fetches.some(f => f.url.endsWith('/stop'))).toBe(true)
  expect(await ui.find({ type: 'Button', text: /Call/ })).toBeDefined()
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

test('Stop cuts the voice and hands you the turn', async ($, on) => {
  const state = { speaking: false }
  const w = world(on, state)
  const clock = mock.clock(on)
  mock.store(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'call' })
  expect(await ui.find({ type: 'Text', text: /speaking/ })).toBeDefined()
  await ui.press({ key: 'stop' })
  expect(w.fetches.some(f => f.url.endsWith('/stop'))).toBe(true)
  await clock.advance(400)
  expect(await ui.find({ type: 'Text', text: /your turn/ })).toBeDefined()
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

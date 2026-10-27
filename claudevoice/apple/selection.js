// What Read aloud needs to know on macOS, in one call: the clipboard (its text, and its
// change count, which moves on every copy, the same text copied again included), and,
// with "app", the text highlighted in the Claude desktop app, its Files pane included,
// and the file open in that pane.
//
// The app tells plugins only about a highlight in the chat. macOS Accessibility, which
// the Claude app already holds (this helper runs as its child), reads the highlight in
// every part of the page. Electron builds that view of its pages only when asked
// (AXManualAccessibility), which takes a moment the first time.
//
// The open file is the Files pane's selected tab (its name), with the path parts its header
// shows above the page, so a file in a folder can be found.
//
// Run: osascript -l JavaScript selection.js [app]. Prints JSON:
// {"changeCount": 12, "clipboard": "...", "selection": "...", "areas": 2, "lengths": [0, 41],
//  "openFile": ["docs", "guide.md"], "trusted": true}
// (trusted: the Claude app is allowed Accessibility, so its highlights and tabs can be read)

ObjC.import('AppKit')
ObjC.bindFunction('AXIsProcessTrusted', ['bool', []])
ObjC.bindFunction('AXUIElementCreateApplication', ['id', ['int']])
ObjC.bindFunction('AXUIElementCopyAttributeValue', ['int', ['id', 'id', 'id*']])
ObjC.bindFunction('AXUIElementSetAttributeValue', ['int', ['id', 'id', 'id']])
ObjC.bindFunction('AXUIElementCopyParameterizedAttributeValue', ['int', ['id', 'id', 'id', 'id*']])

const CLAUDE = 'com.anthropic.claudefordesktop'
const MAX_VISITED = 20000

function attr(el, name) {
  const out = Ref()
  return $.AXUIElementCopyAttributeValue(el, $(name), out) === 0 ? out[0] : null
}

const text = v => {
  const u = v ? ObjC.unwrap(v) : ''
  return typeof u === 'string' ? u.trim() : ''
}

/** The app's web page areas (where highlights are), its selected tabs' names, and the
 *  header buttons naming each part of a file's path, in the order they are drawn. */
function survey(app) {
  const found = { areas: [], tabs: [], headers: [] }
  let visited = 0
  const walk = (el, depth) => {
    if (++visited > MAX_VISITED || depth > 80) return
    const role = ObjC.unwrap(attr(el, 'AXRole'))
    if (role === 'AXWebArea') found.areas.push(el)
    if (role === 'AXRadioButton' && text(attr(el, 'AXRoleDescription')) === 'tab' && String(ObjC.unwrap(attr(el, 'AXValue'))) === '1') {
      found.tabs.push(text(attr(el, 'AXTitle')))
    }
    const kids = attr(el, 'AXChildren')
    const parts = []
    for (let i = 0; kids && i < kids.count; i++) {
      const kid = kids.objectAtIndex(i)
      if (ObjC.unwrap(attr(kid, 'AXRole')) === 'AXPopUpButton') parts.push(text(attr(kid, 'AXTitle')))
    }
    if (parts.length > 0) found.headers.push(parts.filter(p => p !== ''))
    for (let i = 0; kids && i < kids.count; i++) walk(kids.objectAtIndex(i), depth + 1)
  }
  const windows = attr(app, 'AXWindows')
  for (let i = 0; windows && i < windows.count; i++) walk(windows.objectAtIndex(i), 0)
  return found
}

/** The open file's path parts: the header row that ends in the selected tab's name, else the name. */
function openFile(found) {
  for (const tab of found.tabs) {
    if (tab === '' || tab.includes('\n')) continue
    const header = found.headers.find(parts => parts[parts.length - 1] === tab)
    return header ?? [tab]
  }
  return []
}

function highlighted(area) {
  const range = attr(area, 'AXSelectedTextMarkerRange')
  if (!range) return ''
  const text = Ref()
  if ($.AXUIElementCopyParameterizedAttributeValue(area, $('AXStringForTextMarkerRange'), range, text) !== 0) return ''
  return ObjC.unwrap(text[0]) || ''
}

function run(argv) {
  const board = $.NSPasteboard.generalPasteboard
  const result = {
    changeCount: board.changeCount,
    clipboard: ObjC.unwrap(board.stringForType($.NSPasteboardTypeString)) || '',
    selection: '',
    areas: 0,
    lengths: [],
    openFile: [],
    trusted: true,
  }
  if (argv[0] !== 'app') return JSON.stringify(result)
  result.trusted = $.AXIsProcessTrusted()
  if (!result.trusted) return JSON.stringify(result)

  const apps = $.NSWorkspace.sharedWorkspace.runningApplications
  let pid = -1
  for (let i = 0; i < apps.count; i++) {
    const a = apps.objectAtIndex(i)
    if (ObjC.unwrap(a.bundleIdentifier) === CLAUDE) pid = a.processIdentifier
  }
  if (pid < 0) return JSON.stringify(result)
  const app = $.AXUIElementCreateApplication(pid)
  $.AXUIElementSetAttributeValue(app, $('AXManualAccessibility'), $.NSNumber.numberWithBool(true))
  let found = survey(app)
  for (let tries = 0; found.areas.length === 0 && tries < 6; tries++) {
    delay(0.3) // the first time, the page's accessibility view is still being built
    found = survey(app)
  }
  result.areas = found.areas.length
  result.openFile = openFile(found)
  for (const area of found.areas) {
    const text = highlighted(area)
    result.lengths.push(text.length)
    if (text.trim().length > result.selection.trim().length) result.selection = text
  }
  return JSON.stringify(result)
}

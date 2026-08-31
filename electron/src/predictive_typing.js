const { BrowserWindow, screen, ipcMain } = require('electron');
const { execFile, spawn } = require('child_process');
const http = require('http');
const path = require('path');

// Universal: active in every app except this short blocklist. Tab is only ever
// intercepted globally while a suggestion pill is actively showing (see
// tab_tap.swift's `suggestionActive` flag) -- it behaves completely normally
// everywhere else. But terminals and code editors give Tab its own essential
// meaning (shell completion, indentation), and natural-language suggestions
// from the small local model aren't useful there anyway -- if a suggestion
// happened to be showing at the wrong moment, Tab would silently do the wrong
// thing (accept a word instead of indenting/completing). Excluded rather than
// risk that. Bundle identifiers, not display names.
const BLOCKED_BUNDLE_IDS = [
  'com.apple.Terminal',
  'com.googlecode.iterm2',
  'dev.warp.Warp-Stable',
  'co.zeit.hyper',
  'com.github.wez.wezterm',
  'com.apple.dt.Xcode',
  'com.microsoft.VSCode',
  'com.microsoft.VSCodeInsiders',
  'com.sublimetext.4',
  'com.sublimetext.3',
  'com.jetbrains.intellij',
  'com.jetbrains.pycharm',
  'com.jetbrains.WebStorm',
  'com.todesktop.230313mzl4w4u92' // Cursor
];

function getFrontmostBundleId(callback) {
  execFile('osascript', ['-e', 'tell application "System Events" to get bundle identifier of first application process whose frontmost is true'], (err, stdout) => {
    if (err) { callback(null); return; }
    callback(stdout.trim());
  });
}

const AX_HELPER_PATH = path.join(__dirname, '..', 'AXHelper.app', 'Contents', 'MacOS', 'ax_helper');
const TAB_TAP_PATH = path.join(__dirname, '..', 'TabTap.app', 'Contents', 'MacOS', 'tab_tap');
const POLL_INTERVAL_MS = 500;

let ghostWindow = null;
let currentSuggestionWords = [];
let lastTextBeforeCursor = '';
let lastCaretX = 100;
let lastCaretY = 100;
let lastCaretBundleId = null; // which app lastCaretX/Y actually belongs to
let lastShownSuggestion = '';
let pollTimer = null;
let enabled = true;
let tabTapProcess = null;
let pendingRequest = false;
let acceptingInProgress = false;

function readFocusedContext(callback) {
  execFile(AX_HELPER_PATH, ['read'], { timeout: 2000 }, (err, stdout) => {
    if (err) { callback(null); return; }
    try {
      const parsed = JSON.parse(stdout.trim());
      if (parsed.error) { callback(null); return; }
      callback(parsed);
    } catch (e) {
      callback(null);
    }
  });
}

function insertTextViaAX(text, callback) {
  execFile(AX_HELPER_PATH, ['insert', text], { timeout: 2000 }, (err, stdout) => {
    if (err) { callback(false); return; }
    try {
      const parsed = JSON.parse(stdout.trim());
      callback(!!parsed.success);
    } catch (e) {
      callback(false);
    }
  });
}

let activeRequest = null;

function callDaemonForCompletion(textBeforeCursor, callback) {
  // abort any in-flight request before starting a new one — never let requests stack
  if (activeRequest) {
    activeRequest.destroy();
    activeRequest = null;
  }

  const contextTail = textBeforeCursor.slice(-120);
  // kept intentionally short -- this whole prompt gets re-processed by the model
  // on every single request while typing, so its length is a direct, constant
  // latency tax. Trimming it from the original longer instruction block measurably
  // cut request time (~700ms -> ~570ms). The "no filler words" line earns its
  // keep specifically: without it the model has a strong tic of tacking on
  // "now"/"today" onto otherwise-good completions ("the document now").
  const prompt = `Continue this sentence with 2-4 plain words, no punctuation. Do not add filler words like now/today/please unless truly needed:
"${contextTail}"`;

  const url = `http://localhost:11200/complete?prompt=${encodeURIComponent(prompt)}&context=${encodeURIComponent(contextTail)}`;
  const req = http.get(url, (res) => {
    let data = '';
    res.on('data', (chunk) => { data += chunk; });
    res.on('end', () => {
      activeRequest = null;
      try {
        const parsed = JSON.parse(data);
        callback(parsed.response.trim());
      } catch (e) {
        callback(null);
      }
    });
  });

  req.setTimeout(4000, () => {
    req.destroy();
    activeRequest = null;
    callback(null);
  });

  req.on('error', () => {
    activeRequest = null;
    callback(null);
  });

  activeRequest = req;
}

function createGhostWindow() {
  ghostWindow = new BrowserWindow({
    width: 300,
    height: 44,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: false,
    skipTaskbar: true,
    focusable: false,
    show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  ghostWindow.setIgnoreMouseEvents(true);
  ghostWindow.loadFile(path.join(__dirname, 'ghost.html'));
}

function setTabTapActive(active) {
  if (tabTapProcess && !tabTapProcess.killed) {
    tabTapProcess.stdin.write((active ? 'ACTIVE' : 'INACTIVE') + '\n');
  }
}

function showGhostText(words, x, y) {
  if (!ghostWindow || ghostWindow.isDestroyed()) createGhostWindow();

  currentSuggestionWords = words;

  // pill sits clearly BELOW the current line — generous offset so it never
  // overlaps the text being typed, forgiving of imprecise caret coordinates
  ghostWindow.setPosition(Math.round(x), Math.round(y) + 24);
  ghostWindow.webContents.executeJavaScript(
    `window.renderGhost && window.renderGhost(${JSON.stringify(words)})`
  );
  ghostWindow.showInactive();
  setTabTapActive(true);
}

function hideGhostText() {
  currentSuggestionWords = [];
  setTabTapActive(false);
  if (ghostWindow && !ghostWindow.isDestroyed()) {
    ghostWindow.hide();
  }
}

function reportAcceptedWord(context, word) {
  // fire-and-forget: reinforces the personalization model, never blocks typing
  const body = JSON.stringify({ context, word });
  const req = http.request(
    { hostname: 'localhost', port: 11200, path: '/complete/feedback', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } },
    (res) => { res.on('data', () => {}); }
  );
  req.on('error', () => {}); // personalization is a nice-to-have, never surface this failing
  req.write(body);
  req.end();
}

function acceptNextWord() {
  if (currentSuggestionWords.length === 0) return;

  acceptingInProgress = true;
  const word = currentSuggestionWords.shift();
  reportAcceptedWord(lastTextBeforeCursor, word);
  // ensure a space exists before the inserted word if the text doesn't already end with whitespace
  const needsLeadingSpace = lastTextBeforeCursor.length > 0 && !/\s$/.test(lastTextBeforeCursor);
  const textToInsert = (needsLeadingSpace ? ' ' : '') + word + ' ';

  insertTextViaAX(textToInsert, (success) => {
    acceptingInProgress = false;
    if (!success) {
      hideGhostText();
      return;
    }
    // keep lastTextBeforeCursor in sync with what we just typed via AX insert —
    // otherwise the next poll sees "new" text it didn't expect and wipes the
    // remaining suggestion words we're about to show, then re-requests fresh
    // ones that often look like the same suggestion coming back
    lastTextBeforeCursor = lastTextBeforeCursor + textToInsert;

    if (currentSuggestionWords.length === 0) {
      hideGhostText();
      lastTextBeforeCursor = ''; // force fresh read on next poll
      lastShownSuggestion = '';
    } else {
      showGhostText(currentSuggestionWords, lastCaretX, lastCaretY);
    }
  });
}

function pollAndSuggest() {
  if (!enabled || pendingRequest || acceptingInProgress) return;

  getFrontmostBundleId((bundleId) => {
    console.log('[poll] frontmost bundleId:', bundleId);
    if (!bundleId || BLOCKED_BUNDLE_IDS.includes(bundleId)) {
      hideGhostText();
      return;
    }

    readFocusedContext((context) => {
      console.log('[poll] context:', context ? JSON.stringify(context).slice(0, 200) : null);
      if (!context || !context.textBeforeCursor) {
        hideGhostText();
        return;
      }

      // don't re-request if nothing changed since last poll
      if (context.textBeforeCursor === lastTextBeforeCursor) {
        console.log('[poll] no change, skipping');
        return;
      }

      // text changed underneath an old suggestion (user typed past it without Tab) — clear stale pill immediately
      if (currentSuggestionWords.length > 0) {
        hideGhostText();
      }

      lastTextBeforeCursor = context.textBeforeCursor;

      // don't suggest on effectively empty context
      if (context.textBeforeCursor.trim().length < 3) {
        hideGhostText();
        return;
      }

      // Three-layer validation for the caret position, strongest check first:
      //
      // 1. Cross-check against the frontmost window's own bounds (most reliable
      //    when available) -- some apps report a "successful" bounds lookup with
      //    a placeholder rect sitting exactly on the window's edge/corner instead
      //    of a real error, which a bare "caretX >= 0" check can't catch.
      // 2. If window bounds aren't available, at least check the coordinate falls
      //    on SOME actual connected display -- always possible via Electron's
      //    screen module, regardless of the target app's AX quality.
      // 3. If neither check can be trusted, DON'T keep a stale position from a
      //    different app/context (that's exactly what "floating in the air"
      //    looks like) -- snap to the current mouse position instead, which is
      //    always real, current, and usually close to where the user is
      //    actually looking/typing.
      let positionTrusted = false;

      if (typeof context.caretX === 'number' && context.window) {
        const w = context.window;
        const margin = 2; // reject values sitting exactly on an edge (placeholder rects)
        const withinWindow =
          context.caretX > w.x + margin && context.caretX < w.x + w.width - margin &&
          context.caretY > w.y + margin && context.caretY < w.y + w.height - margin;
        if (withinWindow) {
          lastCaretX = context.caretX;
          lastCaretY = context.caretY;
          lastCaretBundleId = bundleId;
          positionTrusted = true;
        }
      } else if (typeof context.caretX === 'number' && context.caretX >= 0) {
        const point = { x: context.caretX, y: context.caretY };
        const onSomeDisplay = screen.getAllDisplays().some(d =>
          point.x >= d.bounds.x && point.x <= d.bounds.x + d.bounds.width &&
          point.y >= d.bounds.y && point.y <= d.bounds.y + d.bounds.height
        );
        if (onSomeDisplay) {
          lastCaretX = context.caretX;
          lastCaretY = context.caretY;
          lastCaretBundleId = bundleId;
          positionTrusted = true;
        }
      }

      // no trustworthy position for THIS app -- a stale position from a
      // different app is worse than useless, fall back to the mouse cursor
      if (!positionTrusted && lastCaretBundleId !== bundleId) {
        const cursor = screen.getCursorScreenPoint();
        lastCaretX = cursor.x;
        lastCaretY = cursor.y;
      }

      pendingRequest = true;
      console.log('[poll] calling daemon...');
      callDaemonForCompletion(context.textBeforeCursor, (completion) => {
        pendingRequest = false;
        console.log('[poll] daemon response:', completion);
        if (!completion) { hideGhostText(); return; }

        // clean up: take only the first line/fragment, strip leading ellipsis/punctuation
        let cleaned = completion.split('\n')[0].trim();
        cleaned = cleaned.replace(/^[.\u2026\s]+/, ''); // strip leading … or .

        // safety net: if model echoed part of the input back, strip it out
        const tailWords = context.textBeforeCursor.trim().split(/\s+/).slice(-6).join(' ');
        if (tailWords && cleaned.toLowerCase().startsWith(tailWords.toLowerCase())) {
          cleaned = cleaned.slice(tailWords.length).trim();
        }

        const words = cleaned
          .replace(/[,]/g, '')
          .split(/\s+/)
          .filter(w => w.length > 0)
          .slice(0, 4);

        if (words.length === 0) { hideGhostText(); return; }

        // reject if this exact phrase already appears anywhere in the typed text —
        // catches the model re-suggesting something the user already wrote,
        // which otherwise compounds into a repeat loop as words get Tab-accepted
        const suggestionPhrase = words.join(' ').toLowerCase();
        const typedSoFar = context.textBeforeCursor.toLowerCase();
        if (typedSoFar.includes(suggestionPhrase)) {
          console.log('[poll] suggestion duplicates already-typed text, discarding:', suggestionPhrase);
          hideGhostText();
          return;
        }

        const suggestionKey = suggestionPhrase;
        if (suggestionKey === lastShownSuggestion) {
          console.log('[poll] suppressing repeat suggestion');
          hideGhostText();
          return;
        }
        lastShownSuggestion = suggestionKey;

        console.log('[poll] showing ghost at', lastCaretX, lastCaretY, 'words:', words);
        showGhostText(words, lastCaretX, lastCaretY);
      });
    });
  });
}

function startPredictiveTyping() {
  pollTimer = setInterval(pollAndSuggest, POLL_INTERVAL_MS);

  tabTapProcess = spawn(TAB_TAP_PATH, [], { stdio: ['pipe', 'pipe', 'pipe'] });

  tabTapProcess.stdout.on('data', (data) => {
    const lines = data.toString().split('\n').filter(l => l.trim().length > 0);
    for (const line of lines) {
      if (line.trim() === 'TAB_PRESSED') {
        acceptNextWord();
      }
    }
  });

  tabTapProcess.stderr.on('data', (data) => {
    console.error('tab_tap error:', data.toString());
  });

  tabTapProcess.on('exit', (code) => {
    console.log('tab_tap exited with code', code);
    tabTapProcess = null;
  });
}

function stopPredictiveTyping() {
  if (pollTimer) clearInterval(pollTimer);
  if (activeRequest) { activeRequest.destroy(); activeRequest = null; }
  if (tabTapProcess && !tabTapProcess.killed) tabTapProcess.kill('SIGKILL');
  hideGhostText();
}

module.exports = { startPredictiveTyping, stopPredictiveTyping };

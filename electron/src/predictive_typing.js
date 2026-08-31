const { BrowserWindow, screen, ipcMain } = require('electron');
const { execFile, spawn } = require('child_process');
const http = require('http');
const path = require('path');

// Temporary allowlist while we stabilize the core mechanism — expand later.
// Bundle identifiers, not display names. TextEdit = com.apple.TextEdit
const ALLOWED_BUNDLE_IDS = ['com.apple.TextEdit'];

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
  const prompt = `Complete this sentence naturally with 2-4 plain, literal words. No creative writing, no poetic language, no emojis, no greetings, no unrelated words, no punctuation. Never repeat any word or phrase that already appears in the text below — only generate genuinely NEW words that haven't been typed yet. Just the most likely next words a person would type to continue their OWN sentence.

"${contextTail}"
Next:`;

  const url = `http://localhost:11200/complete?prompt=${encodeURIComponent(prompt)}`;
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

function acceptNextWord() {
  if (currentSuggestionWords.length === 0) return;

  acceptingInProgress = true;
  const word = currentSuggestionWords.shift();
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
    if (!bundleId || !ALLOWED_BUNDLE_IDS.includes(bundleId)) {
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

      // use real caret coords if AX provided them, else keep last known position
      if (typeof context.caretX === 'number' && context.caretX >= 0) {
        lastCaretX = context.caretX;
        lastCaretY = context.caretY;
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

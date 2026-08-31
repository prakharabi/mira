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

const AX_HELPER_PATH = path.join(__dirname, '..', 'AXHelper.app', 'Contents', 'MacOS', 'ax_helper');
const TAB_TAP_PATH = path.join(__dirname, '..', 'TabTap.app', 'Contents', 'MacOS', 'tab_tap');

// Adaptive polling instead of a fixed interval: this used to poll every 500ms
// FOREVER, even while completely idle, and each tick spawned a separate
// `osascript` subprocess just to check the frontmost app (now folded into
// ax_helper's own read below -- see frontmostBundleIdJSON there). Spawning a
// process twice a second all day is real, avoidable CPU/battery cost. Two
// separate backoff tiers, because "nothing to do here" and "paused mid-
// sentence" deserve very different treatment:
//  - NO_CONTEXT: not in an editable field at all (blocked app, nothing
//    focused) -- there's nothing to lose by backing off fast here, and this
//    covers most of a typical day, so it's where the real savings come from.
//  - PAUSED: genuinely focused in a text field but not currently typing --
//    back off only mildly, and only after a much longer pause, since a
//    laggy first suggestion when the user resumes typing is exactly the
//    "not matching my typing speed" complaint this whole feature exists to
//    avoid. Any real text change resets straight back to fast, either way.
const POLL_FAST_MS = 500;
const POLL_PAUSED_MS = 900;
const POLL_NO_CONTEXT_MS = 3000;
const PAUSED_TICKS_BEFORE_BACKOFF = 10; // ~5s of no change while still focused in a field
const NO_CONTEXT_TICKS_BEFORE_BACKOFF = 2; // ~1s -- nothing to lose by backing off fast
let idleTickCount = 0;

let ghostWindow = null;
let currentSuggestionWords = [];
let lastTextBeforeCursor = '';
let lastCaretX = 100;
let lastCaretY = 100;
let lastCaretBundleId = null; // which app lastCaretX/Y actually belongs to
let lastShownSuggestion = '';
let pollTimer = null;
let enabled = true;
let stopped = true; // starts true; startPredictiveTyping() flips it before scheduling
let tabTapProcess = null;
let pendingRequest = false;
let acceptingInProgress = false;

function readFocusedContext(callback) {
  execFile(AX_HELPER_PATH, ['read'], { timeout: 2000 }, (err, stdout) => {
    if (err) { callback(null); return; }
    try {
      // parsed.bundleId is preserved even on parsed.error (e.g. "no focused
      // element" -- browsing without a text field focused) so the blocklist
      // check can still run without a second lookup
      callback(JSON.parse(stdout.trim()));
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
  // setTimeout-based scheduling is self-chaining (see scheduleNextPoll) --
  // every exit path, including this one, MUST schedule the next poll itself
  // or the whole loop silently dies. (setInterval didn't have this hazard,
  // since missing one tick was harmless -- the timer kept firing regardless.)
  if (!enabled || pendingRequest || acceptingInProgress) {
    // transient busy state, not an idle signal -- retry soon without touching
    // the backoff counters at all
    scheduleNextPoll('retry');
    return;
  }

  // Single subprocess call now covers both the frontmost-app check AND the text
  // context read (ax_helper reports bundleId via a cheap in-process NSWorkspace
  // lookup) -- this used to be two separate subprocess spawns every tick,
  // including a full `osascript`/AppleScript round-trip just for the app check.
  readFocusedContext((context) => {
    console.log('[poll] context:', context ? JSON.stringify(context).slice(0, 200) : null);

    const bundleId = context && context.bundleId;
    if (!bundleId || BLOCKED_BUNDLE_IDS.includes(bundleId)) {
      hideGhostText();
      scheduleNextPoll('noContext');
      return;
    }

    if (!context || !context.textBeforeCursor) {
      hideGhostText();
      scheduleNextPoll('noContext');
      return;
    }

    // don't re-request if nothing changed since last poll
    if (context.textBeforeCursor === lastTextBeforeCursor) {
      console.log('[poll] no change, skipping');
      scheduleNextPoll('paused');
      return;
    }

      // text changed underneath an old suggestion (user typed past it without Tab) — clear stale pill immediately
      if (currentSuggestionWords.length > 0) {
        hideGhostText();
      }

      lastTextBeforeCursor = context.textBeforeCursor;
      // a real text change is "activity" -- stay on fast polling regardless of
      // what happens below (too-short context, discarded suggestion, etc.)
      scheduleNextPoll('active');

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
}

// category: 'active' (just saw a real text change), 'paused' (focused in a
// text field, nothing changed), 'noContext' (not in an editable field at
// all), or 'retry' (transient busy state -- doesn't touch backoff at all)
function scheduleNextPoll(category) {
  // guards against an in-flight async callback (AX read / daemon request) still
  // completing and rescheduling itself AFTER stopPredictiveTyping() already ran --
  // clearTimeout only cancels an already-pending timer, not a callback that
  // hasn't fired yet, so without this a stopped loop could silently resurrect
  if (stopped) return;

  let delay;
  if (category === 'retry') {
    delay = POLL_FAST_MS;
  } else if (category === 'active') {
    idleTickCount = 0;
    delay = POLL_FAST_MS;
  } else if (category === 'noContext') {
    idleTickCount++;
    delay = idleTickCount >= NO_CONTEXT_TICKS_BEFORE_BACKOFF ? POLL_NO_CONTEXT_MS : POLL_FAST_MS;
  } else { // 'paused'
    idleTickCount++;
    delay = idleTickCount >= PAUSED_TICKS_BEFORE_BACKOFF ? POLL_PAUSED_MS : POLL_FAST_MS;
  }
  pollTimer = setTimeout(pollAndSuggest, delay);
}

function startPredictiveTyping() {
  stopped = false;
  pollTimer = setTimeout(pollAndSuggest, POLL_FAST_MS);

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
  stopped = true;
  if (pollTimer) clearTimeout(pollTimer);
  if (activeRequest) { activeRequest.destroy(); activeRequest = null; }
  if (tabTapProcess && !tabTapProcess.killed) tabTapProcess.kill('SIGKILL');
  hideGhostText();
}

module.exports = { startPredictiveTyping, stopPredictiveTyping };

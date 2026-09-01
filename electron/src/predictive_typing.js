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

// Next-word prediction (the LLM call) is deliberately decoupled from every
// keystroke now. Instead of firing on every text change, it waits for a pause
// after a completed word before analyzing the accumulated sentence -- this
// both cuts wasted requests (most in-progress typing never needed a request
// at all) and gives the model a fuller, more coherent chunk of context to
// work with (the last several words of an actual finished thought, not a
// half-typed fragment). Any further typing during the wait resets the timer.
const NEXT_WORD_DEBOUNCE_MS = 1200;
const CONTEXT_WORD_COUNT = 8; // "at least 4-8 words" of context for next-word prediction
const MIN_PARTIAL_WORD_LEN = 3; // shorter than this, in-word completion is too noisy to be useful

let ghostWindow = null;
let lastTextBeforeCursor = '';
let lastCaretX = 100;
let lastCaretY = 100;
let lastCaretBundleId = null; // which app lastCaretX/Y actually belongs to
let lastShownSuggestion = '';
let pollTimer = null;
let nextWordDebounceTimer = null;
let enabled = true;
let stopped = true; // starts true; startPredictiveTyping() flips it before scheduling
let tabTapProcess = null;
let pendingRequest = false;
let acceptingInProgress = false;

// ---------- suggestion state: exactly one of these is active at a time ----------
// mode: null | 'next-word' | 'in-word' | 'correction'
let suggestionMode = null;
let currentSuggestionWords = [];      // mode === 'next-word'
let currentInWordSuffix = '';         // mode === 'in-word'
let currentInWordFullWord = '';
let currentCorrection = null;         // mode === 'correction' -- { wrong, correct }

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

function correctWordViaAX(deleteCount, replacement, callback) {
  execFile(AX_HELPER_PATH, ['correct', String(deleteCount), replacement], { timeout: 2000 }, (err, stdout) => {
    if (err) { callback(false); return; }
    try {
      const parsed = JSON.parse(stdout.trim());
      callback(!!parsed.success);
    } catch (e) {
      callback(false);
    }
  });
}

function spellcheckViaDaemon(word, callback) {
  const url = `http://localhost:11200/spellcheck?word=${encodeURIComponent(word)}`;
  http.get(url, { timeout: 2000 }, (res) => {
    let data = '';
    res.on('data', (c) => { data += c; });
    res.on('end', () => {
      try {
        callback(JSON.parse(data));
      } catch (e) {
        callback(null);
      }
    });
  }).on('error', () => callback(null))
    .on('timeout', function () { this.destroy(); callback(null); });
}

function completeWordViaDaemon(partial, callback) {
  const url = `http://localhost:11200/complete/word?partial=${encodeURIComponent(partial)}`;
  http.get(url, { timeout: 2000 }, (res) => {
    let data = '';
    res.on('data', (c) => { data += c; });
    res.on('end', () => {
      try {
        callback(JSON.parse(data));
      } catch (e) {
        callback(null);
      }
    });
  }).on('error', () => callback(null))
    .on('timeout', function () { this.destroy(); callback(null); });
}

let activeRequest = null;

function callDaemonForCompletion(contextTail, callback) {
  // abort any in-flight request before starting a new one — never let requests stack
  if (activeRequest) {
    activeRequest.destroy();
    activeRequest = null;
  }

  // kept intentionally short -- this whole prompt gets re-processed by the model
  // on every request, so its length is a direct latency tax. The "no filler
  // words" line earns its keep specifically: without it the model has a strong
  // tic of tacking on "now"/"today" onto otherwise-good completions.
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

function showGhost(payload, x, y) {
  if (!ghostWindow || ghostWindow.isDestroyed()) createGhostWindow();

  // pill sits clearly BELOW the current line — generous offset so it never
  // overlaps the text being typed, forgiving of imprecise caret coordinates
  ghostWindow.setPosition(Math.round(x), Math.round(y) + 24);
  ghostWindow.webContents.executeJavaScript(
    `window.renderGhost && window.renderGhost(${JSON.stringify(payload)})`
  );
  ghostWindow.showInactive();
  setTabTapActive(true);
}

function hideGhostText() {
  suggestionMode = null;
  currentSuggestionWords = [];
  currentInWordSuffix = '';
  currentInWordFullWord = '';
  currentCorrection = null;
  setTabTapActive(false);
  if (ghostWindow && !ghostWindow.isDestroyed()) {
    ghostWindow.hide();
  }
}

function reportAcceptedWord(context, word) {
  postFireAndForget('/complete/feedback', { context, word });
}

function reportAcceptedCompletion(word) {
  postFireAndForget('/complete/word/feedback', { word });
}

function postFireAndForget(pathName, bodyObj) {
  // fire-and-forget: reinforces the personalization model, never blocks typing
  const body = JSON.stringify(bodyObj);
  const req = http.request(
    { hostname: 'localhost', port: 11200, path: pathName, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } },
    (res) => { res.on('data', () => {}); }
  );
  req.on('error', () => {}); // best-effort only, never surface this failing
  req.write(body);
  req.end();
}

// ---------- accepting the currently-shown suggestion (Tab), dispatched by mode ----------
function acceptCurrentSuggestion() {
  if (suggestionMode === 'correction') {
    acceptCorrection();
  } else if (suggestionMode === 'in-word') {
    acceptInWordCompletion();
  } else if (suggestionMode === 'next-word') {
    acceptNextWord();
  }
}

function acceptCorrection() {
  if (!currentCorrection) return;
  acceptingInProgress = true;
  const { wrong, correct, trailingWhitespace } = currentCorrection;

  // the cursor sits AFTER the trailing whitespace that triggered this boundary
  // check (see analyzeContext) -- e.g. for "...wrold ", the characters
  // immediately before the cursor going backward are: the space, then "d","l",
  // "o","r","w". Deleting only wrong.length chars would eat into the space
  // instead of the word, so the whitespace has to be included in both the
  // delete count and what gets retyped afterward to preserve it exactly
  // (including any double-space-after-period habits).
  const deleteCount = wrong.length + trailingWhitespace.length;
  const replacement = correct + trailingWhitespace;

  correctWordViaAX(deleteCount, replacement, (success) => {
    acceptingInProgress = false;
    if (!success) { hideGhostText(); return; }
    // the word just changed length -- force a fresh AX read on the next poll
    // rather than trying to patch up lastTextBeforeCursor here
    lastTextBeforeCursor = '';
    hideGhostText();
  });
}

function acceptInWordCompletion() {
  if (!currentInWordSuffix) return;
  acceptingInProgress = true;
  const suffix = currentInWordSuffix;
  const fullWord = currentInWordFullWord;

  insertTextViaAX(suffix, (success) => {
    acceptingInProgress = false;
    if (!success) { hideGhostText(); return; }
    reportAcceptedCompletion(fullWord);
    lastTextBeforeCursor = lastTextBeforeCursor + suffix;
    hideGhostText();
  });
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
      showGhost({ mode: 'next-word', words: currentSuggestionWords }, lastCaretX, lastCaretY);
    }
  });
}

// ---------- splitting textBeforeCursor into "mid-word" vs "just finished a word" ----------
// Boundary is whitespace-only (not general punctuation) deliberately: the
// correction-accept path needs to backspace exactly as many characters as it
// typed, and "the trailing whitespace run is currently right before the
// cursor" is a clean, simple invariant to build that on. A word ending in
// punctuation (e.g. "world.") is handled as part of the token itself, and
// only offered for correction if it's alphabetic once that punctuation is
// stripped -- see handleWordBoundary.
function analyzeContext(textBeforeCursor) {
  const trailingWhitespaceMatch = textBeforeCursor.match(/\s+$/);
  const trailingWhitespace = trailingWhitespaceMatch ? trailingWhitespaceMatch[0] : '';
  const trimmed = textBeforeCursor.trim();
  const tokens = trimmed.length ? trimmed.split(/\s+/) : [];

  if (trailingWhitespace || tokens.length === 0) {
    return { state: 'boundary', lastCompletedWord: tokens[tokens.length - 1] || null, tokens, trailingWhitespace };
  }
  return { state: 'mid-word', partialWord: tokens[tokens.length - 1], precedingTokens: tokens.slice(0, -1) };
}

function updateCaretPosition(context, bundleId) {
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

  if (!positionTrusted && lastCaretBundleId !== bundleId) {
    const cursor = screen.getCursorScreenPoint();
    lastCaretX = cursor.x;
    lastCaretY = cursor.y;
  }
}

function clearNextWordDebounce() {
  if (nextWordDebounceTimer) {
    clearTimeout(nextWordDebounceTimer);
    nextWordDebounceTimer = null;
  }
}

// ---------- mid-word: fast, local, no debounce (cheap dictionary lookup) ----------
function handleMidWord(partialWord) {
  clearNextWordDebounce();

  if (partialWord.length < MIN_PARTIAL_WORD_LEN) {
    hideGhostText();
    return;
  }

  completeWordViaDaemon(partialWord, (result) => {
    console.log('[in-word] partial:', partialWord, '-> result:', JSON.stringify(result));
    if (!result || !result.suffix) {
      hideGhostText();
      return;
    }
    suggestionMode = 'in-word';
    currentInWordSuffix = result.suffix;
    currentInWordFullWord = partialWord + result.suffix;
    showGhost({ mode: 'in-word', suffix: result.suffix }, lastCaretX, lastCaretY);
  });
}

// ---------- word boundary: spellcheck immediately, else debounce next-word ----------
function handleWordBoundary(lastCompletedWord, tokens, rawTextBeforeCursor, trailingWhitespace) {
  if (!lastCompletedWord || lastCompletedWord.length < 2) {
    hideGhostText();
    clearNextWordDebounce();
    return;
  }

  // only offer correction for a plain alphabetic word -- one with punctuation
  // attached ("world.", "wow!") would need the accept-path to carefully
  // preserve that punctuation while deleting/retyping just the letters,
  // which isn't worth the complexity for what's already a less common case
  const isPlainWord = /^[a-zA-Z']+$/.test(lastCompletedWord);

  const proceedToNextWord = () => {
    clearNextWordDebounce();
    nextWordDebounceTimer = setTimeout(() => {
      requestNextWordSuggestion(tokens, rawTextBeforeCursor);
    }, NEXT_WORD_DEBOUNCE_MS);
  };

  if (!isPlainWord) {
    proceedToNextWord();
    return;
  }

  spellcheckViaDaemon(lastCompletedWord, (result) => {
    if (result && result.misspelled && result.suggestion) {
      clearNextWordDebounce();
      suggestionMode = 'correction';
      currentCorrection = { wrong: lastCompletedWord, correct: result.suggestion, trailingWhitespace };
      showGhost({ mode: 'correction', wrong: lastCompletedWord, correct: result.suggestion }, lastCaretX, lastCaretY);
      return;
    }

    // not misspelled (or spellcheck failed) -- debounce the next-word prediction.
    // Any further typing before this fires just resets the timer, so the
    // request only actually happens once the user pauses.
    proceedToNextWord();
  });
}

function requestNextWordSuggestion(tokens, rawTextBeforeCursor) {
  if (pendingRequest || acceptingInProgress) return;

  const contextTail = tokens.slice(-CONTEXT_WORD_COUNT).join(' ');
  if (!contextTail) return;

  pendingRequest = true;
  console.log('[predict] analyzing sentence:', contextTail);
  callDaemonForCompletion(contextTail, (completion) => {
    pendingRequest = false;
    console.log('[predict] daemon response:', completion);
    if (!completion) return;

    // clean up: take only the first line/fragment, strip leading ellipsis/punctuation
    let cleaned = completion.split('\n')[0].trim();
    cleaned = cleaned.replace(/^[.…\s]+/, ''); // strip leading … or .

    // safety net: if model echoed part of the input back, strip it out
    const tailWords = contextTail.split(/\s+/).slice(-6).join(' ');
    if (tailWords && cleaned.toLowerCase().startsWith(tailWords.toLowerCase())) {
      cleaned = cleaned.slice(tailWords.length).trim();
    }

    const words = cleaned
      .replace(/[,]/g, '')
      .split(/\s+/)
      .filter(w => w.length > 0)
      .slice(0, 4);

    if (words.length === 0) return;

    // reject if this exact phrase already appears anywhere in the typed text —
    // catches the model re-suggesting something the user already wrote,
    // which otherwise compounds into a repeat loop as words get Tab-accepted
    const suggestionPhrase = words.join(' ').toLowerCase();
    if (rawTextBeforeCursor.toLowerCase().includes(suggestionPhrase)) {
      console.log('[predict] suggestion duplicates already-typed text, discarding:', suggestionPhrase);
      return;
    }

    if (suggestionPhrase === lastShownSuggestion) {
      console.log('[predict] suppressing repeat suggestion');
      return;
    }
    lastShownSuggestion = suggestionPhrase;

    // the user may have kept typing (or the field/app changed) while this
    // request was in flight -- only show it if we're still in the same
    // boundary state that triggered it (a fresh mid-word or a newer
    // suggestion would already have taken over suggestionMode by now)
    if (suggestionMode === 'in-word' || suggestionMode === 'correction') return;

    suggestionMode = 'next-word';
    currentSuggestionWords = words;
    console.log('[predict] showing ghost at', lastCaretX, lastCaretY, 'words:', words);
    showGhost({ mode: 'next-word', words }, lastCaretX, lastCaretY);
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
      clearNextWordDebounce();
      scheduleNextPoll('noContext');
      return;
    }

    if (!context || !context.textBeforeCursor) {
      hideGhostText();
      clearNextWordDebounce();
      scheduleNextPoll('noContext');
      return;
    }

    // don't re-process if nothing changed since last poll
    if (context.textBeforeCursor === lastTextBeforeCursor) {
      scheduleNextPoll('paused');
      return;
    }

    // text changed underneath an old suggestion (user typed past it without
    // Tab, or accepted it and we're resyncing) — clear the stale pill
    if (suggestionMode) {
      hideGhostText();
    }

    lastTextBeforeCursor = context.textBeforeCursor;
    scheduleNextPoll('active');

    if (context.textBeforeCursor.trim().length < 2) {
      clearNextWordDebounce();
      return;
    }

    updateCaretPosition(context, bundleId);

    const analysis = analyzeContext(context.textBeforeCursor);
    if (analysis.state === 'mid-word') {
      handleMidWord(analysis.partialWord);
    } else {
      handleWordBoundary(analysis.lastCompletedWord, analysis.tokens, context.textBeforeCursor, analysis.trailingWhitespace);
    }
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
        acceptCurrentSuggestion();
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
  clearNextWordDebounce();
  if (activeRequest) { activeRequest.destroy(); activeRequest = null; }
  if (tabTapProcess && !tabTapProcess.killed) tabTapProcess.kill('SIGKILL');
  hideGhostText();
}

module.exports = { startPredictiveTyping, stopPredictiveTyping };

const { BrowserWindow, screen, ipcMain } = require('electron');
const { execFile, spawn } = require('child_process');
const http = require('http');
const path = require('path');

// Set MIRA_DEBUG_PREDICTIVE=1 to log every poll tick. Off by default: the
// polling loop runs for as long as Mira does, so leaving it on grows the log
// file continuously for no benefit.
const DEBUG_PREDICTIVE = process.env.MIRA_DEBUG_PREDICTIVE === '1';

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
const POLL_FAST_MS = 120;
const POLL_PAUSED_MS = 700;
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
const NEXT_WORD_DEBOUNCE_MS = 250;
const CONTEXT_WORD_COUNT = 8; // "at least 4-8 words" of context for next-word prediction
// Next-word prediction is the ONLY thing here that runs a language model, and
// running it after the first word or two is both the least useful (almost no
// context to go on) and the most expensive -- each call wakes a multi-gigabyte
// model that then sits resident. Waiting until there's a real sentence fragment
// to work with cuts the number of model loads sharply and improves the
// suggestions that do appear. In-word completion and spellcheck are unaffected:
// they're local lookups with no model behind them, so they stay instant from
// the first keystroke.
const MIN_WORDS_BEFORE_PREDICT = 2;
const MIN_PARTIAL_WORD_LEN = 3;

let ghostWindow = null;
let lastTextBeforeCursor = '';
// Where a suggestion should be drawn, and how much the position can be trusted.
//   'caret' -- a real measured caret rect; precise enough to draw inline on the
//              user's own text line.
//   'field' -- no caret rect available (Chromium-based apps expose none), so
//              anchored to the BOTTOM of the focused field and drawn under it
//              like a completion popup. Never inline: an inline guess here
//              would land on the wrong line.
//   'mouse' -- nothing better available.
let lastAnchorX = 100;
let lastAnchorY = 100;
// Top edge of whatever the suggestion must not cover (the caret's line, or the
// focused field). Used when there's no room below and the strip has to flip
// above -- flipping relative to lastAnchorY would land it ON the field.
let lastAnchorTopY = 100;
let lastAnchorMode = 'mouse';
let lastCaretH = 0;           // line height at the caret; 0 = unknown
let lastHasTextAfterCaret = false; // inline would overlap the app's own text
let lastCaretBundleId = null; // which app the anchor actually belongs to
let lastShownSuggestion = '';
let lastRecordedLength = 0; // how much of the current field has been learned from
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
  execFile(AX_HELPER_PATH, ['read'], { timeout: 2000 }, (err, stdout, stderr) => {
    if (err) {
      if (DEBUG_PREDICTIVE) console.log('[ax] read failed:', err.code || err.message, '| stderr:', (stderr || '').slice(0, 200));
      callback(null);
      return;
    }
    try {
      // parsed.bundleId is preserved even on parsed.error (e.g. "no focused
      // element" -- browsing without a text field focused) so the blocklist
      // check can still run without a second lookup
      callback(JSON.parse(stdout.trim()));
    } catch (e) {
      if (DEBUG_PREDICTIVE) console.log('[ax] unparseable stdout:', JSON.stringify((stdout || '').slice(0, 300)));
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

// Personal n-grams answer in under a millisecond, where the LLM takes seconds.
// Asking here first is what lets a suggestion appear while the user is still
// typing rather than a beat after they stop.
function localPredictViaDaemon(context, callback) {
  const url = `http://localhost:11200/complete/local?context=${encodeURIComponent(context)}&max_words=4`;
  http.get(url, { timeout: 1200 }, (res) => {
    let data = '';
    res.on('data', (c) => { data += c; });
    res.on('end', () => {
      try { callback(JSON.parse(data)); } catch (e) { callback(null); }
    });
  }).on('error', () => callback(null))
    .on('timeout', function () { this.destroy(); callback(null); });
}

// Feeds finished sentences back into the personal model, so Mira's suggestions
// get more accurate the more its owner writes.
function recordTypedText(text) {
  postFireAndForget('/typing/record', { text });
}

function midwordDecisionViaDaemon(partial, callback) {
  const url = `http://localhost:11200/complete/midword?partial=${encodeURIComponent(partial)}`;
  http.get(url, { timeout: 2000 }, (res) => {
    let data = '';
    res.on('data', (c) => { data += c; });
    res.on('end', () => {
      try { callback(JSON.parse(data)); } catch (e) { callback(null); }
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
    // Deliberately wider/taller than any suggestion needs. The window is
    // transparent and click-through, so extra area costs nothing visually,
    // and a fixed size avoids resizing (and re-flashing) it on every keystroke.
    width: 460,
    height: GHOST_WINDOW_HEIGHT,
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
  // The pipe can be gone while the reference still looks alive: tab_tap dies,
  // its stdin closes, and 'exit' hasn't fired yet -- so a write here throws
  // EPIPE synchronously. That crashed the whole main process with an "Uncaught
  // Exception" dialog. During the auto-restart window this is an entirely
  // expected state, not an error worth surfacing: the next spawn re-syncs the
  // flag anyway, since showGhost/hideGhostText set it on every change.
  const stdin = tabTapProcess && tabTapProcess.stdin;
  if (!stdin || !stdin.writable || tabTapProcess.killed) return;
  try {
    stdin.write((active ? 'ACTIVE' : 'INACTIVE') + '\n');
  } catch (e) {
    if (DEBUG_PREDICTIVE) console.log('[tab_tap] state write failed:', e.code || e.message);
  }
}

const GHOST_WINDOW_HEIGHT = 46;
const DEFAULT_LINE_HEIGHT = 18;

// Converts the caret rect's line height into a font size for the suggestion.
// A line's rect is taller than its glyphs by the font's leading; measured
// against TextEdit at 12pt (rect 14) and 36pt (rect 43), the ratio holds at
// ~0.84, which puts the suggestion on the same visual size as the text it
// continues.
function fontSizeForCaret(caretHeight) {
  const h = caretHeight > 4 ? caretHeight : DEFAULT_LINE_HEIGHT;
  return Math.max(10, Math.min(32, Math.round(h * 0.84)));
}

// AXBoundsForRange hands back the caret rect with its origin on the OPPOSITE
// vertical edge from the top-left screen space everything else here uses, so
// the reported y sits exactly one line height above the real line. Verified in
// TextEdit across four consecutive lines and at two font sizes: the corrected
// value lands exactly on the focused element's top for line one at both 12pt
// (217+14) and 36pt (188+43).
//
// When caretH is 0 the app returned a placeholder rect rather than a real
// measurement, and this correctly becomes a no-op -- those positions get
// rejected by the bounds checks anyway.
function caretTopFromRect(context) {
  return context.caretY + (context.caretH > 0 ? context.caretH : 0);
}

function showGhost(payload) {
  if (!ghostWindow || ghostWindow.isDestroyed()) createGhostWindow();

  const lineHeight = lastCaretH > 4 ? lastCaretH : DEFAULT_LINE_HEIGHT;
  const fontSize = fontSizeForCaret(lastCaretH);

  // Inline only when the position is a real measured caret AND nothing of the
  // user's own would be drawn over. Corrections are a distinct affordance
  // rather than a continuation of the sentence, so they always sit below.
  const inline =
    lastAnchorMode === 'caret' &&
    payload.mode !== 'correction' &&
    !lastHasTextAfterCaret;

  let posX = Math.round(lastAnchorX);
  let posY;

  if (inline) {
    posX += 1;
    posY = Math.round(lastAnchorY + lineHeight / 2 - GHOST_WINDOW_HEIGHT / 2);
  } else if (lastAnchorMode === 'field') {
    // lastAnchorY is already the field's BOTTOM edge -- sit just under it, the
    // way a completion popup does. Anchoring to the top (as this used to) put
    // the suggestion behind the field's own text once it ran to several lines.
    posY = Math.round(lastAnchorY) + 4;
  } else if (lastAnchorMode === 'caret') {
    posY = Math.round(lastAnchorY + lineHeight) + 4;
  } else {
    posY = Math.round(lastAnchorY) + 20;
  }

  posX = Math.max(4, posX);

  // Keep the strip on screen: a field near the bottom of a display would
  // otherwise push it off the edge entirely.
  const display = screen.getDisplayNearestPoint({ x: posX, y: posY });
  if (posY + GHOST_WINDOW_HEIGHT > display.bounds.y + display.bounds.height - 2) {
    posY = Math.round(lastAnchorTopY) - GHOST_WINDOW_HEIGHT - 2;
  }
  posX = Math.min(posX, display.bounds.x + display.bounds.width - 80);

  if (DEBUG_PREDICTIVE) {
    console.log('[ghost]', payload.mode, 'anchor=' + lastAnchorMode,
      'inline=' + inline, 'at', posX, posY, 'caretH=' + lastCaretH);
  }

  ghostWindow.setPosition(posX, posY);
  ghostWindow.webContents.executeJavaScript(
    `window.renderGhost && window.renderGhost(${JSON.stringify({ ...payload, fontSize })})`
  ).catch(() => {});
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
  if (DEBUG_PREDICTIVE) console.log('[tab] mode=', suggestionMode, 'suffix=', JSON.stringify(currentInWordSuffix), 'accepting=', acceptingInProgress);
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
    if (DEBUG_PREDICTIVE) console.log('[tab] insert', JSON.stringify(suffix), 'success=', success);
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
      showGhost({ mode: 'next-word', words: currentSuggestionWords });
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
  // Anchor selection, best source first. Only a real measured caret rect is
  // precise enough to draw on the user's own line; everything else is placed
  // below something, where being a few pixels out is harmless.
  lastHasTextAfterCaret = context.hasTextAfterCaret === true;

  const w = context.window;
  const caretUsable =
    typeof context.caretX === 'number' && context.caretX >= 0 &&
    typeof context.caretY === 'number' && context.caretY >= 0 &&
    context.caretH > 0;

  if (caretUsable) {
    // ax_helper measures a single character rather than the zero-length caret
    // range, so this is already the real line -- it only needs to be sanity
    // checked against the window (or failing that, the displays).
    const insideWindow = w
      ? context.caretX > w.x - 2 && context.caretX < w.x + w.width + 2 &&
        context.caretY > w.y - 2 && context.caretY < w.y + w.height + 2
      : null;

    const onDisplay = screen.getAllDisplays().some(d =>
      context.caretX >= d.bounds.x && context.caretX <= d.bounds.x + d.bounds.width &&
      context.caretY >= d.bounds.y && context.caretY <= d.bounds.y + d.bounds.height
    );

    if (insideWindow === true || (insideWindow === null && onDisplay)) {
      lastAnchorX = context.caretX;
      lastAnchorY = context.caretY;
      lastAnchorTopY = context.caretY;
      lastCaretH = context.caretH;
      lastAnchorMode = 'caret';
      lastCaretBundleId = bundleId;
      return;
    }
  }

  // No usable caret rect. Chromium-based apps (Electron, Chrome, VS Code, Slack)
  // return an all-zero placeholder for every bounds query, so this is the normal
  // path there, not an edge case. The field itself is still a good anchor.
  const e = context.element;
  if (e && w) {
    // An "element" the size of the whole window isn't a text field -- it's the
    // window reported as focused because nothing narrower was.
    const isRealField =
      e.width > 0 && e.height > 0 &&
      (e.width < w.width - 8 || e.height < w.height - 8);

    if (isRealField) {
      lastAnchorX = e.x + 2;
      lastAnchorY = e.y + e.height;   // BOTTOM edge; showGhost draws under it
      lastAnchorTopY = e.y;           // used if it has to flip above the field
      lastCaretH = 0;                 // unknown -- fall back to a default size
      lastAnchorMode = 'field';
      lastCaretBundleId = bundleId;
      return;
    }
  }

  // Nothing to anchor to. Only move to the pointer when the app actually
  // changed, so a suggestion doesn't jump around while typing in one place.
  if (lastCaretBundleId !== bundleId) {
    const cursor = screen.getCursorScreenPoint();
    lastAnchorX = cursor.x;
    lastAnchorY = cursor.y;
    lastAnchorTopY = cursor.y;
    lastCaretH = 0;
    lastAnchorMode = 'mouse';
    lastCaretBundleId = bundleId;
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

  // One call decides between "finish this word" and "you mistyped it". The
  // client used to infer that from two separate signals -- a missing
  // completion meant a typo -- which was wrong: "teh" completes to "tehran"
  // and "recieve" to "recieved", because the personal model learns its owner's
  // misspellings. The daemon now compares real-world word frequency instead.
  midwordDecisionViaDaemon(partialWord, (decision) => {
    if (DEBUG_PREDICTIVE) console.log('[in-word]', partialWord, '->', JSON.stringify(decision));
    if (!decision) { hideGhostText(); return; }

    if (decision.mode === 'complete' && decision.suffix) {
      suggestionMode = 'in-word';
      currentInWordSuffix = decision.suffix;
      currentInWordFullWord = partialWord + decision.suffix;
      showGhost({ mode: 'in-word', suffix: decision.suffix });
      return;
    }

    if (decision.mode === 'correct' && decision.suggestion) {
      suggestionMode = 'correction';
      currentCorrection = {
        wrong: partialWord,
        correct: decision.suggestion,
        trailingWhitespace: '',
      };
      showGhost({ mode: 'correction', wrong: partialWord, correct: decision.suggestion });
      return;
    }

    hideGhostText();
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

    // Instant path first: if the user's own writing already predicts what comes
    // next, show it now rather than making them wait on a model round-trip.
    if (tokens.length >= MIN_WORDS_BEFORE_PREDICT) {
      const contextTail = tokens.slice(-CONTEXT_WORD_COUNT).join(' ');
      localPredictViaDaemon(contextTail, (local) => {
        if (local && local.words && local.words.length && suggestionMode === null) {
          suggestionMode = 'next-word';
          currentSuggestionWords = local.words.slice();
          showGhost({ mode: 'next-word', words: local.words });
        }
      });
    }

    // The model still runs, and replaces the local guess if it has something
    // better -- the local model only knows phrases this user has typed before.
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
      showGhost({ mode: 'correction', wrong: lastCompletedWord, correct: result.suggestion });
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
  if (tokens.length < MIN_WORDS_BEFORE_PREDICT) return;

  const contextTail = tokens.slice(-CONTEXT_WORD_COUNT).join(' ');
  if (!contextTail) return;

  pendingRequest = true;
  if (DEBUG_PREDICTIVE) console.log('[predict] analyzing sentence:', contextTail);
  callDaemonForCompletion(contextTail, (completion) => {
    pendingRequest = false;
    if (DEBUG_PREDICTIVE) console.log('[predict] daemon response:', completion);
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
    if (DEBUG_PREDICTIVE) console.log('[predict] ghost', lastAnchorMode, lastAnchorX, lastAnchorY, words);
    showGhost({ mode: 'next-word', words });
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
    // This fires on every tick for as long as Mira runs, so it stays off unless
    // explicitly asked for -- left on it writes to the log file continuously.
    if (DEBUG_PREDICTIVE) {
      console.log('[poll] context:', context ? JSON.stringify(context).slice(0, 200) : null);
    }

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

    // Learn from finished sentences. Triggered on terminal punctuation so we
    // capture real, complete thoughts rather than half-typed fragments, and
    // only the newly-written part -- the AX read returns the whole field every
    // tick, so re-sending all of it would count the same sentence hundreds of
    // times and swamp the personal model.
    const typed = context.textBeforeCursor;
    if (typed.length < lastRecordedLength) lastRecordedLength = 0; // field cleared or switched
    if (/[.!?\n]\s*$/.test(typed) && typed.length > lastRecordedLength) {
      const fresh = typed.slice(lastRecordedLength).trim();
      if (fresh.length >= 12) recordTypedText(fresh);
      lastRecordedLength = typed.length;
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

// tab_tap is what actually turns a keystroke into "accept this suggestion" --
// if it dies (crash, or something on the system killing it by name/pattern)
// and nothing notices, Tab silently stops doing anything and every other part
// of predictive typing keeps running looking healthy. That happened for real:
// an unrelated `pkill -f tab_tap` during a debugging session took down a
// production Mira instance's own tab_tap with no visible symptom other than
// "predictive text stopped working." Auto-restart with backoff, capped so a
// permanently broken cause (e.g. Accessibility permission revoked) doesn't
// spin forever relaunching a process that will only fail again.
const TAB_TAP_MAX_RESTARTS = 5;
const TAB_TAP_RESTART_DELAY_MS = 2000;
let tabTapRestartCount = 0;

function spawnTabTap() {
  const proc = spawn(TAB_TAP_PATH, [], { stdio: ['pipe', 'pipe', 'pipe'] });
  tabTapProcess = proc;

  // EPIPE on these pipes is EXPECTED whenever tab_tap dies -- it is how the OS
  // reports that the other end is gone. It cannot be caught around the write:
  // Node builds the Error synchronously (so the stack points at our
  // stdin.write) but EMITS it on the stream afterwards, and a stream with no
  // 'error' listener re-throws it as an uncaught exception. That is what put
  // the "A JavaScript error occurred in the main process" dialog on screen and
  // took down all of Mira. Listening here is the fix; the try/catch around the
  // write only ever covered the rarer synchronous case.
  //
  // Nothing to do on error beyond staying quiet: the 'exit' handler below
  // respawns, and the next spawn re-syncs the active flag anyway.
  const ignorePipeError = (stream) => {
    if (stream) stream.on('error', (e) => {
      if (DEBUG_PREDICTIVE) console.log('[tab_tap] pipe error:', e.code || e.message);
    });
  };
  ignorePipeError(proc.stdin);
  ignorePipeError(proc.stdout);
  ignorePipeError(proc.stderr);
  // Same story for the process handle itself: a spawn failure (binary missing,
  // Accessibility revoked) emits 'error' rather than throwing.
  proc.on('error', (e) => {
    console.error('tab_tap failed to spawn:', e.message);
  });

  proc.stdout.on('data', (data) => {
    const lines = data.toString().split('\n').filter(l => l.trim().length > 0);
    for (const line of lines) {
      if (line.trim() === 'TAB_PRESSED') {
        acceptCurrentSuggestion();
      }
    }
  });

  proc.stderr.on('data', (data) => {
    console.error('tab_tap error:', data.toString());
  });

  proc.on('spawn', () => {
    tabTapRestartCount = 0; // a clean, longer-lived run resets the backoff budget
  });

  proc.on('exit', (code) => {
    console.log('tab_tap exited with code', code);
    // A restart may already have replaced this process. Without this guard the
    // dying process's handler clears the reference to its own REPLACEMENT and
    // then spawns a third -- so "Restart" would leave two tab_taps fighting
    // over the same Tab key.
    if (tabTapProcess !== proc) return;
    tabTapProcess = null;

    if (stopped) return; // a deliberate stopPredictiveTyping() -- not a crash
    if (tabTapRestartCount >= TAB_TAP_MAX_RESTARTS) {
      console.error(`tab_tap: giving up after ${TAB_TAP_MAX_RESTARTS} restarts -- Tab-to-accept is now dead until Mira restarts.`);
      return;
    }
    tabTapRestartCount++;
    setTimeout(() => { if (!stopped) spawnTabTap(); }, TAB_TAP_RESTART_DELAY_MS);
  });
}

function startPredictiveTyping() {
  stopped = false;
  pollTimer = setTimeout(pollAndSuggest, POLL_FAST_MS);
  tabTapRestartCount = 0;
  spawnTabTap();
}

function stopPredictiveTyping() {
  stopped = true;
  if (pollTimer) clearTimeout(pollTimer);
  clearNextWordDebounce();
  if (activeRequest) { activeRequest.destroy(); activeRequest = null; }
  if (tabTapProcess && !tabTapProcess.killed) tabTapProcess.kill('SIGKILL');
  hideGhostText();
}

// Predictive typing can die in ways the user notices but Mira doesn't report:
// tab_tap exhausts its restart budget (Accessibility revoked, binary replaced
// by a rebuild), or ax_helper starts failing. Rather than make "quit and
// reopen Mira" the only cure, expose an explicit restart and a status the
// Settings view can show.
function restartPredictiveTyping() {
  stopPredictiveTyping();
  // Let the SIGKILL actually land before spawning a replacement, so the two
  // never overlap on the Tab tap.
  return new Promise((resolve) => {
    setTimeout(() => {
      startPredictiveTyping();
      setTimeout(() => resolve(predictiveStatus()), 400);
    }, 250);
  });
}

function predictiveStatus() {
  return {
    running: !stopped,
    tabTapAlive: !!(tabTapProcess && !tabTapProcess.killed),
    tabTapGaveUp: tabTapRestartCount >= TAB_TAP_MAX_RESTARTS,
    restartCount: tabTapRestartCount,
    enabled,
  };
}

module.exports = {
  startPredictiveTyping,
  stopPredictiveTyping,
  restartPredictiveTyping,
  predictiveStatus,
};

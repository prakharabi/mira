const { app, BrowserWindow, screen, ipcMain, globalShortcut, nativeTheme, Tray, Menu, nativeImage, dialog, systemPreferences } = require('electron');
const { clipboard } = require('electron');
const http = require('http');
const { exec, execFile, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { startPredictiveTyping, stopPredictiveTyping,
        restartPredictiveTyping, predictiveStatus } = require('./predictive_typing');
const { startMeetingRecording, stopMeetingRecording } = require('./meeting_recorder');
const reminders = require('./reminders');

const OCR_HELPER_PATH = path.join(__dirname, '..', 'ocr_helper');

app.dock.hide();

let win = null;
let pillWindow = null;
let notchWindow = null;
let chatWindow = null;
let lastClipboard = clipboard.readText(); // seed with current clipboard so it doesn't trigger on startup
let isQuitting = false; // lets the workspace window's close handler tell "put away" from "really quit"

// tracks whether the pet was already visible BEFORE the current pill triggered it
// null = no pill-triggered show in progress; true/false = pet's visibility state before pill appeared
let petWasVisibleBeforePill = null;
let petPositionBeforePill = null;

function createReminder(title) {
  reminders.addReminder({ title }).then((res) => {
    if (res.success) console.log('Reminder created:', title);
    else console.log('Reminder creation failed:', res.error);
  });
}

function extractPhoneNumber(text) {
  const match = text.match(/(\+?\d[\d\s\-\(\)]{7,}\d)/);
  return match ? match[0].replace(/[\s\-\(\)]/g, '') : null;
}

function callDaemon(prompt, callback) {
  const url = `http://localhost:11200/ask?prompt=${encodeURIComponent(prompt)}`;
  http.get(url, (res) => {
    let data = '';
    res.on('data', (chunk) => { data += chunk; });
    res.on('end', () => {
      try {
        const parsed = JSON.parse(data);
        callback(parsed.response);
      } catch (e) {
        callback('Error: could not parse daemon response');
      }
    });
  }).on('error', (e) => {
    callback('Error: daemon not reachable');
  });
}

// triggers a "Hey Mira"-style voice command without needing the wake word --
// fire-and-forget, the daemon records/answers/speaks on its own
function triggerWakewordManually() {
  const req = http.request(
    'http://localhost:11200/wakeword/trigger',
    { method: 'POST', headers: { 'Content-Type': 'application/json' } },
    (res) => { res.on('data', () => {}); }
  );
  req.on('error', (e) => console.error('Could not trigger wake word:', e.message));
  req.end();
}

let resultWindow = null;

function showResultWindow(text, x, y) {
  if (resultWindow && !resultWindow.isDestroyed()) {
    resultWindow.close();
  }
  resultWindow = null;

  const thisResult = new BrowserWindow({
    width: 380,
    height: 260,
    minWidth: 300,
    minHeight: 160,
    frame: false,
    // vibrancy needs transparent:false -- the two are mutually exclusive, and
    // setting both silently yields a flat window with no material at all
    vibrancy: 'hud',
    visualEffectState: 'active',
    backgroundColor: '#00000000',
    roundedCorners: true,
    alwaysOnTop: true,
    resizable: true,          // long answers were previously clipped with no recourse
    hasShadow: true,
    skipTaskbar: true,
    show: false,
    center: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  resultWindow = thisResult;

  thisResult.loadFile('src/result.html');

  thisResult.once('ready-to-show', () => {
    const [w, h] = thisResult.getSize();
    const { workArea } = screen.getDisplayNearestPoint({ x, y });
    // Clamp into the visible work area. Opening at the cursor near a screen
    // edge used to push the window (and its close button) off-screen, which is
    // how a result could end up stuck with no way to dismiss it.
    const px = Math.min(Math.max(x, workArea.x + 8), workArea.x + workArea.width - w - 8);
    const py = Math.min(Math.max(y, workArea.y + 8), workArea.y + workArea.height - h - 8);
    thisResult.setPosition(Math.round(px), Math.round(py));
    thisResult.showInactive();
  });

  thisResult.webContents.once('did-finish-load', () => {
    thisResult.webContents.send('result-text', text);
  });

  thisResult.on('closed', () => {
    if (resultWindow === thisResult) resultWindow = null;
  });
}

// ---------- Notch UI ----------
// A second home for the same actions the copy-triggered pill offers
// (Search/Summarize/Remind/Hindi/Call), anchored where a physical notch
// sits (or would sit) rather than at the cursor -- built for the "circle
// something on screen" flow (⌘⇧O) specifically, so the whole interaction
// -- read the region, choose an action, see the answer -- happens in one
// place that grows in place instead of a chain of separate popups.
const NOTCH_COLLAPSED_HEIGHT = 34;
// Idle sits at the physical notch's own footprint on a notched MacBook (or
// the equivalent spot at screen-center-top on one without a hardware notch)
// -- narrow and flush with the true top edge, not workArea's, which starts
// BELOW the whole menu bar and left a visible gap under the real notch.
// These are close to a 14"/16" MacBook Pro's actual camera-housing size;
// being a little off costs nothing since idle is invisible anyway (see the
// CSS) -- what matters is the hover TARGET sits where the notch really is.
const NOTCH_IDLE_WIDTH = 200;
const NOTCH_IDLE_HEIGHT = 32;
const NOTCH_ACTIVE_WIDTH = 360;

function notchGeometry(height, width) {
  const display = screen.getPrimaryDisplay();
  const { bounds } = display;
  const w = width || NOTCH_ACTIVE_WIDTH;
  // Centered on the FULL screen (bounds), not workArea -- workArea is
  // narrowed by the Dock, which shifts its horizontal center away from the
  // true screen center where the physical notch actually sits. Centering
  // against workArea here was exactly the "not in center" bug.
  const x = Math.round(bounds.x + (bounds.width - w) / 2);
  // y:0, not workArea.y -- workArea already excludes the whole menu bar
  // height, which sat this well below where the physical notch actually is.
  // Flush against the true screen edge is what makes it read as tucked
  // under the notch rather than a window floating somewhere near the top.
  // (Confirmed the hard way: even a raw setBounds({y:0}) on a bare 'panel'
  // -type BrowserWindow still comes back y:33 -- AppKit constrains ANY
  // window's frame away from the menu bar with no public API to opt out,
  // so y:0 here is the ask and the OS silently clamps it to workArea.y,
  // which happens to be exactly flush with zero gap since that IS the
  // menu bar's height on this Mac.)
  return { x, y: 0, width: w, height };
}

function ensureNotchWindow() {
  if (notchWindow && !notchWindow.isDestroyed()) return notchWindow;

  notchWindow = new BrowserWindow({
    ...notchGeometry(NOTCH_IDLE_HEIGHT, NOTCH_IDLE_WIDTH),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    // Starts with no window shadow at all -- with fully transparent idle
    // content (see notch.html) a shadow would still draw a faint outline
    // around the invisible pill's bounds, which is exactly the "visible at
    // rest" problem this is fixing. Turned on only once actually expanded.
    hasShadow: false,
    skipTaskbar: true,
    focusable: true,  // needs to take clicks (action buttons), unlike the ghost overlay
    show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  notchWindow.setAlwaysOnTop(true, 'screen-saver');
  notchWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // Starts click-through: this window sits at screen-saver z-level, above
  // literally everything including other apps' title/tab bars, and with no
  // mouse-event handling of its own it was swallowing clicks meant for
  // whatever's underneath (browser tabs, most visibly) across its full
  // bounds -- even while idle and showing nothing. `forward: true` still
  // delivers move events through to the OS (needed for anything under the
  // cursor to redraw hover states normally); only clicks pass through.
  notchWindow.setIgnoreMouseEvents(true, { forward: true });

  notchWindow.loadFile('src/notch.html');
  notchWindow.on('closed', () => { notchWindow = null; });
  return notchWindow;
}

// Renderer reports its own content height (and whether it's the idle state,
// which uses the narrow notch-matched width instead of the wider active one)
// after each state change, so the window grows to fit exactly -- no dead
// transparent space below the rounded corners, no clipped content.
ipcMain.on('notch-resize', (event, { height, idle }) => {
  if (!notchWindow || notchWindow.isDestroyed()) return;
  notchWindow.setHasShadow(!idle);
  const h = idle
    ? Math.max(1, Math.round(height))
    : Math.max(NOTCH_COLLAPSED_HEIGHT, Math.min(420, Math.round(height)));
  const { x, y, width } = notchGeometry(h, idle ? NOTCH_IDLE_WIDTH : NOTCH_ACTIVE_WIDTH);
  // Second argument is macOS-only: animates the resize as a native window
  // transition instead of an instant jump between sizes, matching how the
  // reference UI grows/shrinks smoothly rather than snapping.
  notchWindow.setBounds({ x, y, width, height: h }, true);
  // The single, authoritative place click-through gets decided -- this
  // fires after EVERY state change (every show*/goIdle call in notch.html
  // ends in reportSize()), driven by the renderer's own DOM measurement of
  // whether there's real content on screen, not by which of several main.js
  // functions happened to be called on the way here. Scattering
  // setIgnoreMouseEvents(false) across each individual show* function
  // instead of here left a real gap: any transition that reached an active
  // state WITHOUT going through one of those exact call sites (or a timing
  // race between them) could leave the window uninterruptable while
  // invisible -- which is the "can't click what's under the notch" bug this
  // replaces. `idle` here is `!contentShown && !hovering` from the
  // renderer, so this can never disagree with what's actually on screen.
  notchWindow.setIgnoreMouseEvents(!!idle, { forward: true });
});

// The notch is a permanent fixture, not a per-use popup -- Escape/the close
// button returns it to its quiet idle pill rather than destroying the
// window, so the hover hint is still there next time the cursor passes by.
ipcMain.on('notch-close', () => {
  goNotchIdle();
});

ipcMain.on('notch-action', (event, { action, text }) => {
  if (action === 'remind') {
    const prompt = `Extract only the core task as a short reminder title, no explanation, just the title: ${text}`;
    callDaemon(prompt, (response) => {
      const title = response.trim();
      createReminder(title);
      if (notchWindow && !notchWindow.isDestroyed()) {
        notchWindow.webContents.send('notch-response', `Reminder created:\n"${title}"`);
      }
    });
    return;
  }

  if (action === 'call') {
    const number = extractPhoneNumber(text);
    const msg = number
      ? (require('electron').shell.openExternal(`tel:${number}`), `Calling ${number}...`)
      : 'No phone number found in that text.';
    if (notchWindow && !notchWindow.isDestroyed()) notchWindow.webContents.send('notch-response', msg);
    return;
  }

  let prompt = '';
  if (action === 'summarize') prompt = `Summarize this in 2 sentences: ${text}`;
  if (action === 'search') prompt = `Give a brief factual answer about: ${text}`;
  if (action === 'translate') prompt = `Translate this to Hindi, only give the translation, nothing else: ${text}`;

  callDaemon(prompt, (response) => {
    if (notchWindow && !notchWindow.isDestroyed()) notchWindow.webContents.send('notch-response', response);
  });
});

// Opens the notch already showing a collapsed "Reading..." state, for the
// moment between the circle gesture finishing and OCR text coming back --
// otherwise there's a silent gap with nothing on screen to say anything is
// happening at all.
function showNotchWorking(label) {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  w.setHasShadow(true);
  w.setBounds(notchGeometry(NOTCH_COLLAPSED_HEIGHT, NOTCH_ACTIVE_WIDTH), true);
  w.showInactive();
  const send = () => w.webContents.send('notch-collapsed', label);
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
}

function showNotchWithText(text) {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  const send = () => w.webContents.send('notch-context', text);
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
  w.showInactive();
}

function showNotchListening() {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  w.setHasShadow(true);
  w.setBounds(notchGeometry(NOTCH_COLLAPSED_HEIGHT, NOTCH_ACTIVE_WIDTH), true);
  w.showInactive();
  const send = () => w.webContents.send('notch-listening');
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
}

function showNotchSpeaking(text, duration) {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  w.setHasShadow(true);
  w.setBounds(notchGeometry(NOTCH_COLLAPSED_HEIGHT, NOTCH_ACTIVE_WIDTH), true);
  w.showInactive();
  const send = () => w.webContents.send('notch-speaking', { text: text || '', duration: duration || 0 });
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
}

// Unlike the other notch states (OCR/copy/listening/speaking), this one
// needs real keyboard focus -- w.show() rather than showInactive(), which
// every other notch trigger deliberately uses so it never steals focus from
// whatever the user was doing (typically mid-selection when copying text).
// Asking a typed question is the one flow where taking focus is the point.
function showNotchAsk() {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  w.setHasShadow(true);
  w.setBounds(notchGeometry(NOTCH_COLLAPSED_HEIGHT, NOTCH_ACTIVE_WIDTH), true);
  w.show();
  const send = () => w.webContents.send('notch-ask');
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
}

// A dedicated chat session, kept apart from the main chat window's history
// the same way WAKEWORD_SESSION_ID is -- a quick notch question and answer
// isn't necessarily something that belongs mixed into the visible chat log.
const NOTCH_ASK_SESSION_ID = 'notch_ask';

ipcMain.on('notch-ask-submit', (event, text) => {
  if (!text || !text.trim()) return;
  showNotchWorking('Thinking…');

  const req = http.request(
    { hostname: 'localhost', port: 11200, path: '/chat', method: 'POST',
      headers: { 'Content-Type': 'application/json' } },
    (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        if (!notchWindow || notchWindow.isDestroyed()) return;
        try {
          const parsed = JSON.parse(data);
          notchWindow.webContents.send('notch-ask-response',
            parsed.response || parsed.error || "Didn't get an answer that time.");
        } catch (e) {
          notchWindow.webContents.send('notch-ask-response', 'Error: could not reach Mira daemon');
        }
      });
    }
  );
  req.on('error', () => {
    if (notchWindow && !notchWindow.isDestroyed()) {
      notchWindow.webContents.send('notch-ask-response', 'Error: could not reach Mira daemon');
    }
  });
  req.write(JSON.stringify({ session_id: NOTCH_ASK_SESSION_ID, message: text, model: 'auto' }));
  req.end();
});

function hideNotchOnError() {
  goNotchIdle();
}

function goNotchIdle() {
  if (!notchWindow || notchWindow.isDestroyed()) return;
  notchWindow.setHasShadow(false);
  notchWindow.setBounds(notchGeometry(NOTCH_IDLE_HEIGHT, NOTCH_IDLE_WIDTH), true);
  notchWindow.webContents.send('notch-idle');
  // Back to click-through -- true rest means nothing is shown AND nothing
  // is clickable, so whatever's underneath (a browser tab, anything) gets
  // its clicks back instead of losing them to an invisible pill.
  notchWindow.setIgnoreMouseEvents(true, { forward: true });
}

// ---------- NEW: Chat window ----------
function toggleChatWindow() {
  if (chatWindow && !chatWindow.isDestroyed()) {
    if (chatWindow.isVisible()) {
      chatWindow.hide();
    } else {
      app.focus({ steal: true });
      chatWindow.show();
      chatWindow.focus();
    }
    return;
  }

  chatWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 860,
    minHeight: 560,
    // Real macOS window: the system draws the traffic lights, the rounded
    // corners and the shadow. 'hiddenInset' keeps the title bar out of the way
    // so the sidebar can run full height, which is what native apps built this
    // way (Mail, Notes, Finder) do.
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 19, y: 20 },
    // The sidebar material -- a genuine macOS blur that samples the desktop
    // behind it, rather than a CSS gradient imitating one. `transparent` must
    // stay false: it and vibrancy are mutually exclusive, and setting it would
    // silently give a flat window with no material at all.
    vibrancy: 'sidebar',
    visualEffectState: 'followWindow',
    backgroundColor: '#00000000',
    alwaysOnTop: false,
    resizable: true,
    skipTaskbar: false,
    show: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  });

  chatWindow.loadFile('src/workspace.html');

  chatWindow.once('ready-to-show', () => {
    app.focus({ steal: true });
    chatWindow.show();
    chatWindow.focus();
  });

  // The red traffic light hides the window instead of destroying it. Mira is a
  // background app (no dock icon), so closing is really "put it away" -- and
  // keeping the window alive preserves scroll position and in-progress input
  // for the next Control+Space.
  chatWindow.on('close', (e) => {
    if (isQuitting) return;
    e.preventDefault();
    chatWindow.hide();
  });

  chatWindow.on('closed', () => {
    chatWindow = null;
  });
}

// ---------- Quick Capture (Dump Box) ----------
// A Spotlight-style panel for getting a thought out of your head without
// opening the main window. The window is created once and reused: rebuilding it
// per invocation added a visible delay, which defeats the point of a capture
// tool you're supposed to reach for mid-thought.
let quickCaptureWindow = null;

function buildQuickCaptureWindow() {
  const win = new BrowserWindow({
    width: 560,
    height: 210,
    frame: false,
    vibrancy: 'hud',
    visualEffectState: 'active',
    backgroundColor: '#00000000',
    roundedCorners: true,
    alwaysOnTop: true,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  win.loadFile('src/quick_capture.html');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Dismiss on focus loss, the way Spotlight and other system panels behave --
  // except while the mic is live or a transcription is in flight, where hiding
  // would leave a recording running behind an invisible window.
  win.on('blur', () => {
    if (win.isVisible() && !quickCaptureBusy) win.hide();
  });

  return win;
}

let quickCaptureBusy = false;
ipcMain.on('quick-capture-busy', (event, busy) => { quickCaptureBusy = !!busy; });

function showQuickCapture() {
  if (!quickCaptureWindow || quickCaptureWindow.isDestroyed()) {
    quickCaptureWindow = buildQuickCaptureWindow();
  }

  // Open on the display the user is actually looking at, sitting slightly above
  // centre where a system panel would be, rather than always on the main screen.
  const cursor = screen.getCursorScreenPoint();
  const { bounds } = screen.getDisplayNearestPoint(cursor);
  const [w, h] = quickCaptureWindow.getSize();
  quickCaptureWindow.setPosition(
    Math.round(bounds.x + (bounds.width - w) / 2),
    Math.round(bounds.y + (bounds.height - h) / 3)
  );

  quickCaptureWindow.webContents.send('quick-capture-reset');
  // A background app (LSUIElement) is not the active app, and showing a window
  // does not make it one -- so the panel appeared without keyboard focus, and
  // its own blur handler then hid it again the moment anything else was
  // clicked. It looked like the shortcut simply didn't work. Stealing focus is
  // exactly what Spotlight-style panels do, and is required here.
  app.focus({ steal: true });
  quickCaptureWindow.show();
  quickCaptureWindow.focus();
}

ipcMain.on('quick-capture-close', () => {
  if (quickCaptureWindow && !quickCaptureWindow.isDestroyed()) quickCaptureWindow.hide();
});

ipcMain.on('quick-capture-done', (event, entryId) => {
  if (quickCaptureWindow && !quickCaptureWindow.isDestroyed()) quickCaptureWindow.hide();
  if (!entryId) return;

  // Fire-and-forget: the text is already stored, so a failed or slow model call
  // costs the user nothing -- the entry just stays unprocessed in the Dump Box.
  const body = JSON.stringify({ model: 'auto' });
  const req = http.request(
    `http://localhost:11200/dumpbox/${entryId}/process`,
    { method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } },
    (res) => { res.on('data', () => {}); }
  );
  req.on('error', (e) => console.error('Quick capture processing failed:', e.message));
  req.write(body);
  req.end();
});

// ---------- Menu bar ----------
let tray = null;

function buildTray() {
  const iconPath = path.join(__dirname, '..', 'assets', 'miraTemplate.png');
  const icon = nativeImage.createFromPath(iconPath);
  // Template images let macOS handle light/dark menu bars and the highlighted
  // state itself, instead of shipping two icons and guessing which to show.
  icon.setTemplateImage(true);

  tray = new Tray(icon);
  tray.setToolTip('Mira');
  refreshTrayMenu();

  // The assistant's visibility is shown as a checkbox, so the menu has to be
  // rebuilt whenever it changes -- by the shortcut, by the menu itself, or by
  // the pill borrowing it. Cheap, and the alternative is a checkbox that lies.
  if (win && !win.isDestroyed()) {
    win.on('show', refreshTrayMenu);
    win.on('hide', refreshTrayMenu);
  }
}

function refreshTrayMenu() {
  if (!tray || tray.isDestroyed()) return;

  const assistantVisible = !!(win && !win.isDestroyed() && win.isVisible());

  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `Mira ${app.getVersion()}`, enabled: false },
    { type: 'separator' },

    // Actions. Every accelerator below is a real globalShortcut registration
    // (see app.whenReady), so showing it here is accurate rather than
    // decorative -- a tray menu's accelerator does not itself bind anything.
    { label: 'Ask Mira', accelerator: 'Control+A', click: triggerWakewordManually },
    { label: 'Quick Capture…', accelerator: 'Control+D', click: showQuickCapture },
    { label: 'Capture Text (OCR)', accelerator: 'Control+Q', click: runOCR },
    { type: 'separator' },

    { label: 'Open Mira', accelerator: 'Control+Space', click: () => {
        if (chatWindow && !chatWindow.isDestroyed()) { chatWindow.show(); chatWindow.focus(); }
        else toggleChatWindow();
      } },
    {
      label: 'Show Assistant',
      type: 'checkbox',
      checked: assistantVisible,
      accelerator: 'Command+Shift+M',
      click: togglePet,
    },
    { type: 'separator' },

    // The workspace has nine views and the tray used to reach two of them.
    // These are the ones worth a single click; the rest are one nav away.
    { label: 'Tasks', click: () => openWorkspaceAt('tasks') },
    { label: 'Dump Box', click: () => openWorkspaceAt('dumpbox') },
    { label: 'Memory', click: () => openWorkspaceAt('memory') },
    { label: 'Meetings', click: () => openWorkspaceAt('meetings') },
    {
      label: 'More',
      submenu: [
        { label: 'Google', click: () => openWorkspaceAt('google') },
        { label: 'Automations', click: () => openWorkspaceAt('automations') },
      ],
    },
    { type: 'separator' },

    { label: 'Settings…', click: () => openWorkspaceAt('settings') },
    // No accelerator on Quit: Mira is an LSUIElement with no application menu,
    // so nothing binds Command+Q. Displaying it promised a shortcut that did
    // nothing at all.
    { label: 'Quit Mira', click: () => { isQuitting = true; app.quit(); } },
  ]));
}

function openWorkspaceAt(view) {
  const focusAndNavigate = () => {
    chatWindow.show();
    chatWindow.focus();
    chatWindow.webContents.send('navigate-to-view', view);
  };

  if (chatWindow && !chatWindow.isDestroyed()) {
    focusAndNavigate();
    return;
  }
  toggleChatWindow();
  chatWindow.webContents.once('did-finish-load', focusAndNavigate);
}

// Undoes what showPill() did to the pet: hide it again if the pill borrowed
// it from being hidden, or put it back where it actually was if it was
// already sitting on screen -- showPill() always repositions it next to the
// pill's location, and without restoring afterward it just stayed at
// whichever spot the most recent copy happened to be made from, forever.
function hidePetIfPillOpened() {
  if (win && !win.isDestroyed()) {
    if (petWasVisibleBeforePill === false) {
      win.hide();
    } else if (petWasVisibleBeforePill === true && petPositionBeforePill) {
      win.setPosition(petPositionBeforePill.x, petPositionBeforePill.y);
    }
  }
  petWasVisibleBeforePill = null;
  petPositionBeforePill = null;
}

ipcMain.on('close-result', () => {
  if (resultWindow && !resultWindow.isDestroyed()) {
    resultWindow.close();
  }
  resultWindow = null;
});

ipcMain.on('close-pill', () => {
  if (pillWindow && !pillWindow.isDestroyed()) {
    pillWindow.close();
  }
  pillWindow = null;
  hidePetIfPillOpened();
});

// ---------- NEW: chat window IPC handlers ----------
ipcMain.on('close-chat', () => {
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.hide();
  }
});

ipcMain.on('open-chat', () => {
  toggleChatWindow();
});

ipcMain.on('minimize-window', () => {
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.minimize();
  }
});

// ---------- NEW: meeting recording IPC handlers ----------
ipcMain.on('meeting-start', (event) => {
  startMeetingRecording((result, err) => {
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('meeting-start-result', { result, err });
    }
  });
});

ipcMain.on('meeting-stop', (event) => {
  autoMeetingActive = null;  // a manual stop ends whatever was running, auto or not
  stopMeetingRecording((result, err) => {
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('meeting-stop-result', { result, err });
    }
  });
});

// ---------- Auto-record scheduled meetings ----------
// Watches the user's Google Calendar and drives the exact same start/stop/
// transcribe pipeline as the manual Record button, timed to whichever event
// is currently in its window -- so a meeting scheduled with a Google Meet
// link gets recorded without anyone pressing anything. Only events with
// has_meet (see google_integration.py) qualify: a plain calendar block with
// no video call attached has nothing worth recording. Off by default (see
// auto_record_scheduled_meetings in main.py) since this starts the
// microphone and system audio entirely on its own.
const AUTO_MEETING_POLL_MS = 30000;  // fine enough to catch a start time within ~30s
const AUTO_MEETING_STATE_PATH = path.join(app.getPath('userData'), 'auto_meeting_state.json');
let autoMeetingActive = null;  // { eventId, endTime, summary } while an auto-started recording runs
let autoMeetingTimer = null;

function loadAutoMeetingState() {
  try {
    return JSON.parse(fs.readFileSync(AUTO_MEETING_STATE_PATH, 'utf8'));
  } catch (e) {
    return { recordedEventIds: [] };
  }
}

function saveAutoMeetingState(state) {
  try {
    fs.writeFileSync(AUTO_MEETING_STATE_PATH, JSON.stringify(state));
  } catch (e) {
    console.error('[auto-meeting] could not save state:', e.message);
  }
}

function fetchDaemonJson(url, callback) {
  http.get(url, (res) => {
    let data = '';
    res.on('data', (chunk) => { data += chunk; });
    res.on('end', () => {
      try { callback(JSON.parse(data), null); }
      catch (e) { callback(null, 'bad response from daemon'); }
    });
  }).on('error', (e) => callback(null, e.message));
}

function autoStartMeeting(ev) {
  console.log(`[auto-meeting] starting recording for "${ev.summary}"`);
  startMeetingRecording((result, err) => {
    if (err || !result || result.error) {
      // Most commonly: a manual recording was already in progress (the daemon
      // itself refuses a second /meeting/start) -- nothing to do but skip
      // this occurrence, not retry every poll for the rest of the meeting.
      console.error('[auto-meeting] start failed:', err || result.error);
      return;
    }
    autoMeetingActive = { eventId: ev.id, endTime: ev.end, summary: ev.summary };
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('meeting-start-result', { result, err: null });
    }
    const state = loadAutoMeetingState();
    state.recordedEventIds = [...new Set([...(state.recordedEventIds || []), ev.id])].slice(-200);
    saveAutoMeetingState(state);
  });
}

function autoStopMeeting() {
  const finished = autoMeetingActive;
  autoMeetingActive = null;
  console.log(`[auto-meeting] stopping recording for "${finished.summary}"`);
  stopMeetingRecording((result, err) => {
    // auto:true tells the renderer NOT to also call /transcribe-local-meeting
    // itself (its normal manual-stop behavior) -- this function already does
    // that below, and running both would transcribe the same file twice.
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('meeting-stop-result', { result, err, auto: true });
    }
    if (err || !result || result.error || !result.file) return;

    // Same call the manual Stop Recording flow makes -- this is the whole
    // point of "auto": a transcript shows up with nobody having clicked
    // anything.
    const req = http.request(
      { hostname: 'localhost', port: 11200, path: '/transcribe-local-meeting',
        method: 'POST', headers: { 'Content-Type': 'application/json' } },
      (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          if (!chatWindow || chatWindow.isDestroyed()) return;
          try {
            chatWindow.webContents.send('meeting-transcript-ready', JSON.parse(data));
          } catch (e) { /* Meetings view just won't get a live update; the file is still on disk */ }
        });
      }
    );
    req.on('error', () => {});
    req.write(JSON.stringify({ filepath: result.file }));
    req.end();
  });
}

function checkAutoMeetings() {
  fetchDaemonJson('http://localhost:11200/settings', (settings, err) => {
    if (err || !settings || !settings.auto_record_scheduled_meetings) return;

    fetchDaemonJson('http://localhost:11200/google/calendar?days=1&max_results=20', (data, err2) => {
      if (err2 || !data || data.error) return;
      const events = data.events || [];
      const now = Date.now();

      if (autoMeetingActive) {
        // Stop once the event's own scheduled end time has passed, not on a
        // fixed duration -- a 30-minute meeting and a 2-hour one both just
        // end when their calendar entry says they do.
        if (now >= Date.parse(autoMeetingActive.endTime)) autoStopMeeting();
        return;  // one auto-recording at a time
      }

      const state = loadAutoMeetingState();
      const recorded = new Set(state.recordedEventIds || []);
      const due = events.find((ev) =>
        ev.has_meet && !ev.all_day && !recorded.has(ev.id) &&
        now >= Date.parse(ev.start) && now < Date.parse(ev.end)
      );
      if (due) autoStartMeeting(due);
    });
  });
}

function startAutoMeetingWatcher() {
  if (autoMeetingTimer) return;
  autoMeetingTimer = setInterval(checkAutoMeetings, AUTO_MEETING_POLL_MS);
  checkAutoMeetings();  // don't wait a full interval for the first check
}

// ---------- Voice status: drives the notch's listening/speaking states ----------
// The daemon is a headless process and can't push into this renderer, so
// this poll is the only way Electron finds out "Mira is now listening" or
// "...now speaking" -- see voice_state.py for the daemon side, set from
// wakeword_listener.py's handle_wake_detected/speak_reply. 400ms keeps the
// notch's reaction feeling immediate without being a meaningfully heavier
// localhost request than the 30s meeting-watcher poll above.
const VOICE_POLL_MS = 400;
let lastVoiceUIState = 'idle';

function pollVoiceStatus() {
  fetchDaemonJson('http://localhost:11200/voice/status', (data, err) => {
    if (err || !data) return;
    const state = data.state || 'idle';
    if (state === lastVoiceUIState) return;
    lastVoiceUIState = state;

    if (state === 'listening') {
      showNotchListening();
      startListeningCaption();
    } else {
      // Any state other than listening means the daemon has moved past the
      // recording window this caption exists to cover -- stop polling for
      // it immediately rather than waiting on its own backstop timeout,
      // so a quick "heard nothing" round trip doesn't leave a stale caption
      // poll running into the next, unrelated state.
      stopListeningCaption();
      if (state === 'thinking') showNotchWorking('Thinking…');
      else if (state === 'speaking') showNotchSpeaking(data.text, data.duration);
      else goNotchIdle();  // idle -- only reached after one of the states above, so this is always a real return-to-rest
    }
  });
}

function startVoiceStatusWatcher() {
  setInterval(pollVoiceStatus, VOICE_POLL_MS);
}

// ---------- Reminders IPC (Dump Box action items) ----------
// Reminders access lives here rather than in the daemon -- see reminders.js for
// why. The renderer awaits these directly via ipcRenderer.invoke.
ipcMain.handle('reminders-lists', () => reminders.listLists());

ipcMain.handle('reminders-add', (event, item) => reminders.addReminder(item));

// ---------- Action queue: work the daemon can't do itself ----------
// Creating a reminder needs Apple Events, and the daemon is a headless
// LaunchAgent that can't get an Automation consent grant -- its osascript call
// would hang on a dialog nobody sees. So when Mira decides in conversation to
// create a reminder, the daemon queues it here and this poller executes it as
// the foreground app, then reports back so the model can confirm truthfully
// rather than claiming success it never had.
const ACTION_POLL_MS = 2000;
let actionPollTimer = null;

async function runQueuedAction(action) {
  if (action.type === 'create_reminder') {
    const p = action.params || {};
    return reminders.addReminder({
      title: p.title,
      note: p.note || '',
      due: p.due || null,
      // The queued action carries the configured list; hardcoding '' here sent
      // everything to the default list and silently ignored the setting.
      listName: p.list_name || '',
    });
  }
  return { success: false, error: `unknown action type: ${action.type}` };
}

function postActionResult(actionId, result) {
  const body = JSON.stringify({ result });
  const req = http.request(
    `http://localhost:11200/actions/${actionId}/result`,
    { method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } },
    (res) => { res.on('data', () => {}); }
  );
  req.on('error', () => {});
  req.write(body);
  req.end();
}

function pollActions() {
  http.get('http://localhost:11200/actions/pending', { timeout: 4000 }, (res) => {
    let data = '';
    res.on('data', (c) => { data += c; });
    res.on('end', async () => {
      try {
        const parsed = JSON.parse(data);
        for (const action of parsed.actions || []) {
          const result = await runQueuedAction(action);
          postActionResult(action.id, result);
        }
      } catch (e) { /* daemon restarting or mid-write -- next tick retries */ }
    });
  }).on('error', () => {})
    .on('timeout', function () { this.destroy(); });
}

// Appearance override. macOS apps are expected to follow the system setting by
// default while still letting the user pin light or dark, so 'system' hands
// control back to nativeTheme rather than freezing whatever is current.
ipcMain.handle('set-appearance', (event, mode) => {
  nativeTheme.themeSource = ['light', 'dark'].includes(mode) ? mode : 'system';
  return { applied: nativeTheme.themeSource };
});

// ---------- Launch at login ----------
// macOS's own Login Items list IS the persistent state here -- it survives
// reboots and is visible/removable in System Settings -- so there is
// deliberately no separate copy of this in the daemon's settings.json. Reading
// app.getLoginItemSettings() is always the source of truth, never a cache of it.
//
// This only works correctly against a PACKAGED app (Mira.app, built by
// scripts/build_app.js). Electron's own docs note that on macOS this API
// targets the app's own bundle path -- running via `npx electron .` means that
// path is node_modules/electron/dist/Electron.app, so toggling it on in dev
// would silently register a login item that launches a bare, unconfigured
// Electron shell rather than Mira.
ipcMain.handle('login-item-get', () => {
  return { openAtLogin: app.getLoginItemSettings().openAtLogin };
});

ipcMain.handle('login-item-set', (event, enabled) => {
  // openAsHidden matters here specifically because this app has no Dock icon
  // (LSUIElement) and no window on launch -- without it, some macOS versions
  // still surface a brief "just launched" state. It's a no-op when it doesn't
  // apply, so it's safe to always pass.
  app.setLoginItemSettings({ openAtLogin: !!enabled, openAsHidden: true });
  return { openAtLogin: app.getLoginItemSettings().openAtLogin };
});

// Opens a URL in the user's real browser. Used for the Google consent screen,
// which must run in a normal browser session rather than an Electron window.
// ---------- Predictive typing control ----------
// Exposed because predictive typing can stop working in ways that look like a
// hang from the outside: tab_tap exhausting its restart budget after an
// Accessibility grant is revoked, or the binary being swapped under it by a
// rebuild. Before this, the only cure was quitting and reopening Mira.
ipcMain.handle('predictive-status', () => ({
  ...predictiveStatus(),
  // The grant macOS actually checks is Mira.app's, not TabTap.app's: a child
  // process inherits its parent's TCC identity, so granting the helper alone
  // does nothing. Passing false checks without prompting.
  accessibilityTrusted: systemPreferences.isTrustedAccessibilityClient(false),
}));

// Prompting is what puts Mira.app into the Accessibility list under the right
// identity. Far more reliable than telling someone to find and add it by hand,
// which is also how people end up adding TabTap.app instead.
ipcMain.handle('predictive-request-accessibility', () => ({
  trusted: systemPreferences.isTrustedAccessibilityClient(true),
}));

// Ad-hoc signed builds get a new code hash on every rebuild, and macOS keys
// Accessibility grants to that hash -- so rebuilding silently revokes
// tab_tap's permission and Tab-to-accept dies with no visible cause. Give the
// user a direct route to the pane instead of "find it in System Settings".
ipcMain.handle('open-accessibility-settings', () => {
  return require('electron').shell.openExternal(
    'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
});
ipcMain.handle('predictive-restart', async () => ({
  ...(await restartPredictiveTyping()),
  accessibilityTrusted: systemPreferences.isTrustedAccessibilityClient(false),
}));

// Clears the stale Accessibility grant itself (see reset_permissions.sh,
// which this runs the same tccutil command as) rather than just opening the
// System Settings pane -- an ad-hoc rebuild leaves a grant sitting there
// that LOOKS fine (Mira is still listed, still checked) but no longer
// matches the running binary's new code hash, so nothing short of an actual
// reset gets the "add it again" prompt to fire on next launch. Only
// Accessibility, not the other services reset_permissions.sh covers -- this
// button lives next to Restart specifically for predictive typing.
ipcMain.handle('reset-accessibility-permission', () => {
  return new Promise((resolve) => {
    execFile('/usr/bin/tccutil', ['reset', 'Accessibility', 'com.mira.desktop'],
      (err) => {
        // tccutil exits non-zero when there's nothing to reset for that
        // bundle id -- not a real failure, just an empty no-op (same
        // reasoning reset_permissions.sh's `set -uo pipefail`, not `-e`,
        // documents). Only a launch failure (the binary itself missing) is
        // worth reporting back as an error.
        if (err && err.code === 'ENOENT') {
          resolve({ success: false, error: 'tccutil not found' });
          return;
        }
        resolve({
          success: true,
          accessibilityTrusted: systemPreferences.isTrustedAccessibilityClient(false),
        });
      });
  });
});

// Screen Recording (OCR) status for the Settings panel -- same rationale as
// the Accessibility row above: getMediaAccessStatus can't be prompted like
// Accessibility can (there's no equivalent "ask" call for it), so the button
// just opens the pane directly. See the comment on ocrPermissionDenied().
ipcMain.handle('ocr-permission-status', () => ({
  status: systemPreferences.getMediaAccessStatus('screen'),
}));
ipcMain.handle('open-screen-recording-settings', () => {
  return require('electron').shell.openExternal(
    'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
});

// ---------- Apple speech recognition ----------
// Runs from here, not the daemon: Speech Recognition is TCC-gated and a
// headless LaunchAgent can never be granted it. Spawned as a child of Mira so
// TCC reads Mira's own usage description (see build_app.js).
const SPEECH_HELPER_PATH = path.join(
  __dirname, '..', 'SpeechHelper.app', 'Contents', 'MacOS', 'speech_helper');
const FFMPEG_CANDIDATES = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg', 'ffmpeg'];

function ffmpegPath() {
  return FFMPEG_CANDIDATES.find(p => p === 'ffmpeg' || fs.existsSync(p)) || 'ffmpeg';
}

const SPEECH_HELPER_APP = path.join(__dirname, '..', 'SpeechHelper.app');

// Launched through LaunchServices, never spawned directly. A plain subprocess
// of a background app cannot raise the Speech Recognition consent prompt --
// macOS aborts it outright, or blocks it on a dialog that never appears. As a
// real foreground app it can ask properly, at the cost of losing stdout, so
// the helper writes its JSON to a file we poll for.
function runSpeechHelper(args, timeoutMs = 130000) {
  return new Promise((resolve) => {
    const outPath = path.join(os.tmpdir(), `mira-speech-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
    const full = args[0] === 'transcribe'
      ? ['transcribe', args[1], outPath, ...args.slice(2)]
      : [...args, outPath];

    execFile('/usr/bin/open', ['-a', SPEECH_HELPER_APP, '--args', ...full],
      { timeout: 15000 }, (err) => {
        if (err) { resolve({ error: `could not launch speech helper: ${err.message}` }); return; }

        const started = Date.now();
        const poll = setInterval(() => {
          if (fs.existsSync(outPath)) {
            clearInterval(poll);
            let parsed;
            try { parsed = JSON.parse(fs.readFileSync(outPath, 'utf8')); }
            catch (e) { parsed = { error: 'unreadable speech helper output' }; }
            try { fs.unlinkSync(outPath); } catch (e) {}
            resolve(parsed);
          } else if (Date.now() - started > timeoutMs) {
            clearInterval(poll);
            resolve({ error: 'speech helper timed out' });
          }
        }, 120);
      });
  });
}

ipcMain.handle('speech-check', (event, locale) =>
  runSpeechHelper(['check', locale || 'en-IN'], 130000));

// ---------- Live listening caption ----------
// A SEPARATE capture from the daemon's own PyAudio recording of the actual
// command (wakeword_listener.py's record_command_audio) -- this one exists
// purely to drive the notch's "what Mira is hearing" caption in real time,
// via speech_helper's new `stream` mode (see that file's own comment on why
// this runs as its own independent capture rather than piping the daemon's
// audio into a process only launchable through LaunchServices). Its
// transcript is never used to decide what Mira actually does; the daemon's
// Whisper/Groq pipeline remains the one that's acted on.
const CAPTION_STREAM_SECONDS = 4;  // must match wakeword_listener.py's COMMAND_RECORD_SECONDS
const CAPTION_POLL_MS = 150;
let captionPollTimer = null;

function startListeningCaption() {
  if (captionPollTimer || !fs.existsSync(SPEECH_HELPER_APP)) return;

  fetchDaemonJson('http://localhost:11200/settings', (settings) => {
    const locale = (settings && settings.dictation_locale) || 'en-IN';
    const outPath = path.join(os.tmpdir(), `mira-caption-${Date.now()}.json`);

    execFile('/usr/bin/open',
      ['-a', SPEECH_HELPER_APP, '--args', 'stream', outPath, locale, String(CAPTION_STREAM_SECONDS)],
      { timeout: 15000 }, (err) => { if (err) console.error('[caption] could not launch speech helper:', err.message); });

    let lastText = null;
    captionPollTimer = setInterval(() => {
      if (!fs.existsSync(outPath)) return;
      let parsed;
      try { parsed = JSON.parse(fs.readFileSync(outPath, 'utf8')); }
      catch (e) { return; }  // mid-write; try again next tick rather than treating a torn read as empty
      const text = parsed.partial || '';
      if (text !== lastText) {
        lastText = text;
        if (notchWindow && !notchWindow.isDestroyed()) {
          notchWindow.webContents.send('notch-listening-partial', text);
        }
      }
      if (parsed.done) stopListeningCaption(outPath);
    }, CAPTION_POLL_MS);

    // Backstop in case speech_helper never reports done:true (e.g. it never
    // got a final result before its own maxSeconds deadline) -- this poll
    // must not outlive the state it's captioning for.
    setTimeout(() => stopListeningCaption(outPath), (CAPTION_STREAM_SECONDS + 4) * 1000);
  });
}

function stopListeningCaption(outPath) {
  if (captionPollTimer) { clearInterval(captionPollTimer); captionPollTimer = null; }
  if (outPath) { try { fs.unlinkSync(outPath); } catch (e) {} }
}

ipcMain.handle('speech-transcribe', async (event, { buffer, locale }) => {
  if (!fs.existsSync(SPEECH_HELPER_APP)) return { error: 'speech helper not installed' };

  // The recorder produces webm/opus, which AVFoundation cannot read at all --
  // so this is a conversion step, not an optimisation.
  const stamp = Date.now();
  const inPath = path.join(os.tmpdir(), `mira-dictation-${stamp}.webm`);
  const wavPath = path.join(os.tmpdir(), `mira-dictation-${stamp}.wav`);

  try {
    fs.writeFileSync(inPath, Buffer.from(buffer));
    await new Promise((resolve, reject) => {
      execFile(ffmpegPath(), ['-y', '-i', inPath, '-ar', '16000', '-ac', '1', wavPath],
        { timeout: 30000 }, (err) => err ? reject(err) : resolve());
    });
    return await runSpeechHelper(['transcribe', wavPath, locale || 'en-IN']);
  } catch (e) {
    return { error: `audio conversion failed: ${e.message}` };
  } finally {
    for (const f of [inPath, wavPath]) {
      try { if (fs.existsSync(f)) fs.unlinkSync(f); } catch (_) {}
    }
  }
});

// Opens the workspace at a named view on launch. Exists so a view can be
// inspected during development without clicking through the UI to reach it.
//   MIRA_OPEN_VIEW=automations electron/dist/Mira.app/Contents/MacOS/Mira
if (process.env.MIRA_OPEN_VIEW) {
  app.whenReady().then(() => setTimeout(() => {
    openWorkspaceAt(process.env.MIRA_OPEN_VIEW);
    // MIRA_SCROLL_TO takes a CSS selector and scrolls it into view, so a
    // section below the fold can be inspected without synthetic scrolling.
    // MIRA_CLICK fires a real click on a selector after the view loads, so a
    // button's whole handler chain can be exercised the way a user would.
    // MIRA_EVAL runs a snippet in the renderer and logs its result, so a
    // renderer-side code path can be exercised in place rather than reasoned
    // about from the outside.
    if (process.env.MIRA_EVAL && chatWindow && !chatWindow.isDestroyed()) {
      setTimeout(() => chatWindow.webContents.executeJavaScript(process.env.MIRA_EVAL)
        .then(r => console.log('[MIRA_EVAL]', JSON.stringify(r)))
        .catch(e => console.log('[MIRA_EVAL] err', e.message)), 3000);
    }
    if (process.env.MIRA_CLICK && chatWindow && !chatWindow.isDestroyed()) {
      const sel = JSON.stringify(process.env.MIRA_CLICK);
      setTimeout(() => chatWindow.webContents.executeJavaScript(
        `(()=>{const el=document.querySelector(${sel}); if(!el) return 'NOT FOUND';
               if(el.hidden) return 'HIDDEN'; el.click(); return 'CLICKED';})()`
      ).then(r => console.log('[MIRA_CLICK]', r)).catch(e => console.log('[MIRA_CLICK] err', e.message)), 3000);
    }
    if (process.env.MIRA_SCROLL_TO && chatWindow && !chatWindow.isDestroyed()) {
      const sel = JSON.stringify(process.env.MIRA_SCROLL_TO);
      setTimeout(() => chatWindow.webContents.executeJavaScript(
        `document.querySelector(${sel})?.scrollIntoView({block:'start'})`
      ).catch(() => {}), 1200);
    }
  }, 2500));
}

// Opens the notch with a fixed test string, standing in for what runOCR()
// would hand it after a real circle-and-read -- the interactive region
// selection screencapture -i drives can't safely be scripted (it's a live
// mouse drag), so this is how the notch's own rendering, growth and daemon
// round trip get checked without touching the real mouse.
//   MIRA_TEST_NOTCH=1 electron/dist/Mira.app/Contents/MacOS/Mira
// Dispatches a synthetic mouseenter INSIDE the notch's own renderer (pure
// DOM event, no OS-level cursor movement) so the hover-reveal can be checked
// without touching the real mouse.
//   MIRA_TEST_NOTCH_HOVER=1 electron/dist/Mira.app/Contents/MacOS/Mira
if (process.env.MIRA_TEST_NOTCH_HOVER) {
  app.whenReady().then(() => setTimeout(() => {
    if (!notchWindow || notchWindow.isDestroyed()) return;
    console.log('[MIRA_TEST_NOTCH_HOVER] bounds before:', JSON.stringify(notchWindow.getBounds()));
    notchWindow.webContents.executeJavaScript(
      `document.body.dispatchEvent(new MouseEvent('mouseenter')); document.body.className`
    ).then(r => console.log('[MIRA_TEST_NOTCH_HOVER] body.className after:', r))
     .catch(e => console.log('[MIRA_TEST_NOTCH_HOVER] err', e.message));
    setTimeout(() => console.log('[MIRA_TEST_NOTCH_HOVER] bounds after:', JSON.stringify(notchWindow.getBounds())), 500);
  }, 2000));
}

// Exercises the listening/speaking notch UI directly, bypassing the daemon
// poll entirely -- calls the exact same showNotchListening/showNotchSpeaking
// functions pollVoiceStatus does, so this proves out the window+rendering
// path without needing a real wakeword trigger (which would record real
// mic audio and play real speech through the speakers unprompted).
//   MIRA_TEST_VOICE_STATE=listening electron/dist/Mira.app/Contents/MacOS/Mira
//   MIRA_TEST_VOICE_STATE=speaking electron/dist/Mira.app/Contents/MacOS/Mira
if (process.env.MIRA_TEST_VOICE_STATE) {
  app.whenReady().then(() => setTimeout(() => {
    const state = process.env.MIRA_TEST_VOICE_STATE;
    if (state === 'listening') showNotchListening();
    else if (state === 'listening-partial') {
      // Simulates main.js's own startListeningCaption() sending growing
      // partial text over time, without needing a real live mic capture
      // (which needs a human to answer the permission dialog) -- verifies
      // the last-8-words truncation and resize logic in notch.html.
      showNotchListening();
      const words = 'what is the weather like in san francisco today please'.split(' ');
      words.forEach((_, i) => {
        setTimeout(() => {
          if (notchWindow && !notchWindow.isDestroyed()) {
            notchWindow.webContents.send('notch-listening-partial', words.slice(0, i + 1).join(' '));
          }
        }, i * 300);
      });
    }
    else if (state === 'thinking') showNotchWorking('Thinking…');
    else if (state === 'speaking') showNotchSpeaking('This is a test of the speaking caption reveal in the notch.', 3);
    else if (state === 'ask') showNotchAsk();
    const checkDelay = state === 'speaking' ? 3400 : state === 'listening-partial' ? 3300 : 1000;
    setTimeout(() => {
      if (!notchWindow || notchWindow.isDestroyed()) return;
      console.log('[MIRA_TEST_VOICE_STATE] bounds:', JSON.stringify(notchWindow.getBounds()));
      notchWindow.webContents.executeJavaScript(`({
        bodyClass: document.body.className,
        listeningHidden: document.getElementById('listening').hidden,
        speakingHidden: document.getElementById('speaking').hidden,
        idleHidden: document.getElementById('idle').hidden,
        askHidden: document.getElementById('ask').hidden,
        voiceLabel: document.getElementById('voice-label').textContent,
        listeningLabel: document.getElementById('listening-label').textContent,
        barCount: document.querySelectorAll('#speaking .voice-bars span, #listening .voice-bars span').length,
      })`).then(r => console.log('[MIRA_TEST_VOICE_STATE] state:', JSON.stringify(r)));

      if (state === 'ask') {
        // Exercises the full type -> submit -> daemon /chat -> notch-ask-response
        // round trip, via a synthetic DOM value+Enter dispatch inside the
        // notch's own renderer -- no real keyboard/OS input involved.
        notchWindow.webContents.executeJavaScript(`
          (() => {
            const input = document.getElementById('ask-input');
            input.value = 'What is 9 times 7?';
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
          })()
        `);
        setTimeout(() => {
          if (!notchWindow || notchWindow.isDestroyed()) return;
          notchWindow.webContents.executeJavaScript(
            `document.getElementById('response-text').textContent`
          ).then(r => console.log('[MIRA_TEST_VOICE_STATE] ask response:', JSON.stringify(r)));
        }, 4000);
      }
    }, checkDelay);
  }, 1500));
}

if (process.env.MIRA_TEST_NOTCH) {
  app.whenReady().then(() => setTimeout(() => {
    showNotchWithText('Kai Brokering — Founder of VoiceOS (YC F25). If you want to reach me, find me on X. San Francisco, California, United States.');
    // Also exercises the full action -> daemon -> response chain, not just
    // the static layout, when MIRA_TEST_NOTCH_CLICK names an action.
    if (process.env.MIRA_TEST_NOTCH_CLICK) {
      setTimeout(() => {
        if (!notchWindow || notchWindow.isDestroyed()) return;
        const action = JSON.stringify(process.env.MIRA_TEST_NOTCH_CLICK);
        notchWindow.webContents.executeJavaScript(
          `document.querySelector('button[data-action=${action}]').click()`
        ).catch(e => console.log('[MIRA_TEST_NOTCH_CLICK] err', e.message));
      }, 1500);
    }
  }, 2000));
}

// ---------- Custom logo ----------
// The image lives in userData, never inside the .app: writing into the bundle
// breaks its code signature, and every rebuild would wipe it anyway.
const LOGO_DIR = path.join(app.getPath('userData'), 'branding');
const LOGO_EXTS = ['.png', '.gif', '.jpg', '.jpeg', '.webp'];

function customLogoPath() {
  for (const ext of LOGO_EXTS) {
    const p = path.join(LOGO_DIR, `logo${ext}`);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

ipcMain.handle('logo-get', () => {
  const p = customLogoPath();
  // Cache-bust on mtime: the renderer would otherwise keep showing the old
  // image after a replacement, since the file URL never changes.
  return p ? `file://${p}?v=${fs.statSync(p).mtimeMs}` : null;
});

ipcMain.handle('logo-choose', async () => {
  const { canceled, filePaths } = await dialog.showOpenDialog({
    title: 'Choose Mira\'s logo',
    properties: ['openFile'],
    filters: [{ name: 'Images', extensions: ['png', 'gif', 'jpg', 'jpeg', 'webp'] }],
  });
  if (canceled || !filePaths.length) return { ok: false, canceled: true };

  const src = filePaths[0];
  const ext = path.extname(src).toLowerCase();
  if (!LOGO_EXTS.includes(ext)) return { ok: false, error: 'Unsupported image type' };

  try {
    fs.mkdirSync(LOGO_DIR, { recursive: true });
    // Drop any previous logo first -- otherwise a PNG left behind would still
    // be found by customLogoPath() ahead of a newly chosen GIF.
    for (const e of LOGO_EXTS) {
      const old = path.join(LOGO_DIR, `logo${e}`);
      if (fs.existsSync(old)) fs.unlinkSync(old);
    }
    const dest = path.join(LOGO_DIR, `logo${ext}`);
    fs.copyFileSync(src, dest);
    broadcastLogoChange();
    return { ok: true, path: `file://${dest}?v=${Date.now()}` };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('logo-reset', () => {
  try {
    for (const e of LOGO_EXTS) {
      const p = path.join(LOGO_DIR, `logo${e}`);
      if (fs.existsSync(p)) fs.unlinkSync(p);
    }
    broadcastLogoChange();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

function broadcastLogoChange() {
  const p = customLogoPath();
  const url = p ? `file://${p}?v=${fs.statSync(p).mtimeMs}` : null;
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) w.webContents.send('logo-changed', url);
  }
}

// https only, with one exception: loopback over plain http. Locally hosted
// tools -- n8n at 127.0.0.1:5678 among them -- have no certificate and never
// will, and the reason this check exists is to stop arbitrary schemes and
// remote URLs reaching shell.openExternal, not to block the user's own
// machine. Loopback is matched by host, so an http:// URL anywhere else is
// still refused.
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

ipcMain.handle('open-external', (event, url) => {
  if (typeof url !== 'string') {
    return { success: false, error: 'No URL given.' };
  }

  let parsed;
  try {
    parsed = new URL(url);
  } catch (e) {
    return { success: false, error: `Not a valid URL: ${url}` };
  }

  const isHttps = parsed.protocol === 'https:';
  const isLocalHttp = parsed.protocol === 'http:' && LOOPBACK_HOSTS.has(parsed.hostname);
  if (!isHttps && !isLocalHttp) {
    return { success: false, error: `Refused to open ${parsed.protocol}//${parsed.hostname}` };
  }

  require('electron').shell.openExternal(url);
  return { success: true };
});

ipcMain.on('pill-action', (event, { action, text }) => {
  const { x, y } = screen.getCursorScreenPoint();

  if (action === 'remind') {
    const prompt = `Extract only the core task as a short reminder title, no explanation, just the title: ${text}`;
    callDaemon(prompt, (response) => {
      const title = response.trim();
      createReminder(title);
      showResultWindow(`Reminder created:\n"${title}"`, x, y);
    });
    return;
  }

  if (action === 'call') {
    const number = extractPhoneNumber(text);
    if (number) {
      require('electron').shell.openExternal(`tel:${number}`);
      showResultWindow(`Calling ${number}...`, x, y);
    } else {
      showResultWindow('No phone number found in selected text.', x, y);
    }
    return;
  }

  let prompt = '';
  if (action === 'summarize') prompt = `Summarize this in 2 sentences: ${text}`;
  if (action === 'search') prompt = `Give a brief factual answer about: ${text}`;
  if (action === 'translate') prompt = `Translate this to Hindi, only give the translation, nothing else: ${text}`;

  callDaemon(prompt, (response) => {
    showResultWindow(response, x, y);
  });
});

function createWindow() {
  const { width: screenWidth, height: screenHeight } = screen.getPrimaryDisplay().workAreaSize;

  win = new BrowserWindow({
    width: 160,
    height: 300,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: false,
    show: false,
  });

  win.loadFile('src/index.html');

  win.once('ready-to-show', () => {
    win.setPosition(screenWidth - 180, screenHeight - 200);
  });
}

function togglePet() {
  if (!win) return;
  if (win.isVisible()) {
    win.hide();
  } else {
    const { width: screenWidth, height: screenHeight } = screen.getPrimaryDisplay().workAreaSize;
    win.show();
    win.setPosition(screenWidth - 180, screenHeight - 200);
  }
}

function showPill(text) {
  if (pillWindow && !pillWindow.isDestroyed()) {
    pillWindow.close();
  }
  pillWindow = null;

  const { x, y } = screen.getCursorScreenPoint();

  // record pet's visibility (and, if already visible, its position) BEFORE
  // borrowing it to sit next to the pill -- both get restored in
  // hidePetIfPillOpened() once the pill closes.
  if (win && !win.isDestroyed()) {
    petWasVisibleBeforePill = win.isVisible();
    if (petWasVisibleBeforePill) {
      const [px, py] = win.getPosition();
      petPositionBeforePill = { x: px, y: py };
    }
    win.setPosition(x - 170, y - 40); // to the left of the pill, roughly vertically centered on it
    if (!petWasVisibleBeforePill) {
      win.show();
      win.setPosition(x - 170, y - 40);
    }
  }

  // The pill shows a Call button only when the copied text holds a phone
  // number (see pill.html), so the window has to be wide enough for it --
  // a fixed width clipped either the Call button or, without it, left a gap.
  const hasPhone = !!extractPhoneNumber(text || '');

  const thisPill = new BrowserWindow({
    width: hasPhone ? 462 : 396,
    height: 44,
    frame: false,
    vibrancy: 'hud',
    visualEffectState: 'active',
    backgroundColor: '#00000000',
    roundedCorners: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: true,
    skipTaskbar: true,
    show: false,
    center: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  pillWindow = thisPill;

  thisPill.loadFile('src/pill.html');

  thisPill.once('ready-to-show', () => {
    const [w, h] = thisPill.getSize();
    const { workArea } = screen.getDisplayNearestPoint({ x, y });
    // Same clamp as the result window: appearing at the cursor near a screen
    // edge used to push part of the pill (including its controls) off-screen.
    const px = Math.min(Math.max(x, workArea.x + 8), workArea.x + workArea.width - w - 8);
    const py = Math.min(Math.max(y, workArea.y + 8), workArea.y + workArea.height - h - 8);
    thisPill.setPosition(Math.round(px), Math.round(py));
    // showInactive: taking focus from whatever the user just selected text in
    // would deselect it, which is the one thing the pill must not do.
    thisPill.showInactive();

    setTimeout(() => {
      if (!thisPill.isDestroyed()) thisPill.close();
    }, 6000);
  });

  thisPill.webContents.once('did-finish-load', () => {
    thisPill.webContents.send('selected-text', text);
  });

  thisPill.on('closed', () => {
    if (pillWindow === thisPill) pillWindow = null;
    hidePetIfPillOpened();
  });
}

// ---------- System-wide OCR ----------
// Uses macOS's built-in interactive region picker (screencapture -i), then runs
// the captured region through a local Vision-framework helper (ocr_helper). The
// recognized text is written to the clipboard, which reuses the existing
// clipboard-watcher below to pop up the same Search/Summarize/Translate/Remind
// pill already built for selected text -- no separate UI needed for OCR results.
let ocrInProgress = false;

// screencapture is spawned as Mira's own child process, so macOS resolves the
// Screen Recording permission it needs against Mira.app's identity -- same
// "responsible process" rule already documented above for Accessibility (see
// open-accessibility-settings). Two things make this look like "granted it
// and it still doesn't work" in practice, and neither leaves any visible
// error without this check:
//   1. An ad-hoc signed dev build gets a new code hash every rebuild, and
//      macOS keys the grant to that hash, so a rebuild silently revokes it.
//   2. Unlike Accessibility, macOS caches a denied/undetermined Screen
//      Recording check for the lifetime of the process -- granting it in
//      System Settings does NOT take effect until Mira is fully quit and
//      reopened, not just refocused.
// Without this gate, a denied grant made screencapture -i either silently
// fail or hand back a truncated/black image, and runOCR had nothing to show
// for it beyond a bare "Reading..." that never resolved.
function ocrPermissionDenied() {
  const status = systemPreferences.getMediaAccessStatus('screen');
  return status !== 'granted';
}

function showNotchPermissionError(message) {
  const w = ensureNotchWindow();
  w.setIgnoreMouseEvents(false);
  const send = () => w.webContents.send('notch-ask-response', message);
  if (w.webContents.isLoadingMainFrame()) w.webContents.once('did-finish-load', send);
  else send();
  w.showInactive();
}

function runOCR() {
  if (ocrInProgress) return;

  if (ocrPermissionDenied()) {
    showNotchPermissionError(
      'Mira needs Screen Recording access for OCR.\n\n' +
      'Grant it in System Settings → Privacy & Security → Screen Recording, ' +
      'then fully quit and reopen Mira (a refocus is not enough -- macOS only ' +
      'applies the change on next launch). Opening Settings now…'
    );
    require('electron').shell.openExternal(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
    return;
  }

  ocrInProgress = true;

  const tmpImage = path.join(os.tmpdir(), `mira_ocr_${Date.now()}.png`);
  const capture = spawn('screencapture', ['-i', tmpImage]);

  capture.on('error', (e) => {
    console.error('screencapture failed to start:', e.message);
    ocrInProgress = false;
  });

  capture.on('close', () => {
    if (!fs.existsSync(tmpImage)) {
      // user pressed Escape / cancelled the region selection -- nothing to do
      ocrInProgress = false;
      return;
    }

    // The region is already circled at this point; OCR itself is the only
    // remaining wait, so the notch opens now rather than after -- otherwise
    // there's a silent gap between finishing the selection and anything
    // appearing on screen at all.
    showNotchWorking('Reading…');

    const ocr = spawn(OCR_HELPER_PATH, [tmpImage]);
    let output = '';
    let errOutput = '';
    ocr.stdout.on('data', (d) => { output += d.toString(); });
    ocr.stderr.on('data', (d) => { errOutput += d.toString(); });

    ocr.on('close', () => {
      fs.unlink(tmpImage, () => {});
      ocrInProgress = false;

      const text = output.trim();
      if (!text) {
        if (errOutput) console.error('ocr_helper error:', errOutput);
        hideNotchOnError();
        return;
      }

      // Clipboard write kept as-is -- Cmd+V of what was just circled should
      // still work regardless of which UI (notch or the old copy-triggered
      // pill) someone reaches for next. lastClipboard is updated in the same
      // breath so the clipboard-watcher below doesn't see this as a new
      // external copy and pop the OLD pill up too -- the notch already
      // covers this exact flow now.
      clipboard.writeText(text);
      lastClipboard = text;
      showNotchWithText(text);
    });
  });
}

app.whenReady().then(() => {
  createWindow();
  startPredictiveTyping();
  startAutoMeetingWatcher();
  startVoiceStatusWatcher();

  // The notch is created and shown once here, in its resting idle state, and
  // then runs for the lifetime of the app -- every other notch function
  // (working/context/response/idle) operates on this same window rather
  // than creating and destroying one per use.
  const notch = ensureNotchWindow();
  notch.once('ready-to-show', () => notch.showInactive());

  globalShortcut.register('Command+Shift+M', () => {
    togglePet();
  });

  // Control+Space opens/toggles the chat window ("open Mira")
  globalShortcut.register('Control+Space', () => {
    toggleChatWindow();
  });

  // Control+A triggers a "Hey Mira" voice command instantly, without
  // needing to say the wake word -- works system-wide, from any app.
  // (Was Control+Shift+A -> Command+Shift+L before that; a bare
  // Control+Option isn't a valid Electron accelerator -- it requires a real
  // key alongside modifiers, not just modifiers alone -- so this is the key
  // the user chose instead.)
  globalShortcut.register('Control+A', () => {
    triggerWakewordManually();
  });

  // Control+S opens a small text input right in the notch -- type a
  // question, get the answer back in the same panel, no chat window needed.
  globalShortcut.register('Control+S', () => {
    showNotchAsk();
  });

  // Control+Q starts a system-wide OCR capture -- drag-select any region of
  // the screen, recognized text lands on the clipboard and pops up the same
  // action pill used for selected text. (Was Command+Shift+O.)
  globalShortcut.register('Control+Q', () => {
    runOCR();
  });

  // Control+D opens Quick Capture -- dump a thought into the Dump Box from
  // any app without opening the main window. (Was Control+Shift+D ->
  // Command+Shift+D before that.)
  globalShortcut.register('Control+D', () => {
    showQuickCapture();
  });

  buildTray();

  actionPollTimer = setInterval(pollActions, ACTION_POLL_MS);

  setInterval(() => {
    const current = clipboard.readText();
    if (current && current !== lastClipboard) {
      lastClipboard = current;
      // Both triggers -- copying text, and circling a region with ⌘⇧O --
      // now land on the same notch UI rather than a copy going to the old
      // pill and only ⌘⇧O reaching the notch. The pill itself is left
      // intact in the code below rather than deleted outright, in case this
      // turns out to need reverting; it's just no longer wired to anything.
      showNotchWithText(current);
    }
  }, 500);
});

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('will-quit', () => {
  if (actionPollTimer) clearInterval(actionPollTimer);
  globalShortcut.unregisterAll();
  stopPredictiveTyping();
});

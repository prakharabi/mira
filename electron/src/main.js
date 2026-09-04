const { app, BrowserWindow, screen, ipcMain, globalShortcut, nativeTheme } = require('electron');
const { clipboard } = require('electron');
const http = require('http');
const { exec, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { startPredictiveTyping, stopPredictiveTyping } = require('./predictive_typing');
const { startMeetingRecording, stopMeetingRecording } = require('./meeting_recorder');
const reminders = require('./reminders');

const OCR_HELPER_PATH = path.join(__dirname, '..', 'ocr_helper');

app.dock.hide();

let win = null;
let pillWindow = null;
let chatWindow = null;
let lastClipboard = clipboard.readText(); // seed with current clipboard so it doesn't trigger on startup
let isQuitting = false; // lets the workspace window's close handler tell "put away" from "really quit"

// tracks whether the pet was already visible BEFORE the current pill triggered it
// null = no pill-triggered show in progress; true/false = pet's visibility state before pill appeared
let petWasVisibleBeforePill = null;

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
    width: 320,
    height: 180,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: true,
    skipTaskbar: true,
    show: false,
    center: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  resultWindow = thisResult;

  thisResult.loadFile('src/result.html');

  thisResult.once('ready-to-show', () => {
    thisResult.setPosition(x, y);
    thisResult.show();
    thisResult.setPosition(x, y);
  });

  thisResult.webContents.once('did-finish-load', () => {
    thisResult.webContents.send('result-text', text);
  });

  thisResult.on('closed', () => {
    if (resultWindow === thisResult) resultWindow = null;
  });
}

// ---------- NEW: Chat window ----------
function toggleChatWindow() {
  if (chatWindow && !chatWindow.isDestroyed()) {
    if (chatWindow.isVisible()) {
      chatWindow.hide();
    } else {
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

// hides the pet again, but ONLY if it wasn't already visible before the pill triggered it
function hidePetIfPillOpened() {
  if (petWasVisibleBeforePill === false && win && !win.isDestroyed() && win.isVisible()) {
    win.hide();
  }
  petWasVisibleBeforePill = null;
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
  stopMeetingRecording((result, err) => {
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('meeting-stop-result', { result, err });
    }
  });
});

// ---------- Reminders IPC (Dump Box action items) ----------
// Reminders access lives here rather than in the daemon -- see reminders.js for
// why. The renderer awaits these directly via ipcRenderer.invoke.
ipcMain.handle('reminders-lists', () => reminders.listLists());

ipcMain.handle('reminders-add', (event, item) => reminders.addReminder(item));

// Appearance override. macOS apps are expected to follow the system setting by
// default while still letting the user pin light or dark, so 'system' hands
// control back to nativeTheme rather than freezing whatever is current.
ipcMain.handle('set-appearance', (event, mode) => {
  nativeTheme.themeSource = ['light', 'dark'].includes(mode) ? mode : 'system';
  return { applied: nativeTheme.themeSource };
});

// Opens a URL in the user's real browser. Used for the Google consent screen,
// which must run in a normal browser session rather than an Electron window.
ipcMain.handle('open-external', (event, url) => {
  if (typeof url !== 'string' || !/^https:\/\//i.test(url)) {
    return { success: false, error: 'Only https URLs can be opened.' };
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

  // record pet's visibility BEFORE we potentially show it, then show it next to the pill (cursor position)
  if (win && !win.isDestroyed()) {
    petWasVisibleBeforePill = win.isVisible();
    win.setPosition(x - 170, y - 40); // to the left of the pill, roughly vertically centered on it
    if (!petWasVisibleBeforePill) {
      win.show();
      win.setPosition(x - 170, y - 40);
    }
  }

  const thisPill = new BrowserWindow({
    width: 300,
    height: 90,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: false,
    skipTaskbar: true,
    show: false,
    center: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false }
  });

  pillWindow = thisPill;

  thisPill.loadFile('src/pill.html');

  thisPill.once('ready-to-show', () => {
    thisPill.setPosition(x, y);
    thisPill.show();
    thisPill.setPosition(x, y);

    setTimeout(() => {
      if (!thisPill.isDestroyed()) {
        thisPill.close();
      }
    }, 5000);
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

function runOCR() {
  if (ocrInProgress) return;
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
        return;
      }

      clipboard.writeText(text);
    });
  });
}

app.whenReady().then(() => {
  createWindow();
  startPredictiveTyping();

  globalShortcut.register('Command+Shift+M', () => {
    togglePet();
  });

  // NEW: Control+Space opens/toggles the chat window
  globalShortcut.register('Control+Space', () => {
    toggleChatWindow();
  });

  // NEW: Command+Shift+L triggers a "Hey Mira" voice command instantly, without
  // needing to say the wake word -- works system-wide, from any app
  globalShortcut.register('Command+Shift+L', () => {
    triggerWakewordManually();
  });

  // NEW: Command+Shift+O starts a system-wide OCR capture -- drag-select any
  // region of the screen, recognized text lands on the clipboard and pops up
  // the same action pill used for selected text
  globalShortcut.register('Command+Shift+O', () => {
    runOCR();
  });

  setInterval(() => {
    const current = clipboard.readText();
    if (current && current !== lastClipboard) {
      lastClipboard = current;
      showPill(current);
    }
  }, 500);
});

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopPredictiveTyping();
});

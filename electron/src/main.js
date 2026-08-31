const { app, BrowserWindow, screen, ipcMain, globalShortcut } = require('electron');
const { clipboard } = require('electron');
const http = require('http');
const { exec } = require('child_process');
const { startPredictiveTyping, stopPredictiveTyping } = require('./predictive_typing');
const { startMeetingRecording, stopMeetingRecording } = require('./meeting_recorder');

app.dock.hide();

let win = null;
let pillWindow = null;
let chatWindow = null;
let lastClipboard = clipboard.readText(); // seed with current clipboard so it doesn't trigger on startup

// tracks whether the pet was already visible BEFORE the current pill triggered it
// null = no pill-triggered show in progress; true/false = pet's visibility state before pill appeared
let petWasVisibleBeforePill = null;

function createReminder(title) {
  const safeTitle = title.replace(/"/g, '\\"').replace(/\n/g, ' ').trim();
  const script = `tell application "Reminders" to make new reminder in list "Reminders" with properties {name:"${safeTitle}"}`;

  exec(`osascript -e '${script}'`, (error, stdout, stderr) => {
    if (error) {
      console.log('Reminder creation failed:', stderr);
    } else {
      console.log('Reminder created:', safeTitle);
    }
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
    frame: false,
    transparent: true,
    alwaysOnTop: false,
    resizable: true,
    hasShadow: true,
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

  setInterval(() => {
    const current = clipboard.readText();
    if (current && current !== lastClipboard) {
      lastClipboard = current;
      showPill(current);
    }
  }, 500);
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopPredictiveTyping();
});

// macOS Reminders access, owned by Electron rather than the Python daemon.
//
// Same architectural reason as system audio capture: controlling another app via
// Apple Events needs an Automation consent grant, and a headless LaunchAgent
// can't reliably obtain one -- the daemon's osascript call just hangs forever
// waiting on a dialog that never gets shown. Electron is a foreground GUI app,
// so it gets a real prompt the first time and the grant persists.
//
// Every value the user can influence is passed through `osascript ... -- argv`
// and read via `on run argv`, never interpolated into the script source. Task
// titles routinely contain quotes and backslashes, and string-building the
// script would make that an injection vector.

const { execFile } = require('child_process');

const ADD_SCRIPT = `
on run argv
    set theName to item 1 of argv
    set theBody to item 2 of argv
    set dueSpec to item 3 of argv
    set listName to item 4 of argv

    tell application "Reminders"
        if listName is "" then
            set targetList to default list
        else
            set targetList to list listName
        end if

        if dueSpec is "" then
            make new reminder at targetList with properties {name:theName, body:theBody}
        else
            set secsFromNow to dueSpec as integer
            make new reminder at targetList with properties {name:theName, body:theBody, due date:((current date) + secsFromNow)}
        end if
    end tell
    return "ok"
end run
`;

const LIST_SCRIPT = `
tell application "Reminders"
    set out to ""
    repeat with l in lists
        set out to out & (name of l) & linefeed
    end repeat
    return out
end tell
`;

// Reminders can be slow to cold-start, and the very first call also waits on the
// Automation consent dialog, so this is deliberately generous.
const TIMEOUT_MS = 60000;

function describeError(stderr) {
  const err = (stderr || '').trim();
  // -1743 is macOS's "user has not granted Automation permission". Naming it
  // matters because the fix is a one-time consent grant, not a retry.
  if (err.includes('-1743') || /not authorized/i.test(err)) {
    return 'Mira needs permission to control Reminders. Grant it in System Settings → '
         + 'Privacy & Security → Automation → Mira, then try again.';
  }
  if (err.includes('-1712') || /timed out/i.test(err)) {
    return 'Reminders did not respond in time. If a permission dialog is showing, '
         + 'approve it and try again.';
  }
  return err || 'Reminders command failed.';
}

// A YYYY-MM-DD date becomes an offset in seconds from now. AppleScript's own
// date-string parsing is locale-dependent and a known source of silent
// wrong-date bugs, so only a plain integer crosses the boundary.
function dueToOffsetSeconds(due) {
  if (!due) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due.trim());
  if (!m) return '';
  const target = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 9, 0, 0);
  if (Number.isNaN(target.getTime())) return '';
  return String(Math.round((target.getTime() - Date.now()) / 1000));
}

function listLists() {
  return new Promise((resolve) => {
    execFile('osascript', ['-e', LIST_SCRIPT], { timeout: TIMEOUT_MS }, (error, stdout, stderr) => {
      if (error) return resolve({ error: describeError(stderr || error.message) });
      const lists = stdout.split('\n').map(s => s.trim()).filter(Boolean);
      resolve({ lists });
    });
  });
}

function addReminder({ title, note = '', due = null, listName = '' }) {
  return new Promise((resolve) => {
    const name = (title || '').trim();
    if (!name) return resolve({ success: false, error: 'Reminder needs a title.' });

    const args = ['-e', ADD_SCRIPT, '--', name, note || '', dueToOffsetSeconds(due), listName || ''];
    execFile('osascript', args, { timeout: TIMEOUT_MS }, (error, stdout, stderr) => {
      if (error) {
        const message = describeError(stderr || error.message);
        return resolve({
          success: false,
          error: message,
          needsPermission: /permission|Automation/i.test(message),
        });
      }
      resolve({ success: true });
    });
  });
}

module.exports = { listLists, addReminder };

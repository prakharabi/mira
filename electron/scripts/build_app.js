#!/usr/bin/env node
// Packages Mira into a real double-clickable macOS app: Mira.app.
//
// Electron's own npm package already ships a prebuilt app shell
// (node_modules/electron/dist/Electron.app) -- that shell IS the Chromium/Node
// runtime. Running via `npx electron .` just points a generic copy of that
// shell at this folder. This script does the same thing electron-packager
// would, but by hand: copy the shell, rename it to Mira, point its Info.plist
// at a Mira identity instead of Electron's default one, drop our source into
// Contents/Resources/app, and re-sign. No extra dependency, no network
// install required.
//
// Login Items (Settings -> "Launch Mira at login") only works correctly
// against a packaged app on macOS -- Electron's own docs say so, and this is
// the reason that feature needs this script to exist at all.

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const APP_NAME = 'Mira';
const BUNDLE_ID = 'com.mira.desktop';

const SHELL_SRC = path.join(ROOT, 'node_modules', 'electron', 'dist', 'Electron.app');
const DIST_DIR = path.join(ROOT, 'dist');
const APP_DEST = path.join(DIST_DIR, `${APP_NAME}.app`);

// Everything under electron/ that the packaged app actually needs at runtime.
// node_modules is deliberately excluded: nothing in src/ requires a runtime
// package (electron itself is the only dependency, and its own runtime lives
// in the shell we're copying, not in node_modules/electron).
const INCLUDE = [
  'src',
  'assets',
  'AXHelper.app',
  'SpeechHelper.app',
  'MicHelper.app',
  'AudioHelper.app',
  'TabTap.app',
  // Loose binaries the code actually resolves via __dirname/.. -- the rest of
  // the helpers run from their .app bundles above, which is what carries their
  // TCC identity.
  'ocr_helper',
  'system_audio_helper',
  'package.json',
];

function log(msg) { console.log(`[build] ${msg}`); }

function rmrf(p) {
  if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
}

function copyInto(src, destDir) {
  const dest = path.join(destDir, path.basename(src));
  fs.cpSync(src, dest, { recursive: true, dereference: true });
}

function plistSet(plistPath, args) {
  // `plutil -replace` fails if the key doesn't already exist, and every key
  // this script sets is either new (LSUIElement, usage strings) or being
  // changed to a different type/value than Electron's stock plist has -- so
  // remove-then-add covers both cases uniformly instead of branching per key.
  try {
    execFileSync('/usr/bin/plutil', ['-remove', args[0], plistPath], { stdio: 'ignore' });
  } catch (e) { /* key didn't exist -- fine */ }
  execFileSync('/usr/bin/plutil', ['-insert', ...args, plistPath]);
}

function main() {
  if (!fs.existsSync(SHELL_SRC)) {
    console.error(`Electron shell not found at ${SHELL_SRC}. Run "npm install" first.`);
    process.exit(1);
  }

  // Rebuilding while the app is running leaves codesign trying to re-sign a
  // bundle macOS still has open, which fails with a wall of buffer output that
  // looks nothing like the actual problem. Quit it first.
  log('quitting any running instance...');
  try {
    execFileSync('/usr/bin/osascript', ['-e', `tell application id "${BUNDLE_ID}" to quit`],
                 { stdio: 'ignore' });
  } catch (e) { /* not running */ }
  try {
    execFileSync('/usr/bin/pkill', ['-f', `${APP_NAME}.app/Contents/MacOS/${APP_NAME}`],
                 { stdio: 'ignore' });
  } catch (e) { /* nothing to kill */ }

  log('cleaning previous build...');
  rmrf(DIST_DIR);
  fs.mkdirSync(DIST_DIR, { recursive: true });

  log('copying Electron shell...');
  // `ditto` (not fs.cpSync) is required here: Electron's shell contains
  // framework bundles with internal RELATIVE symlinks (Squirrel.framework's
  // Versions/Current layout). fs.cpSync's symlink handling rewrote those as
  // absolute paths pointing back into node_modules, which codesign then
  // rejects as "unsealed contents ... embedded framework". ditto is Apple's
  // own tool for copying bundles and preserves them correctly.
  execFileSync('/usr/bin/ditto', [SHELL_SRC, APP_DEST]);

  const contents = path.join(APP_DEST, 'Contents');
  const macos = path.join(contents, 'MacOS');
  const resources = path.join(contents, 'Resources');
  const plistPath = path.join(contents, 'Info.plist');

  log('renaming executable...');
  fs.renameSync(path.join(macos, 'Electron'), path.join(macos, APP_NAME));

  log('installing icon...');
  fs.copyFileSync(path.join(ROOT, 'assets', 'Mira.icns'), path.join(resources, `${APP_NAME}.icns`));
  // the stock Electron icon is dead weight once ours is in place
  rmrf(path.join(resources, 'electron.icns'));

  log('rewriting Info.plist...');
  plistSet(plistPath, ['CFBundleExecutable', '-string', APP_NAME]);
  plistSet(plistPath, ['CFBundleName', '-string', APP_NAME]);
  plistSet(plistPath, ['CFBundleDisplayName', '-string', APP_NAME]);
  plistSet(plistPath, ['CFBundleIdentifier', '-string', BUNDLE_ID]);
  plistSet(plistPath, ['CFBundleIconFile', '-string', APP_NAME]);
  // LSUIElement -- macOS's own "no Dock icon, no window on launch" flag. The
  // app.dock.hide() call in main.js still runs too, but that hides the icon a
  // beat AFTER Electron finishes launching, so without this the Dock icon
  // still briefly bounces on every start.
  plistSet(plistPath, ['LSUIElement', '-bool', 'true']);
  // Usage-description strings: macOS shows these in the TCC consent dialogs
  // the first time each permission is requested. Required for Microphone;
  // strongly recommended for Apple Events, since without one the system
  // dialog shows a blank, unhelpful reason.
  plistSet(plistPath, ['NSMicrophoneUsageDescription', '-string',
    'Mira uses the microphone for voice dictation and the "Hey Mira" wake word.']);
  plistSet(plistPath, ['NSAppleEventsUsageDescription', '-string',
    'Mira uses Apple Events to create reminders from the Dump Box.']);
  // Speech Recognition aborts a process outright if TCC cannot find this
  // string -- and it looks it up on the RESPONSIBLE process, which for a
  // helper Mira spawns is Mira itself. Without it here, speech_helper dies
  // with SIGABRT no matter what its own plist says.
  plistSet(plistPath, ['NSSpeechRecognitionUsageDescription', '-string',
    'Mira uses speech recognition to turn your dictation into text on this Mac.']);

  log('copying app source into Resources/app...');
  const appDir = path.join(resources, 'app');
  fs.mkdirSync(appDir, { recursive: true });
  for (const name of INCLUDE) {
    const src = path.join(ROOT, name);
    if (fs.existsSync(src)) copyInto(src, appDir);
    else log(`  (skipping missing ${name})`);
  }

  log('code signing (ad-hoc)...');
  execFileSync('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', APP_DEST]);

  log(`done: ${APP_DEST}`);
  return APP_DEST;
}

if (require.main === module) {
  main();
}

module.exports = { main, APP_DEST, APP_NAME, BUNDLE_ID };

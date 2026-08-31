const http = require('http');
const { spawn, execFile } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

// ARCHITECTURE NOTE:
// System audio capture (ScreenCaptureKit) is spawned HERE, directly by Electron,
// because Screen Recording permission can only be reliably granted to a foreground
// GUI app -- a headless background daemon (LaunchAgent) cannot get real, persistent
// consent for it (verified via testing: the OS re-prompts every time and the grant
// never sticks for a background process).
//
// Mic recording still happens in the Python daemon, since microphone access WAS
// solved there via a code-signed entitlement (com.apple.security.device.audio-input)
// on the daemon's Python interpreter -- no runtime prompt needed for that one.

const SYSTEM_AUDIO_HELPER_PATH = path.join(os.homedir(), 'Mira', 'electron', 'system_audio_helper');
const MEETINGS_DIR = path.join(os.homedir(), 'Mira', 'daemon', 'meetings');

let systemAudioProcess = null;
let currentSystemAudioFile = null;

function startMeetingRecording(callback) {
  // 1. tell the daemon to start mic recording
  const req = http.request(
    { hostname: 'localhost', port: 11200, path: '/meeting/start', method: 'POST' },
    (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(data);
          if (result.error) {
            callback(null, result.error);
            return;
          }

          // 2. spawn system_audio_helper HERE in Electron (foreground process)
          const basePath = result.base_path; // e.g. .../meetings/meeting_169...
          const systemAudioPath = `${basePath}_system.wav`;
          currentSystemAudioFile = systemAudioPath;

          systemAudioProcess = spawn(SYSTEM_AUDIO_HELPER_PATH, ['start', systemAudioPath], {
            stdio: ['ignore', 'pipe', 'pipe']
          });

          let startedOk = false;
          systemAudioProcess.stdout.on('data', (chunk) => {
            const line = chunk.toString();
            if (line.includes('"status":"recording"')) {
              startedOk = true;
            }
          });

          systemAudioProcess.stderr.on('data', (chunk) => {
            console.error('system_audio_helper stderr:', chunk.toString());
          });

          // give it a moment to actually start before reporting back
          setTimeout(() => {
            callback({ status: 'recording started', base_path: basePath, system_audio_started: startedOk }, null);
          }, 500);

        } catch (e) {
          callback(null, 'Could not parse daemon response');
        }
      });
    }
  );
  req.on('error', (e) => callback(null, e.message));
  req.end();
}

function stopMeetingRecording(callback) {
  // 1. stop system_audio_helper (Electron-spawned)
  if (systemAudioProcess) {
    execFile(SYSTEM_AUDIO_HELPER_PATH, ['stop'], () => {
      // give it time to finalize the WAV file before continuing
      setTimeout(() => finishStop(callback), 800);
    });
  } else {
    finishStop(callback);
  }
}

function finishStop(callback) {
  // 2. tell the daemon to stop mic recording AND merge the two files
  //    (system audio file is already on disk at the path the daemon expects,
  //    since we told it that exact path when we started)
  const req = http.request(
    { hostname: 'localhost', port: 11200, path: '/meeting/stop', method: 'POST' },
    (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        systemAudioProcess = null;
        currentSystemAudioFile = null;
        try {
          callback(JSON.parse(data), null);
        } catch (e) {
          callback(null, 'Could not parse daemon response');
        }
      });
    }
  );
  req.on('error', (e) => callback(null, e.message));
  req.end();
}

module.exports = { startMeetingRecording, stopMeetingRecording };

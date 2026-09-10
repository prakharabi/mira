const { ipcRenderer } = require('electron');

// Shared voice-to-text for capture fields (Dump Box view and Quick Capture).
//
// Toggle-based rather than the chat mic's press-and-hold: a dump is often a
// long, rambling thought, and holding a button down for a minute while trying
// to think is its own kind of friction.
//
// Transcription goes through the daemon's /transcribe, so it follows whatever
// STT engine and language settings the user has configured -- including the
// local-first whisper.cpp path, which matters here because a brain-dump is
// usually the most personal thing in the app.

// Whisper invents fluent sentences when handed silence -- recording nothing and
// stopping produced "You are asking me to do this. I am not asking you to do
// this." out of an empty room. For a capture tool that text is worse than
// useless, since the whole point is to trust what comes back. So the audio is
// metered while recording and never sent unless something was actually said.
const SILENCE_PEAK_THRESHOLD = 0.02; // normalised amplitude, ~ -34 dBFS
const MIN_SPEECH_MS = 300;


/**
 * Turn recorded audio into text, honouring the configured dictation engine.
 *
 * Exported because Mira has three separate mic buttons -- Dump Box, Quick
 * Capture, and the chat composer -- and the chat one had its own copy of this
 * that went straight to Whisper. Choosing Apple in Settings therefore did
 * nothing for the mic most people press most often. One implementation now, so
 * a fourth mic cannot drift the same way.
 *
 * Returns the transcript, or null if nothing could transcribe it.
 */
// Settings are read per transcription rather than cached: switching engine in
// Settings should take effect on the very next press, not after a restart.
async function fetchSettings() {
  try {
    return await (await fetch('http://localhost:11200/settings')).json();
  } catch (e) {
    return {};
  }
}

async function transcribeBlob(blob) {
  // Apple's recognizer first when chosen: it runs on this Mac, needs no API
  // key, and answers a short clip faster than a round trip to a cloud Whisper.
  // Whisper stays the fallback rather than being removed, because Apple's
  // engine can be unavailable (permission refused, a locale with no model) and
  // dictation failing outright is worse than dictation being a second slower.
  const settings = await fetchSettings();

  if ((settings.dictation_engine || 'apple') === 'apple') {
    try {
      const buffer = await blob.arrayBuffer();
      const res = await ipcRenderer.invoke('speech-transcribe', {
        buffer, locale: settings.dictation_locale || 'en-IN',
      });
      if (res && !res.error && typeof res.text === 'string') return res.text;
      if (res && res.error) {
        console.warn('[dictation] Apple engine unavailable, using Whisper:', res.error);
      }
    } catch (e) {
      console.warn('[dictation] Apple engine threw, using Whisper:', e.message);
    }
  }

  const form = new FormData();
  form.append('audio', blob, 'dictation.webm');
  const res = await fetch('http://localhost:11200/transcribe', { method: 'POST', body: form });
  const data = await res.json();
  if (data.error) return null;
  return data.text || '';
}

function createDictation({ onText, onStateChange, onError }) {
  let recorder = null;
  let stream = null;
  let chunks = [];
  let recording = false;
  let transcribing = false;
  let audioContext = null;
  let levelTimer = null;
  let speechMs = 0;

  function setState(state) {
    onStateChange && onStateChange(state);
  }

  async function start() {
    if (recording || transcribing) return;

    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      onError && onError('Microphone access denied or unavailable.');
      return;
    }

    chunks = [];
    speechMs = 0;

    // Meter the live signal so silence can be detected before anything is sent.
    try {
      audioContext = new AudioContext();
      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      const TICK_MS = 100;

      levelTimer = setInterval(() => {
        analyser.getFloatTimeDomainData(buf);
        let peak = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = Math.abs(buf[i]);
          if (v > peak) peak = v;
        }
        if (peak > SILENCE_PEAK_THRESHOLD) speechMs += TICK_MS;
      }, TICK_MS);
    } catch (e) {
      // Metering is a safety net, not a requirement -- if AudioContext isn't
      // available, fall through and let the transcript speak for itself.
      speechMs = Infinity;
    }

    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);

    recorder.onstop = async () => {
      // Release the mic before the network call, so the recording indicator in
      // the menu bar doesn't stay lit while transcription runs.
      if (stream) {
        stream.getTracks().forEach(t => t.stop());
        stream = null;
      }
      if (levelTimer) { clearInterval(levelTimer); levelTimer = null; }
      if (audioContext) { audioContext.close().catch(() => {}); audioContext = null; }

      const blob = new Blob(chunks, { type: 'audio/webm' });
      chunks = [];
      if (!blob.size) { setState('idle'); return; }

      if (speechMs < MIN_SPEECH_MS) {
        setState('idle');
        onError && onError("Didn't hear anything — nothing was added.");
        return;
      }

      transcribing = true;
      setState('transcribing');

      try {
        const text = await transcribeBlob(blob);
        if (text === null) onError && onError('Could not transcribe that.');
        else if (text.trim()) onText && onText(text.trim());
      } catch (e) {
        onError && onError('Could not reach Mira daemon.');
      } finally {
        transcribing = false;
        setState('idle');
      }
    };

    recorder.start();
    recording = true;
    setState('recording');
  }

  function stop() {
    if (!recording) return;
    recording = false;
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    recorder = null;
  }

  return {
    toggle() { recording ? stop() : start(); },
    stop,
    isRecording: () => recording,
    isBusy: () => recording || transcribing,
  };
}

module.exports = { createDictation, transcribeBlob };

// Transcription using Apple's Speech framework -- the same engine behind
// system dictation.
//
// Why this exists alongside Whisper: dictation is short, constant and wants to
// feel instant. Sending a five-second clip to a cloud Whisper endpoint costs a
// round trip and an API key for something the Mac can already do locally, and
// Apple's recognizer handles Indian English and Hindi well. Whisper stays the
// right tool for meeting-length audio, where this one is not built to compete.
//
// This runs from Electron rather than the daemon for the usual reason: Speech
// Recognition is a TCC-gated permission, and a headless LaunchAgent can never
// be granted one.
//
// Usage:
//   speech_helper check [locale]        -- authorization + on-device support
//   speech_helper locales               -- locales this Mac can recognize
//   speech_helper transcribe <file> <out.json> [locale] [--force-server]
//
// Results go to a FILE, not stdout, because this has to be launched through
// LaunchServices (`open -a`) rather than spawned directly. macOS will not let
// a plain subprocess of a background app raise the Speech Recognition consent
// prompt -- it either aborts the process or blocks forever on a dialog nobody
// can see. Launched as a real foreground app it can ask properly, but then
// stdout goes nowhere, hence the output file.
//
// Always prints one line of JSON, including on failure, so the caller never
// has to distinguish "crashed" from "said nothing".

import Foundation
import Speech

// MARK: - output

/// Set when invoked with an output path; results are written there as well as
/// to stdout, so the tool stays usable by hand.
var outputPath: String?

func emit(_ object: [String: Any]) -> Never {
    let payload = (try? JSONSerialization.data(withJSONObject: object))
        ?? Data("{\"error\":\"encoding failed\"}".utf8)
    if let outputPath = outputPath {
        // Written atomically: the caller polls for this file, and must never
        // read a half-written one.
        try? payload.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
    }
    FileHandle.standardOutput.write(payload)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(object["error"] == nil ? 0 : 1)
}

func fail(_ message: String, extra: [String: Any] = [:]) -> Never {
    var out: [String: Any] = ["error": message]
    out.merge(extra) { a, _ in a }
    emit(out)
}

// MARK: - authorization

/// Speech authorization is per-app and asked once. Blocking here is correct:
/// this is a one-shot CLI, and there is nothing useful to do until the user
/// has answered.
func authorize() -> SFSpeechRecognizerAuthorizationStatus {
    var status = SFSpeechRecognizer.authorizationStatus()
    if status == .notDetermined {
        var answered = false
        SFSpeechRecognizer.requestAuthorization { newStatus in
            status = newStatus
            answered = true
        }
        // Spin the run loop rather than blocking on a semaphore: the Speech
        // framework delivers these callbacks on the main queue, so a main
        // thread parked in sem.wait() deadlocks against the very callback it
        // is waiting for. Generous deadline -- the first call puts a consent
        // dialog on screen.
        let authDeadline = Date().addingTimeInterval(120)
        while !answered && Date() < authDeadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
    }
    return status
}

func describe(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

func makeRecognizer(_ localeID: String) -> SFSpeechRecognizer? {
    SFSpeechRecognizer(locale: Locale(identifier: localeID))
}

// MARK: - commands

func cmdLocales() -> Never {
    let ids = SFSpeechRecognizer.supportedLocales()
        .map { $0.identifier }
        .sorted()
    emit(["locales": ids, "count": ids.count])
}

func cmdCheck(_ localeID: String) -> Never {
    let status = authorize()
    guard let recognizer = makeRecognizer(localeID) else {
        fail("no recognizer for locale '\(localeID)'",
             extra: ["authorization": describe(status)])
    }
    emit([
        "authorization": describe(status),
        "locale": localeID,
        "available": recognizer.isAvailable,
        "supportsOnDevice": recognizer.supportsOnDeviceRecognition,
    ])
}

func cmdTranscribe(_ path: String, _ localeID: String, forceServer: Bool) -> Never {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else {
        fail("file not found: \(path)")
    }

    let status = authorize()
    guard status == .authorized else {
        fail("speech recognition not authorized", extra: ["authorization": describe(status)])
    }

    guard let recognizer = makeRecognizer(localeID) else {
        fail("no recognizer for locale '\(localeID)'")
    }
    guard recognizer.isAvailable else {
        fail("recognizer unavailable for '\(localeID)'")
    }

    let request = SFSpeechURLRecognitionRequest(url: url)
    // On-device keeps dictation local and removes the network from the latency
    // path. Not every locale has the model downloaded, so this is a request,
    // not a guarantee -- the framework falls back on its own.
    let wantsOnDevice = !forceServer && recognizer.supportsOnDeviceRecognition
    request.requiresOnDeviceRecognition = wantsOnDevice
    request.shouldReportPartialResults = false

    var finished = false
    var transcript: String?
    var failure: String?

    recognizer.recognitionTask(with: request) { result, error in
        if let error = error {
            // "No speech detected" is a normal outcome for a clip of silence,
            // not an error worth surfacing as one.
            let ns = error as NSError
            if ns.code == 1110 || ns.localizedDescription.lowercased().contains("no speech") {
                transcript = ""
            } else {
                failure = error.localizedDescription
            }
            finished = true
            return
        }
        guard let result = result else { return }
        if result.isFinal {
            transcript = result.bestTranscription.formattedString
            finished = true
        }
    }

    // Same reason as authorize(): the result callback arrives on the main
    // queue, so this thread has to keep servicing it rather than block.
    let deadline = Date().addingTimeInterval(120)
    while !finished && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    if !finished {
        fail("transcription timed out")
    }
    if let failure = failure {
        fail(failure, extra: ["locale": localeID])
    }

    emit([
        "text": transcript ?? "",
        "locale": localeID,
        "onDevice": wantsOnDevice,
    ])
}

// MARK: - entry

let args = CommandLine.arguments
guard args.count >= 2 else {
    fail("usage: speech_helper check|locales|transcribe <file> [locale]")
}

let defaultLocale = "en-IN"

switch args[1] {
case "locales":
    cmdLocales()
case "check":
    // check <locale> [out.json]
    if args.count >= 4 { outputPath = args[3] }
    cmdCheck(args.count >= 3 ? args[2] : defaultLocale)
case "transcribe":
    guard args.count >= 4 else { fail("transcribe needs an input file and an output path") }
    outputPath = args[3]
    let locale = (args.count >= 5 && !args[4].hasPrefix("--")) ? args[4] : defaultLocale
    cmdTranscribe(args[2], locale, forceServer: args.contains("--force-server"))
default:
    fail("unknown command '\(args[1])'")
}

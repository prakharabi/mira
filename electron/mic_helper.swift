import Foundation
import AVFoundation

// Records ONLY microphone audio via AVAudioEngine.
// Writes raw PCM samples directly to disk ourselves and constructs the WAV header
// manually, instead of relying on AVAudioFile (whose automatic header finalization
// proved unreliable in this async/queue recording context -- data was being written
// to disk but the WAV header's data-size field was never correctly updated, making
// every player/tool treat the file as empty even while it was actively growing).
//
// Usage:
//   mic_helper start /path/to/output.wav
//   mic_helper stop

let CONTROL_FILE = "/tmp/mira_mic_control"

func logError(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

// Writes a WAV header with placeholder sizes, to be patched at the end once we know
// the real total byte count.
func writeWAVHeaderPlaceholder(to handle: FileHandle, sampleRate: UInt32, channels: UInt16, bitsPerSample: UInt16) {
    var header = Data()
    let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample / 8)
    let blockAlign = channels * (bitsPerSample / 8)

    header.append(contentsOf: "RIFF".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(0).littleEndian) { Array($0) }) // placeholder chunk size
    header.append(contentsOf: "WAVE".utf8)
    header.append(contentsOf: "fmt ".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) }) // fmt chunk size
    header.append(contentsOf: withUnsafeBytes(of: UInt16(3).littleEndian) { Array($0) })  // format = 3 (IEEE float)
    header.append(contentsOf: withUnsafeBytes(of: channels.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: sampleRate.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: byteRate.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: blockAlign.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: bitsPerSample.littleEndian) { Array($0) })
    header.append(contentsOf: "data".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(0).littleEndian) { Array($0) }) // placeholder data size

    handle.write(header)
}

// Patches the RIFF chunk size and data chunk size fields now that we know the total
// number of PCM bytes written.
func patchWAVHeader(at url: URL, totalPCMBytes: UInt32) {
    guard let handle = try? FileHandle(forUpdating: url) else {
        logError("Could not open file to patch WAV header")
        return
    }
    defer { try? handle.close() }

    let riffChunkSize = 36 + totalPCMBytes // 4 (WAVE) + 24 (fmt chunk incl header) + 8 (data header) + data

    handle.seek(toFileOffset: 4)
    handle.write(withUnsafeBytes(of: riffChunkSize.littleEndian) { Data($0) })

    handle.seek(toFileOffset: 40)
    handle.write(withUnsafeBytes(of: totalPCMBytes.littleEndian) { Data($0) })
}

func runRecorder(outputPath: String) {
    let outputURL = URL(fileURLWithPath: outputPath)
    FileManager.default.createFile(atPath: outputPath, contents: nil)

    guard let fileHandle = try? FileHandle(forWritingTo: outputURL) else {
        print("{\"error\":\"failed to open output file for writing\"}")
        exit(1)
    }

    let engine = AVAudioEngine()
    let input = engine.inputNode
    let micFormat = input.outputFormat(forBus: 0)
    logError("Mic native format: \(micFormat)")

    let sampleRate = UInt32(micFormat.sampleRate)
    let channels = UInt16(micFormat.channelCount)
    let bitsPerSample: UInt16 = 32 // Float32

    writeWAVHeaderPlaceholder(to: fileHandle, sampleRate: sampleRate, channels: channels, bitsPerSample: bitsPerSample)

    var totalBytesWritten: UInt32 = 0
    let writeQueue = DispatchQueue(label: "com.mira.micwrite")

    input.installTap(onBus: 0, bufferSize: 1024, format: micFormat) { buffer, _ in
        guard let channelData = buffer.floatChannelData else { return }
        let frameLength = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)

        // interleave channels into a flat Float32 buffer for raw PCM writing
        var interleaved = [Float32](repeating: 0, count: frameLength * channelCount)
        for frame in 0..<frameLength {
            for ch in 0..<channelCount {
                interleaved[frame * channelCount + ch] = channelData[ch][frame]
            }
        }

        let byteCount = interleaved.count * MemoryLayout<Float32>.size
        let data = interleaved.withUnsafeBufferPointer { Data(buffer: $0) }

        writeQueue.async {
            fileHandle.write(data)
            totalBytesWritten += UInt32(byteCount)
        }
    }

    do {
        try engine.start()
        logError("Mic engine started successfully")
    } catch {
        print("{\"error\":\"failed to start engine: \(error.localizedDescription)\"}")
        exit(1)
    }

    print("{\"status\":\"recording\",\"file\":\"\(outputPath)\"}")
    fflush(stdout)

    while true {
        if FileManager.default.fileExists(atPath: CONTROL_FILE) {
            let contents = try? String(contentsOfFile: CONTROL_FILE, encoding: .utf8)
            if contents?.trimmingCharacters(in: .whitespacesAndNewlines) == "STOP" {
                try? FileManager.default.removeItem(atPath: CONTROL_FILE)
                break
            }
        }
        Thread.sleep(forTimeInterval: 0.3)
    }

    input.removeTap(onBus: 0)
    engine.stop()
    writeQueue.sync {} // wait for any in-flight write to finish

    try? fileHandle.close()
    patchWAVHeader(at: outputURL, totalPCMBytes: totalBytesWritten)

    logError("Total bytes written: \(totalBytesWritten)")
    print("{\"status\":\"stopped\",\"file\":\"\(outputPath)\",\"bytes\":\(totalBytesWritten)}")
    fflush(stdout)
    exit(0)
}

let args = CommandLine.arguments

if args.count >= 3 && args[1] == "start" {
    runRecorder(outputPath: args[2])
} else if args.count >= 2 && args[1] == "stop" {
    try? "STOP".write(toFile: CONTROL_FILE, atomically: true, encoding: .utf8)
    print("{\"status\":\"stop signal sent\"}")
} else {
    print("{\"error\":\"usage: mic_helper start <output.wav> | mic_helper stop\"}")
}

import Foundation
import AVFoundation

// Minimal isolated test: records ONLY the microphone via AVAudioEngine, no ScreenCaptureKit.
// Usage: mic_test_helper <output.wav> <seconds>

func logError(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

let args = CommandLine.arguments
guard args.count >= 3, let seconds = Double(args[2]) else {
    print("usage: mic_test_helper <output.wav> <seconds>")
    exit(1)
}

let outputPath = args[1]
let outputURL = URL(fileURLWithPath: outputPath)

let engine = AVAudioEngine()
let input = engine.inputNode
let micFormat = input.outputFormat(forBus: 0)
logError("Mic native format: \(micFormat)")

let settings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: micFormat.sampleRate,
    AVNumberOfChannelsKey: micFormat.channelCount,
    AVLinearPCMBitDepthKey: 32,
    AVLinearPCMIsFloatKey: true,
    AVLinearPCMIsBigEndianKey: false
]

guard let file = try? AVAudioFile(forWriting: outputURL, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false) else {
    logError("Failed to create output file")
    exit(1)
}

var frameCount: Int64 = 0

input.installTap(onBus: 0, bufferSize: 1024, format: micFormat) { buffer, _ in
    do {
        try file.write(from: buffer)
        frameCount += Int64(buffer.frameLength)
    } catch {
        logError("Write failed: \(error.localizedDescription)")
    }
}

do {
    try engine.start()
    logError("Engine started, recording for \(seconds) seconds...")
} catch {
    logError("Failed to start engine: \(error.localizedDescription)")
    exit(1)
}

Thread.sleep(forTimeInterval: seconds)

input.removeTap(onBus: 0)
engine.stop()
logError("Stopped. Total frames written: \(frameCount)")
print("{\"status\":\"done\",\"frames\":\(frameCount),\"file\":\"\(outputPath)\"}")

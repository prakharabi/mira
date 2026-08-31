import Foundation
import ScreenCaptureKit
import AVFoundation

// Records system audio (via ScreenCaptureKit) and microphone audio (via AVAudioEngine)
// to TWO SEPARATE WAV files. They are merged into one mixed file afterward by the
// Python daemon using ffmpeg (amix filter) -- this avoids the bug in earlier versions
// where writing two independent live audio streams into a single AVAudioFile
// concatenated them in time instead of mixing them, producing slowed/choppy playback.
//
// Usage:
//   audio_helper start /path/to/output_basename   (produces output_basename_mic.wav and output_basename_system.wav)
//   audio_helper stop

let CONTROL_FILE = "/tmp/mira_meeting_control"
let micWriteQueue = DispatchQueue(label: "com.mira.micwrite")
let systemWriteQueue = DispatchQueue(label: "com.mira.systemwrite")

func logError(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

// Format all audio is converted to before writing. Float32 deinterleaved is required
// because AVAudioFile.write(from:) always expects buffers in this canonical processing
// format, regardless of the on-disk file settings passed at creation.
let fileFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 2, interleaved: false)!

class MeetingRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    var audioEngine: AVAudioEngine?
    var micFile: AVAudioFile?
    var systemFile: AVAudioFile?
    let micURL: URL
    let systemURL: URL
    var isRecording = false
    var systemAudioFrameCount: Int64 = 0
    var micFrameCount: Int64 = 0
    var micConverter: AVAudioConverter?

    init(basePath: String) {
        self.micURL = URL(fileURLWithPath: basePath + "_mic.wav")
        self.systemURL = URL(fileURLWithPath: basePath + "_system.wav")
        super.init()
    }

    func start() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                print("{\"error\":\"no display found for capture\"}")
                exit(1)
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 48000
            config.channelCount = 2
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: 48000,
                AVNumberOfChannelsKey: 2,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false
            ]

            do {
                micFile = try AVAudioFile(forWriting: micURL, settings: settings)
                systemFile = try AVAudioFile(forWriting: systemURL, settings: settings)
            } catch {
                print("{\"error\":\"failed to create output files: \(error.localizedDescription)\"}")
                exit(1)
            }

            stream = SCStream(filter: filter, configuration: config, delegate: self)
            try stream?.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio.capture.queue"))
            try await stream?.startCapture()

            setupMicCapture()

            isRecording = true
            print("{\"status\":\"recording\",\"micFile\":\"\(micURL.path)\",\"systemFile\":\"\(systemURL.path)\"}")
            fflush(stdout)

            while isRecording {
                if FileManager.default.fileExists(atPath: CONTROL_FILE) {
                    let contents = try? String(contentsOfFile: CONTROL_FILE, encoding: .utf8)
                    if contents?.trimmingCharacters(in: .whitespacesAndNewlines) == "STOP" {
                        try? FileManager.default.removeItem(atPath: CONTROL_FILE)
                        await stopRecording()
                        break
                    }
                }
                try? await Task.sleep(nanoseconds: 300_000_000)
            }
        } catch {
            print("{\"error\":\"\(error.localizedDescription)\"}")
            exit(1)
        }
    }

    func setupMicCapture() {
        audioEngine = AVAudioEngine()
        guard let engine = audioEngine else {
            logError("Failed to create AVAudioEngine")
            return
        }
        let input = engine.inputNode
        let micFormat = input.outputFormat(forBus: 0)
        logError("Mic native format: \(micFormat)")

        micConverter = AVAudioConverter(from: micFormat, to: fileFormat)
        if micConverter == nil {
            logError("Failed to create mic AVAudioConverter from \(micFormat) to \(fileFormat)")
        }

        input.installTap(onBus: 0, bufferSize: 1024, format: micFormat) { [weak self] buffer, _ in
            guard let self = self, let converter = self.micConverter else { return }

            let outputFrameCapacity = AVAudioFrameCount(Double(buffer.frameLength) * (fileFormat.sampleRate / micFormat.sampleRate)) + 16
            guard let convertedBuffer = AVAudioPCMBuffer(pcmFormat: fileFormat, frameCapacity: outputFrameCapacity) else { return }

            var error: NSError?
            let status = converter.convert(to: convertedBuffer, error: &error) { _, outStatus in
                outStatus.pointee = .haveData
                return buffer
            }

            if status == .error {
                logError("Mic conversion failed: \(error?.localizedDescription ?? "unknown")")
                return
            }

            micWriteQueue.async {
                guard let file = self.micFile, self.isRecording else { return }
                do {
                    try file.write(from: convertedBuffer)
                    self.micFrameCount += Int64(convertedBuffer.frameLength)
                } catch {
                    logError("Mic write failed: \(error.localizedDescription)")
                }
            }
        }

        do {
            try engine.start()
            logError("AVAudioEngine started successfully")
        } catch {
            logError("Failed to start AVAudioEngine: \(error.localizedDescription)")
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, isRecording else { return }

        var bufferListSizeNeeded: Int = 0
        var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &bufferListSizeNeeded,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )
        guard status == noErr, bufferListSizeNeeded > 0 else {
            logError("Could not determine audio buffer list size: \(status)")
            return
        }

        let rawListPointer = UnsafeMutableRawPointer.allocate(byteCount: bufferListSizeNeeded, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { rawListPointer.deallocate() }
        let audioBufferListPtr = rawListPointer.bindMemory(to: AudioBufferList.self, capacity: 1)

        var blockBuffer: CMBlockBuffer?
        status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: audioBufferListPtr,
            bufferListSize: bufferListSizeNeeded,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else {
            logError("CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer failed: \(status)")
            return
        }

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            logError("Could not read format description from system audio buffer")
            return
        }

        guard let sourceFormat = AVAudioFormat(streamDescription: asbd) else {
            logError("Could not create AVAudioFormat from system audio stream description")
            return
        }

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0 else { return }

        let audioBufferList = UnsafeMutableAudioBufferListPointer(audioBufferListPtr)
        guard let sourcePcmBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: frameCount) else { return }
        sourcePcmBuffer.frameLength = frameCount

        for (i, buf) in audioBufferList.enumerated() {
            guard let srcData = buf.mData else { continue }
            if let dstFloat = sourcePcmBuffer.floatChannelData?[i] {
                memcpy(dstFloat, srcData, Int(buf.mDataByteSize))
            } else if let dstInt16 = sourcePcmBuffer.int16ChannelData?[i] {
                memcpy(dstInt16, srcData, Int(buf.mDataByteSize))
            }
        }

        guard let converter = AVAudioConverter(from: sourceFormat, to: fileFormat) else {
            logError("Could not create system audio converter from \(sourceFormat)")
            return
        }
        guard let convertedBuffer = AVAudioPCMBuffer(pcmFormat: fileFormat, frameCapacity: frameCount + 16) else { return }

        var convError: NSError?
        let convStatus = converter.convert(to: convertedBuffer, error: &convError) { _, outStatus in
            outStatus.pointee = .haveData
            return sourcePcmBuffer
        }

        if convStatus == .error {
            logError("System audio conversion failed: \(convError?.localizedDescription ?? "unknown")")
            return
        }

        systemWriteQueue.async { [weak self] in
            guard let self = self, let file = self.systemFile, self.isRecording else { return }
            do {
                try file.write(from: convertedBuffer)
                self.systemAudioFrameCount += Int64(convertedBuffer.frameLength)
            } catch {
                logError("System audio write failed: \(error.localizedDescription)")
            }
        }
    }

    func stopRecording() async {
        isRecording = false
        try? await stream?.stopCapture()
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()

        micWriteQueue.sync {}
        systemWriteQueue.sync {}

        // release file references to force AVAudioFile to finalize WAV headers
        // before the process exits (exit() skips deinitializers otherwise)
        micFile = nil
        systemFile = nil

        logError("Total frames written -- mic: \(micFrameCount), system audio: \(systemAudioFrameCount)")
        print("{\"status\":\"stopped\",\"micFile\":\"\(micURL.path)\",\"systemFile\":\"\(systemURL.path)\",\"micFrames\":\(micFrameCount),\"systemFrames\":\(systemAudioFrameCount)}")
        fflush(stdout)

        try? await Task.sleep(nanoseconds: 300_000_000)
        exit(0)
    }
}

let args = CommandLine.arguments

if args.count >= 3 && args[1] == "start" {
    let basePath = args[2]
    let recorder = MeetingRecorder(basePath: basePath)
    Task {
        await recorder.start()
    }
    RunLoop.main.run()
} else if args.count >= 2 && args[1] == "stop" {
    try? "STOP".write(toFile: CONTROL_FILE, atomically: true, encoding: .utf8)
    print("{\"status\":\"stop signal sent\"}")
} else {
    print("{\"error\":\"usage: audio_helper start <output_basepath> | audio_helper stop\"}")
}

import Foundation
import ScreenCaptureKit
import AVFoundation

// Records ONLY system audio via ScreenCaptureKit.
// Writes raw PCM samples directly to disk ourselves and constructs the WAV header
// manually -- see mic_helper.swift for why (AVAudioFile's automatic finalization
// proved unreliable in this recording context).
//
// Usage:
//   system_audio_helper start /path/to/output.wav
//   system_audio_helper stop

let CONTROL_FILE = "/tmp/mira_system_control"
let writeQueue = DispatchQueue(label: "com.mira.systemwrite")

func logError(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

func writeWAVHeaderPlaceholder(to handle: FileHandle, sampleRate: UInt32, channels: UInt16, bitsPerSample: UInt16) {
    var header = Data()
    let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample / 8)
    let blockAlign = channels * (bitsPerSample / 8)

    header.append(contentsOf: "RIFF".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(0).littleEndian) { Array($0) })
    header.append(contentsOf: "WAVE".utf8)
    header.append(contentsOf: "fmt ".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: UInt16(3).littleEndian) { Array($0) }) // IEEE float
    header.append(contentsOf: withUnsafeBytes(of: channels.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: sampleRate.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: byteRate.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: blockAlign.littleEndian) { Array($0) })
    header.append(contentsOf: withUnsafeBytes(of: bitsPerSample.littleEndian) { Array($0) })
    header.append(contentsOf: "data".utf8)
    header.append(contentsOf: withUnsafeBytes(of: UInt32(0).littleEndian) { Array($0) })

    handle.write(header)
}

func patchWAVHeader(at url: URL, totalPCMBytes: UInt32) {
    guard let handle = try? FileHandle(forUpdating: url) else {
        logError("Could not open file to patch WAV header")
        return
    }
    defer { try? handle.close() }

    let riffChunkSize = 36 + totalPCMBytes
    handle.seek(toFileOffset: 4)
    handle.write(withUnsafeBytes(of: riffChunkSize.littleEndian) { Data($0) })
    handle.seek(toFileOffset: 40)
    handle.write(withUnsafeBytes(of: totalPCMBytes.littleEndian) { Data($0) })
}

// fixed output format: 48kHz, stereo, Float32 -- all incoming system audio is
// converted to this before writing, regardless of its native format
let outputSampleRate: Double = 48000
let outputChannels: AVAudioChannelCount = 2
let fileFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outputSampleRate, channels: outputChannels, interleaved: true)!

class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    var fileHandle: FileHandle?
    let outputURL: URL
    var isRecording = false
    var totalBytesWritten: UInt32 = 0

    init(outputPath: String) {
        self.outputURL = URL(fileURLWithPath: outputPath)
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
            config.sampleRate = Int(outputSampleRate)
            config.channelCount = Int(outputChannels)
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            FileManager.default.createFile(atPath: outputURL.path, contents: nil)
            guard let handle = try? FileHandle(forWritingTo: outputURL) else {
                print("{\"error\":\"failed to open output file for writing\"}")
                exit(1)
            }
            fileHandle = handle
            writeWAVHeaderPlaceholder(to: handle, sampleRate: UInt32(outputSampleRate), channels: UInt16(outputChannels), bitsPerSample: 32)

            stream = SCStream(filter: filter, configuration: config, delegate: self)
            try stream?.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio.capture.queue"))
            try await stream?.startCapture()

            isRecording = true
            print("{\"status\":\"recording\",\"file\":\"\(outputPath())\"}")
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

        let frameCountLocal = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCountLocal > 0 else { return }

        let audioBufferList = UnsafeMutableAudioBufferListPointer(audioBufferListPtr)
        guard let sourcePcmBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: frameCountLocal) else { return }
        sourcePcmBuffer.frameLength = frameCountLocal

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
        guard let convertedBuffer = AVAudioPCMBuffer(pcmFormat: fileFormat, frameCapacity: frameCountLocal + 16) else { return }

        var convError: NSError?
        let convStatus = converter.convert(to: convertedBuffer, error: &convError) { _, outStatus in
            outStatus.pointee = .haveData
            return sourcePcmBuffer
        }

        if convStatus == .error {
            logError("System audio conversion failed: \(convError?.localizedDescription ?? "unknown")")
            return
        }

        // fileFormat is interleaved, so channel 0's buffer already holds the
        // interleaved frames -- write it directly as raw bytes
        guard let channelData = convertedBuffer.floatChannelData else { return }
        let frameLength = Int(convertedBuffer.frameLength)
        let channelCount = Int(fileFormat.channelCount)
        let byteCount = frameLength * channelCount * MemoryLayout<Float32>.size
        let data = Data(bytes: channelData[0], count: byteCount)

        writeQueue.async { [weak self] in
            guard let self = self, let handle = self.fileHandle, self.isRecording else { return }
            handle.write(data)
            self.totalBytesWritten += UInt32(byteCount)
        }
    }

    func stopRecording() async {
        isRecording = false
        try? await stream?.stopCapture()

        writeQueue.sync {}

        try? fileHandle?.close()
        patchWAVHeader(at: outputURL, totalPCMBytes: totalBytesWritten)

        logError("Total bytes written: \(totalBytesWritten)")
        print("{\"status\":\"stopped\",\"file\":\"\(outputPath())\",\"bytes\":\(totalBytesWritten)}")
        fflush(stdout)
        exit(0)
    }

    func outputPath() -> String {
        return outputURL.path
    }
}

let args = CommandLine.arguments

if args.count >= 3 && args[1] == "start" {
    let outputPath = args[2]
    let recorder = SystemAudioRecorder(outputPath: outputPath)
    Task {
        await recorder.start()
    }
    RunLoop.main.run()
} else if args.count >= 2 && args[1] == "stop" {
    try? "STOP".write(toFile: CONTROL_FILE, atomically: true, encoding: .utf8)
    print("{\"status\":\"stop signal sent\"}")
} else {
    print("{\"error\":\"usage: system_audio_helper start <output.wav> | system_audio_helper stop\"}")
}

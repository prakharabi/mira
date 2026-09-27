import Foundation
import Vision
import AppKit

// `ocr_helper <image>` prints the recognized text, one line per observation
// (the Control+Q OCR flow). `ocr_helper --json <image>` instead prints each
// line with where it sits in the image -- normalized 0..1, origin top-left --
// which is what lets "point at the Export button" land on the button's exact
// position instead of a vision model's approximate guess.
let args = Array(CommandLine.arguments.dropFirst())
let jsonMode = args.contains("--json")
guard let imagePath = args.first(where: { $0 != "--json" }) else {
    FileHandle.standardError.write("Usage: ocr_helper [--json] <image_path>\n".data(using: .utf8)!)
    exit(1)
}
guard let nsImage = NSImage(contentsOfFile: imagePath),
      let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("Could not load image at \(imagePath)\n".data(using: .utf8)!)
    exit(1)
}

var recognizedText = ""
let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        FileHandle.standardError.write("OCR error: \(error)\n".data(using: .utf8)!)
        return
    }
    guard let observations = request.results as? [VNRecognizedTextObservation] else { return }
    if jsonMode {
        // Vision's boundingBox is normalized with a bottom-left origin; flip y
        // so callers can map straight onto screen coordinates.
        let items: [[String: Any]] = observations.compactMap { obs in
            guard let text = obs.topCandidates(1).first?.string else { return nil }
            let b = obs.boundingBox
            return ["text": text, "x": b.minX, "y": 1 - b.maxY, "w": b.width, "h": b.height]
        }
        let data = (try? JSONSerialization.data(withJSONObject: items)) ?? Data("[]".utf8)
        recognizedText = String(data: data, encoding: .utf8) ?? "[]"
        return
    }
    let lines = observations.compactMap { $0.topCandidates(1).first?.string }
    recognizedText = lines.joined(separator: "\n")
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("OCR failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}

print(recognizedText)

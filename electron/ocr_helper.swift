import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("Usage: ocr_helper <image_path>\n".data(using: .utf8)!)
    exit(1)
}

let imagePath = CommandLine.arguments[1]
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

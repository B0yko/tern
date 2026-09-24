// vision-ocr — Apple Vision OCR sidecar for Tern.
//
// Reads images, runs VNRecognizeTextRequest, emits one JSON object per image
// on stdout (JSONL). Called from tern/vision.py::VisionOCR.
//
// Build:
//   swiftc -O -o bin/vision-ocr bin/vision-ocr.swift
//
// Usage:
//   vision-ocr --batch [--mode fast|accurate]    (reads paths from stdin, one per line)
//   vision-ocr --image /path/to/img.jpg [--mode fast|accurate]
//
// Output, one line per input image:
//   {"path":"...","text":"all blocks\njoined","blocks":[...],"elapsed_ms":486}
//   {"path":"/abs/path.jpg","error":"could not load image"}
//
// Each block: {"text":"...","confidence":0.94,"bbox":[x,y,w,h]}
//
// bbox is normalized (0...1) with origin at the TOP-LEFT of the image. Vision
// itself reports bottom-left origin, so we flip Y on the way out (`y' = 1 - y
// - h`) to match the convention every consumer of this JSON expects — image
// crops, HTML overlays, and Core Graphics draws all count from the top.
//
// Exit codes: 0 on success (including per-image errors, which are reported
// in-band so one bad frame doesn't kill a batch), 1 on usage error.

import Foundation
import Vision
import CoreGraphics
import ImageIO

enum OCRMode: String {
    case fast
    case accurate

    var recognitionLevel: VNRequestTextRecognitionLevel {
        switch self {
        case .fast: return .fast
        case .accurate: return .accurate
        }
    }

    /// Language correction costs ~15% throughput but fixes the diacritic noise
    /// that "accurate" mode is chosen for in the first place (Y Combìnator →
    /// Y Combinator). Off in fast mode, where throughput is the point.
    var usesLanguageCorrection: Bool {
        switch self {
        case .fast: return false
        case .accurate: return true
        }
    }
}

/// Languages Vision is asked to consider. Vision picks per-block; listing more
/// costs little. Cyrillic / CJK / Hangul coverage is what lets the index stay
/// honest about the "25+ scripts" claim.
let recognitionLanguages = [
    "en-US", "de-DE", "fr-FR", "es-ES", "it-IT", "pt-BR", "nl-NL",
    "ru-RU", "uk-UA",
    "zh-Hans", "zh-Hant", "ja-JP", "ko-KR",
]

func usage() -> String {
    return """
    Usage:
      vision-ocr --batch [--mode fast|accurate]    (reads paths from stdin, one per line)
      vision-ocr --image /path/to/img.jpg [--mode fast|accurate]
    """
}

/// Emit a single JSONL record. Uses JSONSerialization so text containing
/// quotes, newlines, or non-ASCII is escaped correctly — hand-rolled string
/// interpolation here would produce malformed JSON on the first slide that
/// contains a `"`.
func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: []),
          let line = String(data: data, encoding: .utf8) else {
        return
    }
    print(line)
}

func loadCGImage(at path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          CGImageSourceGetCount(source) > 0,
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        return nil
    }
    return image
}

func ocr(path: String, mode: OCRMode) {
    let started = Date()
    guard let image = loadCGImage(at: path) else {
        emit(["path": path, "error": "could not load image"])
        return
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = mode.recognitionLevel
    request.usesLanguageCorrection = mode.usesLanguageCorrection
    request.recognitionLanguages = recognitionLanguages

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        emit(["path": path, "error": "vision request failed: \(error.localizedDescription)"])
        return
    }

    var blocks: [[String: Any]] = []
    var lines: [String] = []
    for observation in (request.results ?? []) {
        // topCandidates(1) is what carries the confidence we filter on;
        // observation.confidence is the detection confidence, not the
        // transcription confidence, and they diverge on low-contrast slides.
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        lines.append(candidate.string)
        blocks.append([
            "text": candidate.string,
            "confidence": Double(candidate.confidence),
            "bbox": [
                Double(box.origin.x),
                // Flip to top-left origin. See the bbox note in the header.
                Double(1.0 - box.origin.y - box.size.height),
                Double(box.size.width),
                Double(box.size.height),
            ],
        ])
    }

    emit([
        "path": path,
        "text": lines.joined(separator: "\n"),
        "blocks": blocks,
        "elapsed_ms": Int(Date().timeIntervalSince(started) * 1000),
    ])
}

// ---- argument parsing ----

var args = Array(CommandLine.arguments.dropFirst())

if args.contains("--help") || args.contains("-h") {
    print(usage())
    exit(0)
}

var mode: OCRMode = .fast
if let i = args.firstIndex(of: "--mode") {
    guard i + 1 < args.count, let parsed = OCRMode(rawValue: args[i + 1]) else {
        FileHandle.standardError.write("Error: --mode expects 'fast' or 'accurate'.\n".data(using: .utf8)!)
        exit(1)
    }
    mode = parsed
    args.removeSubrange(i...(i + 1))
}

if args.contains("--batch") {
    // One absolute path per line on stdin. Blank lines are skipped so a
    // trailing newline from the caller doesn't produce a bogus error record.
    while let line = readLine(strippingNewline: true) {
        let path = line.trimmingCharacters(in: .whitespaces)
        if path.isEmpty { continue }
        ocr(path: path, mode: mode)
    }
    exit(0)
}

if let i = args.firstIndex(of: "--image") {
    guard i + 1 < args.count else {
        FileHandle.standardError.write("Error: --image expects a path.\n".data(using: .utf8)!)
        exit(1)
    }
    ocr(path: args[i + 1], mode: mode)
    exit(0)
}

FileHandle.standardError.write("Error: --image or --batch required. Use --help for usage.\n".data(using: .utf8)!)
exit(1)

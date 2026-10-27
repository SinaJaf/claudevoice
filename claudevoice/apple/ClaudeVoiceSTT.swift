// ClaudeVoiceSTT: transcribe one WAV file with Apple's speech recognizer (the engine
// behind macOS dictation), on-device when the Mac supports it.
//
// Usage: ClaudeVoiceSTT <output.json> <locale> <piece.wav>...
// Transcribes the pieces in order and writes {"text": "...", "on_device": true} or
// {"error": "..."} to output.json. Callers cut long takes into pieces under a minute
// at pauses: on long audio the recognizer hands back only its last stretch.
//
// It is a separate little app (not Python) because macOS only grants speech
// recognition to an app that declares NSSpeechRecognitionUsageDescription, and it
// is launched with `open` so the permission belongs to it, not to its parent.
import Foundation
import Speech

let args = CommandLine.arguments
guard args.count >= 4 else {
    FileHandle.standardError.write("usage: ClaudeVoiceSTT <output.json> <locale> <piece.wav>...\n".data(using: .utf8)!)
    exit(2)
}
let outputPath = args[1]
let localeId = args[2].isEmpty ? Locale.current.identifier : args[2]
let pieces = args[3...].map { URL(fileURLWithPath: $0) }

func finish(_ payload: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8)
    let tmp = outputPath + ".tmp"
    FileManager.default.createFile(atPath: tmp, contents: data)
    _ = try? FileManager.default.removeItem(atPath: outputPath)
    _ = try? FileManager.default.moveItem(atPath: tmp, toPath: outputPath)
    exit(0)
}

func pump(until done: () -> Bool, timeout: TimeInterval) {
    let deadline = Date().addingTimeInterval(timeout)
    while !done() && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
}

var status: SFSpeechRecognizerAuthorizationStatus? = nil
SFSpeechRecognizer.requestAuthorization { status = $0 }
pump(until: { status != nil }, timeout: 120)
guard status == .authorized else {
    finish(["error": "speech recognition is not allowed for ClaudeVoiceSTT; turn it on in System Settings, Privacy & Security, Speech Recognition"])
}
guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeId)), recognizer.isAvailable else {
    finish(["error": "Apple speech recognition is not available for \(localeId)"])
}

/// One piece: its text, "" when it holds no speech, or nil and an error.
///
/// The recognizer works utterance by utterance. While one is being spoken its results carry
/// no timing; when it ends, it is reported once with timing, and the next utterance starts
/// from an empty transcription (the earlier words are not in it). So each utterance is kept
/// when its timed result arrives; the final result repeats the last one and is skipped; an
/// utterance still untimed when the audio ends is kept as it stands.
func transcribe(_ url: URL) -> (String?, String?) {
    let request = SFSpeechURLRecognitionRequest(url: url)
    request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
    request.shouldReportPartialResults = true
    request.addsPunctuation = true
    var kept: [(start: TimeInterval, text: String)] = []
    var pending = ""
    var failure: String? = nil
    var settled = false
    let task = recognizer.recognitionTask(with: request) { result, error in
        if let result = result {
            let transcription = result.bestTranscription
            let text = transcription.formattedString
            let end = transcription.segments.last.map { $0.timestamp + $0.duration } ?? 0
            if end > 0 {
                let start = transcription.segments.first?.timestamp ?? 0
                let repeated = kept.contains { abs($0.start - start) < 0.05 }
                if !repeated && !text.isEmpty { kept.append((start, text)) }
                pending = ""
            } else {
                pending = text
            }
            if result.isFinal { settled = true }
        }
        if let error = error as NSError? {
            // "No speech detected" is an empty piece, not a failure.
            let noSpeech = error.domain == "kAFAssistantErrorDomain" && (error.code == 1110 || error.code == 1101)
            if !noSpeech && kept.isEmpty && pending.isEmpty {
                failure = error.localizedDescription
            }
            settled = true
        }
    }
    pump(until: { settled }, timeout: 300)
    _ = task
    if let failure = failure { return (nil, failure) }
    var parts = kept.sorted { $0.start < $1.start }.map { $0.text }
    if !pending.isEmpty { parts.append(pending) }
    // A pause between utterances usually ends a sentence; mark it where the recognizer did not.
    let joined = parts.enumerated().map { index, part -> String in
        guard index < parts.count - 1, let last = part.last, !".?!,;:".contains(last) else { return part }
        return part + "."
    }
    return (joined.joined(separator: " "), nil)
}

var parts: [String] = []
for piece in pieces {
    let (text, failure) = transcribe(piece)
    if let failure = failure { finish(["error": failure]) }
    if let text = text, !text.isEmpty { parts.append(text) }
}
finish(["text": parts.joined(separator: " "), "on_device": recognizer.supportsOnDeviceRecognition])

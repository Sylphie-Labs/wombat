//
//  PushToTalkRecorder.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  Push-to-activate capture ONLY: audio is captured while, and only while, a hold gesture
//  is active. There is no background listening mode anywhere in this file or this app — the
//  mic opens on beginHold() and closes on endHold(), nothing else starts it.
//
//  The recording indicator (PushToTalkButtonView) is not a second flag that could drift from
//  the engine's own state: a caller reads `activeHold != nil` for BOTH "should the indicator
//  show" and "is the mic live", because they are the same property. There is structurally no
//  way to show the indicator without the engine running, or hide it while the engine is
//  running.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import AVFoundation
import Foundation

/// One completed press-and-release capture, ready to hand to VoiceUploadClient.
public struct CapturedClip {
    public let audioData: Data
    /// The moment the hold began, i.e. the moment audio started being captured. This is the
    /// value TalkSessionController formats into the X-Wombat-Captured-At header (§2).
    public let startedAt: Date
}

/// Hold-to-talk capture. One instance owns at most one in-flight hold at a time.
public final class PushToTalkRecorder: ObservableObject {

    /// Non-nil for EXACTLY the lifetime of a hold — see the file header. This is the only
    /// state this type exposes for "am I recording right now".
    @Published public private(set) var activeHold: ActiveHold?

    public struct ActiveHold {
        public let startedAt: Date
    }

    public enum StartFailure: Error {
        case fileCreationFailed
    }

    private let engine = AVAudioEngine()
    private var outputFile: AVAudioFile?
    private var tempURL: URL?

    public init() {}

    /// Opens the mic and starts writing. Call ONLY on gesture-down of the hold control.
    public func beginHold() throws {
        guard activeHold == nil else { return }

        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.record, mode: .measurement)
        try audioSession.setActive(true)

        let inputNode = engine.inputNode
        // The mic's own native format — never a chosen or typed-in rate. What this capture
        // is encoded at is independent of the playback path's sample-rate discipline; this
        // keeps that concern out of this file entirely.
        let format = inputNode.outputFormat(forBus: 0)

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("wav")
        guard let file = try? AVAudioFile(forWriting: url, settings: format.settings) else {
            throw StartFailure.fileCreationFailed
        }
        outputFile = file
        tempURL = url

        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            try? self?.outputFile?.write(from: buffer)
        }

        try engine.start()
        activeHold = ActiveHold(startedAt: Date())
    }

    /// Closes the mic and returns the captured clip. Call ONLY on gesture-up (release,
    /// including a release that slides off the control — see PushToTalkButtonView).
    public func endHold() -> CapturedClip? {
        guard let hold = activeHold, let url = tempURL else { return nil }

        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        outputFile = nil
        activeHold = nil
        tempURL = nil

        let data = (try? Data(contentsOf: url)) ?? Data()
        try? FileManager.default.removeItem(at: url)

        return CapturedClip(audioData: data, startedAt: hold.startedAt)
    }
}

//
//  WatchPushToTalkRecorder.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  Push-to-activate capture ONLY: audio is captured while, and only while, a hold gesture
//  is active. There is no listening mode that runs without a held gesture, and no keyword
//  or voice trigger anywhere in this file or this app — the mic opens on beginHold() and
//  closes on endHold(), nothing else starts it.
//
//  Uses AVAudioRecorder (not AVAudioEngine) — watchOS capture goes through the recorder
//  API, unlike the phone's tap-based PushToTalkRecorder (ios/WombatCompanion, out of this
//  ticket's scope). Recording is written straight to a WAV file on disk, which conveniently
//  serves BOTH consumers of a captured clip without a second copy: WatchVoiceUploadClient
//  reads the file's Data for the direct POST body, and WatchRelaySession hands the same
//  file URL straight to WCSession's transferFile for the relay fallback.
//
//  The recording indicator (WatchPushToTalkButtonView) is not a second flag that could
//  drift from the engine's own state: a caller reads `activeHold != nil` for BOTH "should
//  the indicator show" and "is the mic live", because they are the same property. There is
//  structurally no way to show the indicator without the engine running, or hide it while
//  the engine is running.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import AVFoundation
import Foundation

/// One completed press-and-release capture, ready to hand to WatchVoiceUploadClient
/// (direct) and/or WatchRelaySession (fallback).
public struct WatchCapturedClip {
    public let fileURL: URL
    /// The moment the hold began, i.e. the moment audio started being captured. This is the
    /// value WatchTalkSessionController formats into the X-Wombat-Captured-At header (§2)
    /// and the value the staleness check (§4's stale_audio_window_seconds) measures from.
    public let startedAt: Date
}

/// Hold-to-talk capture. One instance owns at most one in-flight hold at a time.
public final class WatchPushToTalkRecorder: ObservableObject {

    /// Non-nil for EXACTLY the lifetime of a hold — see the file header. This is the only
    /// state this type exposes for "am I recording right now".
    @Published public private(set) var activeHold: ActiveHold?

    public struct ActiveHold {
        public let startedAt: Date
    }

    public enum StartFailure: Error {
        case recorderCreationFailed
    }

    private var recorder: AVAudioRecorder?
    private var tempURL: URL?

    public init() {}

    /// Opens the mic and starts writing. Call ONLY on gesture-down of the hold control.
    public func beginHold() throws {
        guard activeHold == nil else { return }

        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.record, mode: .measurement)
        try audioSession.setActive(true)

        // Linear PCM into a .wav-extensioned file, matching WireContract.Voice.contentType
        // ("audio/wav") — the direct POST body needs no transcoding step between capture
        // and send.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("wav")
        guard let newRecorder = try? AVAudioRecorder(url: url, settings: settings) else {
            throw StartFailure.recorderCreationFailed
        }
        recorder = newRecorder
        tempURL = url

        newRecorder.record()
        activeHold = ActiveHold(startedAt: Date())
    }

    /// Closes the mic and returns the captured clip. Call ONLY on gesture-up (release,
    /// including a release that slides off the control — see WatchPushToTalkButtonView).
    public func endHold() -> WatchCapturedClip? {
        guard let hold = activeHold, let url = tempURL else { return nil }

        recorder?.stop()
        recorder = nil
        activeHold = nil
        tempURL = nil

        return WatchCapturedClip(fileURL: url, startedAt: hold.startedAt)
    }
}

//
//  TalkSessionController.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  Owns the "may I record right now" gate: a hold never opens the mic without a fresh
//  unreachable/unauthorized/ok read of the health handshake first (WireContract §0.1's
//  result trichotomy). Capturing audio wombat cannot receive is worse than not capturing it
//  at all, so an unreachable or unauthorized host refuses the hold before
//  PushToTalkRecorder.beginHold() is ever called.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

@MainActor
public final class TalkSessionController: ObservableObject {

    /// Why a hold was refused. Deliberately two cases, not a shared "offline" case — see
    /// the file header and WireContract §0.1.
    public enum Refusal {
        case unreachable
        case revoked

        public var message: String {
            switch self {
            case .unreachable:
                return "wombat is not reachable on the network right now"
            case .revoked:
                return "re-pair this device"
            }
        }
    }

    @Published public private(set) var refusal: Refusal?
    public let recorder: PushToTalkRecorder

    private let healthClient: HealthHandshakeClient
    private let voiceUploadClient: VoiceUploadClient

    /// Set once a health call returns `.unauthorized`. Per §0.1, a revoked device "stops
    /// retrying": while this is true, holdDown() refuses locally without a network call,
    /// until a fresh pairing clears it.
    private var isRevoked = false

    public init(
        recorder: PushToTalkRecorder,
        healthClient: HealthHandshakeClient,
        voiceUploadClient: VoiceUploadClient
    ) {
        self.recorder = recorder
        self.healthClient = healthClient
        self.voiceUploadClient = voiceUploadClient
    }

    /// Gesture-down. Async because the pre-flight health read must complete before the mic
    /// is allowed to open.
    public func holdDown() {
        Task { await beginHoldIfReachable() }
    }

    /// Gesture-up.
    public func holdUp() {
        Task { await endHoldAndUpload() }
    }

    private func beginHoldIfReachable() async {
        guard recorder.activeHold == nil else { return }

        if isRevoked {
            refusal = .revoked
            return
        }

        switch await healthClient.checkHealth() {
        case .unreachable:
            refusal = .unreachable
        case .unauthorized:
            isRevoked = true
            refusal = .revoked
        case .ok(let status, let value):
            guard status == 200, value != nil else {
                refusal = .unreachable
                return
            }
            refusal = nil
            try? recorder.beginHold()
        }
    }

    private func endHoldAndUpload() async {
        guard let clip = recorder.endHold() else { return }

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let capturedAtHeaderValue = formatter.string(from: clip.startedAt)

        switch await voiceUploadClient.upload(
            audioData: clip.audioData,
            capturedAtHeaderValue: capturedAtHeaderValue
        ) {
        case .unreachable:
            refusal = .unreachable
        case .unauthorized:
            isRevoked = true
            refusal = .revoked
        case .ok:
            refusal = nil
        }
    }
}

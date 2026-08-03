//
//  WatchTalkSessionController.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  THE ORDERING THAT MUST BE VISIBLE IN THE SOURCE (the research finding this whole ticket
//  exists to express): every send tries the DIRECT POST (WatchVoiceUploadClient, over the
//  watch's own Wi-Fi) FIRST. The WatchConnectivity relay (WatchRelaySession, through the
//  phone) is reached ONLY when that direct attempt has already failed. Every Apple Watch
//  model has Wi-Fi and can join networks the paired iPhone previously joined even with the
//  phone off — so watch-direct is the PRIMARY path and the phone relay is only the
//  FALLBACK. A draft that tried the relay first would pass every hardware test with the
//  phone in the room and fail the one test that matters: the phone absent. See
//  `attemptSend(clip:capturedAtHeaderValue:)` below — the direct call is the first
//  statement in that method's body and the relay call is reachable ONLY from inside the
//  fallback branches beneath it.
//
//  THE STALENESS CHECK: the refusal window is READ FROM WOMBAT — the
//  stale_audio_window_seconds field of WireContract.Health.Response (WireContract.Route.health,
//  §4), decoded through WireContract via WatchHealthClient. This type holds no numeric
//  window of its own. A clip found stale is refused with a message and NEITHER send path
//  is attempted — a stale clip must never be delivered late, whether by the fast path or
//  the slow one.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

@MainActor
public final class WatchTalkSessionController: ObservableObject {

    public enum Refusal {
        case unreachable
        case revoked
        case stale

        public var message: String {
            switch self {
            case .unreachable:
                return "wombat is not reachable on the network right now"
            case .revoked:
                return "re-pair this device"
            case .stale:
                return "that clip took too long to send and was not delivered"
            }
        }
    }

    @Published public private(set) var refusal: Refusal?
    public let recorder: WatchPushToTalkRecorder

    private let healthClient: WatchHealthClient
    private let voiceUploadClient: WatchVoiceUploadClient
    private let relaySession: WatchRelaySession

    public init(
        recorder: WatchPushToTalkRecorder,
        healthClient: WatchHealthClient,
        voiceUploadClient: WatchVoiceUploadClient,
        relaySession: WatchRelaySession
    ) {
        self.recorder = recorder
        self.healthClient = healthClient
        self.voiceUploadClient = voiceUploadClient
        self.relaySession = relaySession
    }

    /// Gesture-down. Push-to-activate only — this is the sole trigger for opening the mic,
    /// with no pre-flight network call gating it: a hold of any duration works regardless of
    /// which (if either) send path is currently reachable, because staleness and
    /// reachability are judged once, at release, against the clip actually captured.
    public func holdDown() {
        try? recorder.beginHold()
    }

    /// Gesture-up.
    public func holdUp() {
        Task { await endHoldAndSend() }
    }

    private func endHoldAndSend() async {
        guard let clip = recorder.endHold() else { return }
        defer { try? FileManager.default.removeItem(at: clip.fileURL) }

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let capturedAtHeaderValue = formatter.string(from: clip.startedAt)

        if await isAlreadyStale(startedAt: clip.startedAt) {
            refusal = .stale
            return
        }

        await attemptSend(clip: clip, capturedAtHeaderValue: capturedAtHeaderValue)
    }

    /// Reads the refusal window fresh from wombat's own health response (never a hardcoded
    /// constant) and compares it to how long ago the hold began. When the window cannot be
    /// learned right now (health unreachable, unauthorized, or an undecodable body), this
    /// returns `false` rather than guessing a window — the clip is not blocked here, and
    /// wombat's own server-side staleness check (§2) remains the backstop.
    private func isAlreadyStale(startedAt: Date) async -> Bool {
        guard case .ok(_, let value) = await healthClient.checkHealth(),
              let staleAudioWindowSeconds = value?.staleAudioWindowSeconds
        else {
            return false
        }

        let elapsedSeconds = Date().timeIntervalSince(startedAt)
        return elapsedSeconds > Double(staleAudioWindowSeconds)
    }

    /// PRIMARY, then SECONDARY — in that literal order. See the file header.
    private func attemptSend(clip: WatchCapturedClip, capturedAtHeaderValue: String) async {
        // PRIMARY: direct POST over the watch's own Wi-Fi, attempted FIRST, unconditionally.
        let directResult = await voiceUploadClient.upload(
            audioData: (try? Data(contentsOf: clip.fileURL)) ?? Data(),
            capturedAtHeaderValue: capturedAtHeaderValue
        )

        switch directResult {
        case .ok(let status, _) where (200..<300).contains(status):
            // The primary path succeeded — the relay below is never reached.
            refusal = nil
            return
        case .unauthorized:
            // This watch's OWN token was revoked. The relay below still authenticates as
            // the PHONE (PhoneWatchSession relays through the phone's own token, not this
            // watch's — see ios/WombatCompanion/PhoneWatchSession.swift), so a revoked
            // watch token does not by itself doom a clip the phone can still deliver.
            refusal = .revoked
        default:
            // .unreachable, or an .ok with a non-2xx status: both count as "the direct
            // attempt failed" for the ordering this ticket exists to enforce.
            refusal = .unreachable
        }

        // SECONDARY / fallback: reached ONLY because the direct attempt above did not
        // succeed. Never called from anywhere else in this file.
        relaySession.relay(fileURL: clip.fileURL, capturedAtHeaderValue: capturedAtHeaderValue)
    }
}

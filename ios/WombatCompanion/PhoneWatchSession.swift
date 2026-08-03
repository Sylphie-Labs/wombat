//
//  PhoneWatchSession.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  The phone side of the WatchConnectivity session. Two jobs, both one-directional:
//
//  1. SEND (one-shot, at setup): once the session activates, and the watch's own,
//     separately-minted token is present locally (staged into KeychainStore under the
//     `.watch` account by the pairing flow, via the same PairingQRParser the phone uses for
//     its own token — see ios/Shared/PairingQRParser.swift), hand it to the watch exactly
//     once. The phone's OWN token (`.phone`) is never sent here — the watch must speak with
//     its own identity, never the phone's.
//  2. RECEIVE (relay): a clip the watch captured arrives as a file transfer carrying the
//     watch's ORIGINAL capture timestamp in its metadata. That stamp is read once into
//     `capturedAtHeaderValue` and forwarded to VoiceUploadClient unchanged — never
//     reassigned to "now" on receipt — so a slow relay cannot launder a stale clip into a
//     fresh one (wombat's own staleness check, §2, judges the ORIGINAL capture time).
//
//  This file owns no route, header or payload shape of its own: the relayed upload goes
//  through the exact same VoiceUploadClient the phone's own capture uses.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import WatchConnectivity

public final class PhoneWatchSession: NSObject {
    /// Metadata key the watch-side transfer attaches its original capture stamp under.
    /// Internal to the phone<->watch handoff only — NOT part of WireContract, which covers
    /// only the phone/watch <-> wombat wire.
    public static let capturedAtMetadataKey = "wombat.relay.captured_at"

    private let voiceUploadClient: VoiceUploadClient
    private let session: WCSession
    private var hasSentWatchToken = false

    public init(voiceUploadClient: VoiceUploadClient, session: WCSession = .default) {
        self.voiceUploadClient = voiceUploadClient
        self.session = session
        super.init()
    }

    /// Call once at app setup. No-ops on a phone with no WatchConnectivity support.
    public func activate() {
        guard WCSession.isSupported() else { return }
        session.delegate = self
        session.activate()
    }

    private func sendWatchTokenIfNeeded() {
        guard !hasSentWatchToken else { return }
        guard session.activationState == .activated else { return }
        guard let watchToken = KeychainStore.loadToken(for: .watch) else { return }

        // One-shot: transferUserInfo is a queued, guaranteed-delivery handoff — it does not
        // require the watch to be reachable at this instant, but this call site fires
        // exactly once per activation, never on a repeating timer or a retry loop.
        session.transferUserInfo(["deviceToken": watchToken])
        hasSentWatchToken = true
    }
}

extension PhoneWatchSession: WCSessionDelegate {
    public func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        guard activationState == .activated else { return }
        sendWatchTokenIfNeeded()
    }

    public func sessionDidBecomeInactive(_ session: WCSession) {}

    public func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    /// The relay. `capturedAtHeaderValue` is read once from the transfer's metadata and
    /// handed straight to VoiceUploadClient — see the file header. It is a `let`, bound
    /// once, with no line between receipt and send that could reassign it.
    public func session(_ session: WCSession, didReceive file: WCSessionFile) {
        guard let capturedAtHeaderValue = file.metadata?[Self.capturedAtMetadataKey] as? String else {
            return
        }
        guard let audioData = try? Data(contentsOf: file.fileURL) else {
            return
        }

        Task {
            _ = await self.voiceUploadClient.upload(
                audioData: audioData,
                capturedAtHeaderValue: capturedAtHeaderValue
            )
        }
    }
}

//
//  WatchRelaySession.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  The watch side of the WatchConnectivity session. Two jobs, both the mirror image of
//  ios/WombatCompanion/PhoneWatchSession.swift (out of this ticket's scope — read, not
//  edited):
//
//  1. RECEIVE (one-shot, at setup): the phone sends this watch's own, separately-minted
//     token exactly once via `transferUserInfo(["deviceToken": ...])`. On receipt, this
//     type persists it into KeychainStore under the `.watch` account and never asks again.
//     After this point every send authenticates from that stored token — see
//     WatchConnection.deviceToken and WatchVoiceUploadClient — with NO further
//     WatchConnectivity round trip. The watch is autonomous by construction.
//  2. SEND (relay, SECONDARY / fallback only): a queued file transfer carrying the clip's
//     ORIGINAL capture timestamp in its metadata, reached ONLY when
//     WatchVoiceUploadClient's direct POST has already failed — see
//     WatchTalkSessionController, which owns that ordering. This type does not decide
//     when to relay; it only knows how.
//
//  This file owns no route, header or payload shape of the wombat wire itself — the relay
//  target is the phone, not wombat, and the phone forwards to wombat through its own
//  VoiceUploadClient (PhoneWatchSession.swift).
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import WatchConnectivity

public final class WatchRelaySession: NSObject {
    /// Metadata key the relay file transfer attaches the clip's original capture stamp
    /// under. MUST match PhoneWatchSession.capturedAtMetadataKey
    /// ("wombat.relay.captured_at") byte-for-byte — that file is out of this ticket's
    /// scope and lives in a different synchronized target group, so the two sides share
    /// this string by convention, not by importing a common symbol. This is internal to
    /// the watch<->phone handoff only, NOT part of WireContract, which covers only the
    /// phone/watch <-> wombat wire.
    public static let capturedAtMetadataKey = "wombat.relay.captured_at"

    private let session: WCSession

    public init(session: WCSession = .default) {
        self.session = session
        super.init()
    }

    /// Call once at app setup. No-ops on a watch with no WatchConnectivity support.
    public func activate() {
        guard WCSession.isSupported() else { return }
        session.delegate = self
        session.activate()
    }

    /// The SECONDARY / fallback send. Callers (WatchTalkSessionController) call this ONLY
    /// after the direct POST (WatchVoiceUploadClient) has already failed — this type makes
    /// no attempt of its own to be tried first. `capturedAtHeaderValue` is the SAME
    /// already-formatted, already-captured stamp the direct path would have sent, forwarded
    /// unchanged so the phone relays the ORIGINAL capture time rather than the relay time.
    public func relay(fileURL: URL, capturedAtHeaderValue: String) {
        guard session.activationState == .activated else { return }
        session.transferFile(fileURL, metadata: [Self.capturedAtMetadataKey: capturedAtHeaderValue])
    }
}

extension WatchRelaySession: WCSessionDelegate {
    public func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        // No action needed here: the token arrives via didReceiveUserInfo below, on the
        // phone's own schedule (its one-shot send fires once ITS session activates), not
        // on a request this side makes. `activationState`/`error` are intentionally unread
        // — see the file header for what this method's job is NOT.
        _ = activationState
        _ = error
    }

    /// Receives the phone's one-shot `transferUserInfo(["deviceToken": ...])` send (see
    /// PhoneWatchSession.sendWatchTokenIfNeeded) and persists it as this watch's OWN token.
    /// No reply, no acknowledgement call back into the phone — this is the entire receive
    /// side of the handoff.
    public func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        guard let token = userInfo["deviceToken"] as? String else { return }
        try? KeychainStore.saveToken(token, for: .watch)
    }
}

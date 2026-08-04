//
//  WatchPlaybackStateView.swift
//  ios/WombatCompanion Watch App
//
//  TK-360 — DRAFT SOURCE (DEC-82 tier A).
//
//  Renders WatchUtterancePlaybackController.state directly — every case below is a REAL,
//  worded UI state, fully implemented, never a stub. The charging dead-state in particular is
//  the whole reason this ticket exists: PTT works, wombat replies, and this is the screen
//  that has to say nothing is coming out of the speaker right now, rather than staying
//  silent about it.
//
//  `.unreachableRetrying` and `.revoked` are two DISTINCT states with two distinct
//  messages — a revoked watch never shows "not reachable" and never spins forever behind
//  it. Neither branch, nor anything else in this file, names a fallback to any other
//  device's speaker: when the watch cannot play, the watch says so.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import SwiftUI

public struct WatchPlaybackStateView: View {
    @ObservedObject private var controller: WatchUtterancePlaybackController

    public init(controller: WatchUtterancePlaybackController) {
        self.controller = controller
    }

    public var body: some View {
        switch controller.state {
        case .idle:
            EmptyView()

        case .waitingForReply:
            Text("Waiting for wombat's reply…")

        case .unreachableRetrying:
            // Transient — retried automatically by the controller within the TTL. Never
            // presented as the same thing as a revoked token.
            Text("wombat is not reachable on the network right now — retrying")

        case .revoked:
            // Terminal for this turn — no further automatic retry.
            Text("This watch's token was revoked. Re-pair this watch.")

        case .chargingBlocked(let sourceDescription):
            VStack(spacing: 4) {
                Text(sourceDescription)
                Text("Speaker playback is not available while the watch is charging.")
                    .font(.footnote)
            }

        case .playing(let sourceDescription):
            VStack(spacing: 4) {
                Text(sourceDescription)
                Text(WatchUtterancePlaybackController.batteryCostNotice)
                    .font(.footnote)
            }
        }
    }
}

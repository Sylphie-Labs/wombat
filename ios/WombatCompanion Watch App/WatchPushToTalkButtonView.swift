//
//  WatchPushToTalkButtonView.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  The only place a user's press-and-hold gesture turns into holdDown()/holdUp() calls on
//  the watch. The recording indicator is driven directly off recorder.activeHold (see
//  WatchPushToTalkRecorder's header) — there is no separate "isShowingIndicator" flag
//  anywhere in this view, so the indicator cannot be shown without a live hold or hidden
//  during one, for the hold's entire length.
//
//  Push-to-activate only: the gesture below is the SOLE trigger for holdDown()/holdUp() in
//  this file. Nothing here starts a hold on its own — no keyword or voice trigger, and no
//  listening mode that runs without a held gesture.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import SwiftUI

public struct WatchPushToTalkButtonView: View {
    @ObservedObject private var controller: WatchTalkSessionController

    public init(controller: WatchTalkSessionController) {
        self.controller = controller
    }

    public var body: some View {
        VStack(spacing: 8) {
            // Bound to the same property WatchPushToTalkRecorder uses internally to know
            // the mic is live — see its file header. Cannot show without recording, cannot
            // hide while recording, for the hold's entire length.
            if controller.recorder.activeHold != nil {
                Text("Recording…")
            }

            if let refusal = controller.refusal {
                Text(refusal.message)
            }

            Circle()
                .frame(width: 64, height: 64)
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { _ in controller.holdDown() }
                        .onEnded { _ in controller.holdUp() }
                )
        }
    }
}

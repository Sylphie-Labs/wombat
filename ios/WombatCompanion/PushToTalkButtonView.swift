//
//  PushToTalkButtonView.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  The only place a user's press-and-hold gesture turns into holdDown()/holdUp() calls. The
//  recording indicator is driven directly off recorder.activeHold (see PushToTalkRecorder's
//  header) — there is no separate "isShowingIndicator" flag anywhere in this view, so the
//  indicator cannot be shown without a live hold or hidden during one.
//
//  Push-to-activate only: the gesture below is the SOLE trigger for holdDown()/holdUp() in
//  this file. Nothing here starts a hold on its own.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import SwiftUI

public struct PushToTalkButtonView: View {
    @ObservedObject private var controller: TalkSessionController

    public init(controller: TalkSessionController) {
        self.controller = controller
    }

    public var body: some View {
        VStack(spacing: 12) {
            // Bound to the same property PushToTalkRecorder uses internally to know the
            // engine is live — see its file header. Cannot show without recording, cannot
            // hide while recording.
            if controller.recorder.activeHold != nil {
                Text("Recording…")
            }

            if let refusal = controller.refusal {
                Text(refusal.message)
            }

            Circle()
                .frame(width: 88, height: 88)
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { _ in controller.holdDown() }
                        .onEnded { _ in controller.holdUp() }
                )
        }
    }
}

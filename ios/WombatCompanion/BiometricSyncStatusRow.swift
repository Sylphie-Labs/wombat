//
//  BiometricSyncStatusRow.swift
//  ios/WombatCompanion
//
//  TK-357 — DRAFT SOURCE (DEC-82 tier A).
//
//  The ONE sync-status row TK-357's brief allows — no new screen. Reads
//  BiometricBackgroundSyncCoordinator.status and renders every case, including a stalled
//  drain, as a REAL, visible row rather than silence.
//
//  The caption is deliberately non-committal about cadence: HealthKit's own background-
//  delivery ceiling is roughly hourly at best, and iOS further coalesces and defers
//  background launches under its own scheduler on top of that, so this copy never implies
//  anything tighter than "roughly hourly" — see BiometricBackgroundSyncCoordinator's header
//  for why that ceiling is real rather than a rounding choice.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import SwiftUI

public struct BiometricSyncStatusRow: View {
    @ObservedObject private var coordinator: BiometricBackgroundSyncCoordinator

    public init(coordinator: BiometricBackgroundSyncCoordinator) {
        self.coordinator = coordinator
    }

    public var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Background sync")
                Text("Updates roughly hourly while the app is backgrounded, at the platform's own pace — never sooner.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            statusBadge
        }
    }

    @ViewBuilder
    private var statusBadge: some View {
        switch coordinator.status {
        case .idle:
            Text("Idle")
                .foregroundStyle(.secondary)
        case .synced(let at):
            Text("Synced \(at.formatted(date: .omitted, time: .shortened))")
        case .stalled(let reason):
            // The visible stalled state TK-357 AC4 requires: a background drain that
            // isn't landing shows up here as a named reason, not as an unchanged "Idle".
            Text("Stalled — \(reason)")
                .foregroundStyle(.red)
        }
    }
}

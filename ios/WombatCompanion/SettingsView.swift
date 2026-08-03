//
//  SettingsView.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Shows pairing state, the fixed set of requested HealthKit types
//  (HealthKitSampleTypes.all — display only; see that file's header for why "granted"
//  cannot be read back from HealthKit), and each type's last-sync status from
//  BiometricSyncEngine — including the possibly-revoked PROBLEM state, never collapsed
//  into a plain "synced" when it is actually stale.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import HealthKit
import SwiftUI

public struct SettingsView: View {
    @ObservedObject private var pairingCoordinator: PairingCoordinator
    @ObservedObject private var syncEngine: BiometricSyncEngine

    public init(pairingCoordinator: PairingCoordinator, syncEngine: BiometricSyncEngine) {
        self.pairingCoordinator = pairingCoordinator
        self.syncEngine = syncEngine
    }

    public var body: some View {
        List {
            Section("Pairing") {
                pairingRow
            }
            Section("HealthKit types") {
                ForEach(HealthKitSampleTypes.all, id: \.identifier) { type in
                    HStack {
                        Text(displayName(for: type))
                        Spacer()
                        syncStatusText(for: type)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var pairingRow: some View {
        switch pairingCoordinator.state {
        case .notPaired:
            Text("Not paired")
        case .probing:
            Text("Checking connection…")
        case .paired(let host, let port, let name):
            Text("Paired to \(name) (\(host):\(port))")
        case .failed(let message):
            Text(message)
        }
    }

    /// Local UI label ONLY — never sent on any request; nothing here touches WireContract.
    private func displayName(for type: HKSampleType) -> String {
        switch type {
        case HealthKitSampleTypes.sleepAnalysis: return "Sleep"
        case HealthKitSampleTypes.workout: return "Workouts"
        case HealthKitSampleTypes.restingHeartRate: return "Resting heart rate"
        case HealthKitSampleTypes.heartRateVariabilitySDNN: return "Heart rate variability"
        case HealthKitSampleTypes.stepCount: return "Steps"
        default: return type.identifier
        }
    }

    @ViewBuilder
    private func syncStatusText(for type: HKSampleType) -> some View {
        let status = syncEngine.statusByType[type.identifier] ?? .neverSynced
        switch status {
        case .neverSynced:
            Text("Never synced")
        case .ok(let lastSuccessfulSampleAt):
            Text("Synced \(lastSuccessfulSampleAt.formatted(date: .abbreviated, time: .shortened))")
        case .possiblyRevoked(let lastSuccessfulSampleAt):
            if let lastSuccessfulSampleAt {
                let when = lastSuccessfulSampleAt.formatted(date: .abbreviated, time: .shortened)
                Text("Possibly revoked — last sample \(when)")
            } else {
                Text("Possibly revoked")
            }
        case .syncFailed(let reason):
            Text("Sync failed: \(reason)")
        }
    }
}

//
//  BiometricSyncEngine.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Runs one "Sync now" pass over every member of HealthKitSampleTypes.all — the single
//  list constant (TK-356 AC2); no HKAnchoredObjectQuery anywhere in this file names a type
//  outside it. Each type's samples are projected through BiometricSampleProjection (the
//  one pure function) and uploaded through BiometricsUploadClient (§3). The anchor for a
//  type advances ONLY after wombat has confirmed the batch it covers was accepted.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import HealthKit

/// Per-type status this engine can report. `.possiblyRevoked` and `.syncFailed` are both
/// PROBLEM states — see the criteria named on each case below and in `sync(type:)`.
public enum TypeSyncStatus {
    case neverSynced
    case ok(lastSuccessfulSampleAt: Date)
    /// TK-349's revoked-type criterion ("a granted type whose permission the operator then
    /// revokes ... the app surfaces last-successful-sample age and flags the type as
    /// possibly revoked rather than silently reporting success"). HealthKit returning
    /// empty-with-no-error must never be read as a quiet day on its own — see
    /// BiometricSyncEngine.evaluateQuietPeriod.
    case possiblyRevoked(lastSuccessfulSampleAt: Date?)
    /// TK-349's off-LAN/wombat-not-running criterion ("it fails visibly with a plain reason
    /// and retains its anchors — a failed sync never advances an anchor and never loses
    /// samples").
    case syncFailed(reason: String)
}

@MainActor
public final class BiometricSyncEngine: ObservableObject {
    private let healthStore: HKHealthStore
    private let uploadClient: BiometricsUploadClient

    /// Heuristic only — HealthKit discloses no authorization status
    /// (HealthKitAuthorizationManager's header explains why), so "possibly revoked" is
    /// inferred from staleness, never read from an API. This window is an app-side
    /// judgment call, not part of the locked wire contract.
    private let revocationInferenceWindow: TimeInterval

    @Published public private(set) var statusByType: [String: TypeSyncStatus] = [:]

    public init(
        healthStore: HKHealthStore = HKHealthStore(),
        uploadClient: BiometricsUploadClient,
        revocationInferenceWindow: TimeInterval = 7 * 24 * 3600
    ) {
        self.healthStore = healthStore
        self.uploadClient = uploadClient
        self.revocationInferenceWindow = revocationInferenceWindow
    }

    public func syncNow() async {
        for type in HealthKitSampleTypes.all {
            await sync(type: type)
        }
    }

    private func sync(type: HKSampleType) async {
        let anchor = BiometricAnchorStore.loadAnchor(for: type)
        let (samples, newAnchor) = await runAnchoredQuery(for: type, anchor: anchor)

        guard let newAnchor else {
            // TK-349's off-LAN/wombat-not-running criterion: the local HealthKit read
            // itself failed. BiometricAnchorStore.saveAnchor is never called on this path,
            // so the on-disk anchor is untouched and no sample this pass covered is lost.
            statusByType[type.identifier] = .syncFailed(reason: "could not read the HealthKit store")
            return
        }

        let projected = samples.compactMap { BiometricSampleProjection.project($0) }

        guard !projected.isEmpty else {
            evaluateQuietPeriod(for: type)
            return
        }

        switch await uploadClient.upload(samples: projected) {
        case .unreachable:
            // TK-349's off-LAN/wombat-not-running criterion, restated for the upload leg:
            // BiometricAnchorStore.saveAnchor is skipped on every branch below except the
            // confirmed-accept one, so a failed POST never advances the anchor and the
            // samples it covered are re-offered on the next pass.
            statusByType[type.identifier] = .syncFailed(reason: "wombat is not reachable on the network right now")
        case .unauthorized:
            statusByType[type.identifier] = .syncFailed(reason: "re-pair this device")
        case .ok(let status, let value):
            guard status == 200 || status == 202, value != nil else {
                statusByType[type.identifier] = .syncFailed(reason: "wombat rejected this batch")
                return
            }
            // Only NOW, after a confirmed accept, does the anchor advance.
            BiometricAnchorStore.saveAnchor(newAnchor, for: type)
            let now = Date()
            BiometricAnchorStore.recordSuccessfulSync(for: type, at: now)
            statusByType[type.identifier] = .ok(lastSuccessfulSampleAt: now)
        }
    }

    /// No new samples this pass is the ordinary case ("a quiet day") UNLESS
    /// last-successful-sample age has crossed `revocationInferenceWindow`, in which case it
    /// is surfaced as a PROBLEM rather than read as success. TK-349's revoked-type
    /// criterion exists to prevent exactly the silent-empty-is-fine reading.
    private func evaluateQuietPeriod(for type: HKSampleType) {
        guard let lastSuccess = BiometricAnchorStore.lastSuccessfulSampleAt(for: type) else {
            statusByType[type.identifier] = .neverSynced
            return
        }
        if Date().timeIntervalSince(lastSuccess) > revocationInferenceWindow {
            statusByType[type.identifier] = .possiblyRevoked(lastSuccessfulSampleAt: lastSuccess)
        } else {
            statusByType[type.identifier] = .ok(lastSuccessfulSampleAt: lastSuccess)
        }
    }

    /// Wraps HKAnchoredObjectQuery in async/await. Returns (samples, nil) on any HealthKit-
    /// side error so the caller treats it as a FAILED pass — never as "zero new samples".
    /// `type` is always a member of HealthKitSampleTypes.all, supplied by syncNow()'s loop.
    private func runAnchoredQuery(
        for type: HKSampleType,
        anchor: HKQueryAnchor?
    ) async -> ([HKSample], HKQueryAnchor?) {
        await withCheckedContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: type,
                predicate: nil,
                anchor: anchor,
                limit: HKObjectQueryNoLimit
            ) { _, samplesOrNil, _, resultAnchor, error in
                guard error == nil, let samplesOrNil else {
                    continuation.resume(returning: ([], nil))
                    return
                }
                continuation.resume(returning: (samplesOrNil, resultAnchor))
            }
            healthStore.execute(query)
        }
    }
}

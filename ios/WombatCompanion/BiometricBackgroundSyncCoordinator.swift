//
//  BiometricBackgroundSyncCoordinator.swift
//  ios/WombatCompanion
//
//  TK-357 — DRAFT SOURCE (DEC-82 tier A).
//
//  The background half of biometric sync. BiometricSyncEngine (TK-356) is the FOREGROUND
//  "Sync now" path; this file is what keeps samples flowing while the app is suspended or
//  not running at all.
//
//  THE WIRE IS THE SAME WIRE. Background delivery does not open a second route, a second
//  request-body shape or a second POST implementation — it drives BiometricsUploadClient.
//  upload(samples:), the EXACT call site BiometricSyncEngine already uses, just handed a
//  background-configured URLSession instead of `.shared`. There is one call site family
//  for POST /v1/biometrics in this tree (TK-357 AC1), not two.
//
//  THE PLATFORM CEILING IS HOURLY-ISH. HKObserverQuery plus enableBackgroundDelivery(
//  frequency: .hourly) is the most frequent background wake HealthKit offers for these
//  types, and iOS further coalesces and defers background launches under its own
//  scheduler at its own discretion on top of that. Nothing here, and nothing in
//  BiometricSyncStatusRow's copy, may claim a tighter cadence than that (TK-357 non-goal).
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import HealthKit

/// The overall background-path state TK-357's one sync-status row renders. A stalled
/// drain is a REAL, named case here — not silence, and not folded into `.idle`.
public enum BackgroundSyncStatus: Equatable {
    case idle
    case synced(at: Date)
    case stalled(reason: String)
}

@MainActor
public final class BiometricBackgroundSyncCoordinator: ObservableObject {
    private let healthStore: HKHealthStore
    private let uploadClient: BiometricsUploadClient
    private var observerQueries: [HKObserverQuery] = []

    /// Namespaced under the app's own bundle so a relaunch's URLSessionDelegate can
    /// reattach to an in-flight background transfer. That reattachment is standard
    /// background-session plumbing (an AppDelegate/SceneDelegate hook), not this
    /// coordinator's own concern, and is not re-described here.
    public static let backgroundSessionIdentifier = "com.wombat.companion.biometrics.background"

    @Published public private(set) var status: BackgroundSyncStatus = .idle

    public init(healthStore: HKHealthStore = HKHealthStore(), connection: WombatConnection) {
        self.healthStore = healthStore
        let configuration = URLSessionConfiguration.background(withIdentifier: Self.backgroundSessionIdentifier)
        configuration.sessionSendsLaunchEvents = true
        self.uploadClient = BiometricsUploadClient(
            connection: connection,
            session: URLSession(configuration: configuration)
        )
    }

    /// Registers ONE HKObserverQuery per member of HealthKitSampleTypes.all — the same one
    /// list HealthKitAuthorizationManager and BiometricSyncEngine already read — and asks
    /// HealthKit for background delivery on each. Call once, e.g. at app launch.
    public func registerBackgroundDelivery() {
        for type in HealthKitSampleTypes.all {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { [weak self] _, completionHandler, _ in
                Task { @MainActor in
                    await self?.handleObserverFire(for: type)
                    completionHandler()
                }
            }
            observerQueries.append(query)
            healthStore.execute(query)
            healthStore.enableBackgroundDelivery(for: type, frequency: .hourly) { _, _ in
                // Best-effort. A failed enable here is not distinguishable from "already
                // enabled" on a repeat call; BiometricSyncStatusRow's stalled state (driven
                // by drain outcomes, not this callback) is what actually surfaces a
                // background path that has gone quiet.
            }
        }
    }

    /// One observer fire for one type: read what's new, project it through the ONE pure
    /// projection function (BiometricSampleProjection.project — reused, not reimplemented),
    /// persist the projected bytes to the offline buffer, THEN attempt to drain the whole
    /// buffer over the background session. The anchor for `type` advances once its samples
    /// are durably in the buffer, not once they've been uploaded — the persisted buffer,
    /// not an unconfirmed in-flight anchor, is what survives the process being killed
    /// mid-transfer.
    private func handleObserverFire(for type: HKSampleType) async {
        let anchor = BiometricAnchorStore.loadAnchor(for: type)
        let (samples, newAnchor) = await runAnchoredQuery(for: type, anchor: anchor)
        guard let newAnchor else {
            status = .stalled(reason: "could not read the HealthKit store in the background")
            return
        }
        let projected = samples.compactMap { BiometricSampleProjection.project($0) }
        if !projected.isEmpty {
            BiometricOfflineBuffer.enqueue(projected)
        }
        BiometricAnchorStore.saveAnchor(newAnchor, for: type)
        BiometricAnchorStore.recordSuccessfulSync(for: type)
        await drainBuffer()
    }

    /// Drains the persisted buffer in order over the background session, via the SAME
    /// BiometricsUploadClient.upload(samples:) call site the foreground engine drives
    /// (TK-357 AC1). One batch per drain, capped at the wire's own
    /// maxSamplesPerBatch — the exact cap wombat enforces server-side.
    public func drainBuffer() async {
        let ordered = BiometricOfflineBuffer.drainOrdered()
        guard !ordered.isEmpty else {
            status = .idle
            return
        }
        let batch = Array(ordered.prefix(WireContract.Biometrics.maxSamplesPerBatch))
        switch await uploadClient.upload(samples: batch) {
        case .unreachable:
            status = .stalled(reason: "wombat is not reachable on the network right now")
        case .unauthorized:
            status = .stalled(reason: "re-pair this device")
        case .ok(let httpStatus, let value):
            guard httpStatus == 200 || httpStatus == 202, value != nil else {
                status = .stalled(reason: "wombat rejected this batch")
                return
            }
            // Confirmed accept: drop exactly the entries this batch covered, oldest
            // first, off the head of the buffer. Anything queued after this batch was
            // read stays put for the next drain.
            BiometricOfflineBuffer.removeDrained(count: batch.count)
            status = .synced(at: Date())
        }
    }

    /// The same anchored-query wrapping BiometricSyncEngine uses for the foreground pass,
    /// duplicated here (rather than shared) because the two run from different triggers —
    /// a background observer callback vs. a user-initiated "Sync now" — and this is a thin
    /// HKAnchoredObjectQuery/async wrapper, not load-bearing logic. The projection
    /// function and the upload call site, which ARE load-bearing, stay singular.
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

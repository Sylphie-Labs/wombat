//
//  HealthKitAuthorizationManager.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Requests read authorization for exactly HealthKitSampleTypes.all — the one list — and
//  nothing else. HealthKit never discloses per-type read-authorization status back to the
//  requesting app (Apple's own documented design): a granted read type and a denied read
//  type look identical afterward, both to `getRequestStatusForAuthorization` (which reports
//  only whether the SHEET would show, not what the person picked) and to a query that
//  simply returns zero rows either way. This file therefore does NOT attempt to gate query
//  construction on a per-type "was this granted" check — there is no such check to make.
//  Every HKAnchoredObjectQuery in this app (BiometricSyncEngine) is built from
//  HealthKitSampleTypes.all unconditionally; revocation is inferred elsewhere from
//  last-successful-sample age, never read off an authorization-status API.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import HealthKit

public final class HealthKitAuthorizationManager {
    private let healthStore: HKHealthStore

    public init(healthStore: HKHealthStore = HKHealthStore()) {
        self.healthStore = healthStore
    }

    public static var isHealthDataAvailable: Bool {
        HKHealthStore.isHealthDataAvailable()
    }

    /// Requests read-only authorization for exactly HealthKitSampleTypes.all, one prompt
    /// per type. wombat never writes to Health (DEC-29 absolute) — `toShare` is always
    /// empty; ios/WombatCompanion/Info.plist already declares NSHealthUpdateUsageDescription
    /// saying so.
    public func requestAuthorization() async throws {
        try await healthStore.requestAuthorization(
            toShare: [],
            read: Set(HealthKitSampleTypes.all)
        )
    }
}

//
//  HealthKitSampleTypes.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  THE single list constant every HealthKit type construction in this tree is built from
//  (TK-356 AC2). No HKQuantityType, HKCategoryType, HKWorkoutType or HKObjectType may be
//  constructed anywhere else in ios/ — authorization requests read `all`
//  (HealthKitAuthorizationManager), and every HKAnchoredObjectQuery (BiometricSyncEngine)
//  is built by iterating `all`, never by naming a type inline at the query call site. This
//  is what makes "did we ever query a type we didn't ask permission for" a one-file
//  question instead of a tree-wide audit.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import HealthKit

public enum HealthKitSampleTypes {
    /// planning/design/wire-contract.md §3.1 `sleep_session`.
    public static let sleepAnalysis = HKCategoryType(.sleepAnalysis)
    /// §3.1 `workout`.
    public static let workout = HKWorkoutType.workoutType()
    /// §3.1 `resting_hr_daily`.
    public static let restingHeartRate = HKQuantityType(.restingHeartRate)
    /// §3.1 `hrv_daily`.
    public static let heartRateVariabilitySDNN = HKQuantityType(.heartRateVariabilitySDNN)
    /// §3.1 `steps_hourly`.
    public static let stepCount = HKQuantityType(.stepCount)

    /// THE one list. HealthKitAuthorizationManager requests exactly these five read types
    /// and no others; BiometricSyncEngine builds one HKAnchoredObjectQuery per member of
    /// this array and none from any other source.
    public static let all: [HKSampleType] = [
        sleepAnalysis,
        workout,
        restingHeartRate,
        heartRateVariabilitySDNN,
        stepCount,
    ]
}

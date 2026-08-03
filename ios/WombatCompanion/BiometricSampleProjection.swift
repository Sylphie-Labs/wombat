//
//  BiometricSampleProjection.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  THE ARCHITECTURALLY LOAD-BEARING FILE. `project(_:)` is the ONE function in this app
//  that turns a raw HealthKit sample into the closed wire struct planning/design/wire-
//  contract.md §3.1/§3.2 defines. Every value it reads comes from a sample's own start
//  time, end time, numeric quantity, category value, workout activity type, or one of the
//  two energy/distance totals a workout object carries directly — nothing else. There is
//  no branch anywhere below that reaches into a sample's descriptive attribution fields;
//  reviewing this one function is the entire audit surface for "can free text reach the
//  wire" (DEC-80(b)), which is exactly why it is kept to one function rather than a
//  scattered mapping spread across call sites.
//
//  An unmapped HKWorkoutActivityType projects to `.other` and NEVER to its own name — the
//  concrete mechanism, named in the ticket brief, by which no free text crosses through the
//  activity field.
//
//  KNOWN SIMPLIFICATIONS (draft-source honesty, DEC-82(f)):
//  - `avgHrBpm`/`maxHrBpm` are always nil here. Both are the only workout fields whose
//    HealthKit source lives outside a workout object's own totals (they need a second
//    HKQuantityType query); leaving them nil — which the schema allows via `?` — keeps
//    every type construction confined to HealthKitSampleTypes.swift (TK-356 AC2) instead
//    of opening a second construction site here. Populating them for real is a natural
//    follow-up, not a wire-contract gap.
//  - HealthKit reports sleep as a series of per-stage rows, not one row per night, and step
//    counts as HealthKit's own recorded windows, not clock-hour buckets. This function
//    stays a pure PER-SAMPLE projection (the ticket's requirement) by emitting one wire
//    sample per input sample, over that sample's own window. Any coarser rollup is a
//    separate, later concern outside this function's boundary.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import HealthKit

public enum BiometricSampleProjection {

    public static func project(_ sample: HKSample) -> WireContract.Biometrics.Sample? {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime]

        switch sample {
        case let workout as HKWorkout:
            let activity: WireContract.Biometrics.Activity
            switch workout.workoutActivityType {
            case .walking: activity = .walking
            case .running: activity = .running
            case .cycling: activity = .cycling
            case .traditionalStrengthTraining, .functionalStrengthTraining: activity = .strength
            case .swimming: activity = .swimming
            case .highIntensityIntervalTraining: activity = .hiit
            case .yoga: activity = .yoga
            default: activity = .other
            }
            let kcal = workout.totalEnergyBurned?.doubleValue(for: .kilocalorie()) ?? 0
            let distanceMeters = workout.totalDistance?.doubleValue(for: .meter())
            let payload = WireContract.Biometrics.WorkoutPayload(
                activity: activity,
                durationSeconds: Int(workout.duration.rounded()),
                activeEnergyKcal: kcal,
                avgHrBpm: nil,
                maxHrBpm: nil,
                distanceMeters: distanceMeters
            )
            return WireContract.Biometrics.Sample(
                kind: .workout,
                startedAt: iso.string(from: workout.startDate),
                endedAt: iso.string(from: workout.endDate),
                payload: .workout(payload)
            )

        case let category as HKCategorySample where category.categoryType == HealthKitSampleTypes.sleepAnalysis:
            let minutes = Int(category.endDate.timeIntervalSince(category.startDate) / 60)
            var asleepMinutes = 0
            var inBedMinutes = 0
            var awakenings = 0
            if let stage = HKCategoryValueSleepAnalysis(rawValue: category.value) {
                switch stage {
                case .inBed:
                    inBedMinutes = minutes
                case .awake:
                    awakenings = 1
                case .asleepUnspecified, .asleepCore, .asleepDeep, .asleepREM:
                    asleepMinutes = minutes
                default:
                    break
                }
            }
            let payload = WireContract.Biometrics.SleepSessionPayload(
                asleepMinutes: asleepMinutes,
                inBedMinutes: inBedMinutes,
                awakenings: awakenings
            )
            return WireContract.Biometrics.Sample(
                kind: .sleepSession,
                startedAt: iso.string(from: category.startDate),
                endedAt: iso.string(from: category.endDate),
                payload: .sleepSession(payload)
            )

        case let quantity as HKQuantitySample where quantity.quantityType == HealthKitSampleTypes.restingHeartRate:
            let bpm = Int(quantity.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute())).rounded())
            return WireContract.Biometrics.Sample(
                kind: .restingHrDaily,
                startedAt: iso.string(from: quantity.startDate),
                endedAt: iso.string(from: quantity.endDate),
                payload: .restingHrDaily(WireContract.Biometrics.RestingHrDailyPayload(bpm: bpm))
            )

        case let quantity as HKQuantitySample
            where quantity.quantityType == HealthKitSampleTypes.heartRateVariabilitySDNN:
            let sdnnMs = quantity.quantity.doubleValue(for: .secondUnit(with: .milli))
            return WireContract.Biometrics.Sample(
                kind: .hrvDaily,
                startedAt: iso.string(from: quantity.startDate),
                endedAt: iso.string(from: quantity.endDate),
                payload: .hrvDaily(WireContract.Biometrics.HrvDailyPayload(sdnnMs: sdnnMs))
            )

        case let quantity as HKQuantitySample where quantity.quantityType == HealthKitSampleTypes.stepCount:
            let steps = Int(quantity.quantity.doubleValue(for: .count()).rounded())
            return WireContract.Biometrics.Sample(
                kind: .stepsHourly,
                startedAt: iso.string(from: quantity.startDate),
                endedAt: iso.string(from: quantity.endDate),
                payload: .stepsHourly(WireContract.Biometrics.StepsHourlyPayload(steps: steps))
            )

        default:
            return nil
        }
    }
}

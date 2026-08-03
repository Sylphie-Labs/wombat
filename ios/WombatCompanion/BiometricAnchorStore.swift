//
//  BiometricAnchorStore.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Persists, per HealthKit type, the last HKQueryAnchor a successful upload advanced past
//  and the timestamp of the last successful sync, so a relaunch never re-walks the whole
//  HealthKit store from scratch. Backed by UserDefaults — an anchor and a timestamp are
//  operational state, not secrets, unlike the pairing token (ios/Shared/KeychainStore.swift
//  is the Keychain-only home for that).
//
//  ANCHOR PERSISTENCE satisfies TK-349's "Sync now tapped twice in a row" criterion: the
//  first run POSTs the available samples and advances the anchor; the second run POSTs
//  nothing new because the persisted anchor already covers them.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import HealthKit

public enum BiometricAnchorStore {
    private static let defaults = UserDefaults.standard

    private static func anchorKey(_ type: HKSampleType) -> String {
        "wombat.biometrics.anchor.\(type.identifier)"
    }

    private static func lastSuccessKey(_ type: HKSampleType) -> String {
        "wombat.biometrics.lastSuccess.\(type.identifier)"
    }

    public static func loadAnchor(for type: HKSampleType) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: anchorKey(type)) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    /// Call ONLY after a batch this anchor covers has been accepted by wombat (a §3
    /// response `202`). BiometricSyncEngine is the sole caller, and it is annotated with
    /// the same TK-349 criterion this store exists to satisfy: a FAILED sync must never
    /// reach this call.
    public static func saveAnchor(_ anchor: HKQueryAnchor, for type: HKSampleType) {
        guard let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true)
        else {
            return
        }
        defaults.set(data, forKey: anchorKey(type))
    }

    public static func lastSuccessfulSampleAt(for type: HKSampleType) -> Date? {
        defaults.object(forKey: lastSuccessKey(type)) as? Date
    }

    public static func recordSuccessfulSync(for type: HKSampleType, at date: Date = Date()) {
        defaults.set(date, forKey: lastSuccessKey(type))
    }

    /// TK-357's app-side reset (a later ticket, out of this ticket's scope) clears this
    /// alongside the offline buffer so the DEC-75 wipe's promise reaches the device side
    /// too. Not called anywhere in this ticket's own flow.
    public static func clear(for type: HKSampleType) {
        defaults.removeObject(forKey: anchorKey(type))
        defaults.removeObject(forKey: lastSuccessKey(type))
    }
}

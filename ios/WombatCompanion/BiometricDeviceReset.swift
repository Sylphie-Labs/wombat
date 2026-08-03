//
//  BiometricDeviceReset.swift
//  ios/WombatCompanion
//
//  TK-357 — DRAFT SOURCE (DEC-82 tier A).
//
//  THE APP-SIDE RESET. The DEC-75 schema-driven wipe lives on wombat's own host and its
//  blast radius stops at wombat's own tables — it has no reach into a phone's local
//  storage. BiometricOfflineBuffer's persisted bytes and BiometricAnchorStore's per-type
//  anchors both live entirely on-device, OUTSIDE that blast radius, so without this
//  control the wipe's promise is quietly incomplete on the phone side — the gap the
//  TK-342 wipe dialog now names. Calling `reset()` is what closes it: the operator can
//  start clean on BOTH sides of a wipe.
//
//  Clears the buffer AND every per-type anchor in ONE function, deliberately, so a caller
//  cannot clear one half and forget the other.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import HealthKit

public enum BiometricDeviceReset {
    /// Clears BiometricOfflineBuffer's queued payload bytes and every per-type anchor in
    /// HealthKitSampleTypes.all (BiometricAnchorStore) so the next sync re-reads from the
    /// anchorless start on both the buffer side and the HealthKit side.
    public static func reset() {
        BiometricOfflineBuffer.clear()
        for type in HealthKitSampleTypes.all {
            BiometricAnchorStore.clear(for: type)
        }
    }
}

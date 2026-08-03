//
//  WatchChargingMonitor.swift
//  ios/WombatCompanion Watch App
//
//  TK-360 — DRAFT SOURCE (DEC-82 tier A).
//
//  Reports whether THIS watch is on its charger right now. This is one of the two real
//  costs TK-360's intent names explicitly: speaker playback is not supported while the
//  watch is charging, and that is a genuine dead state — PTT works, wombat replies, and
//  nothing plays. WatchUtterancePlaybackController reads this monitor immediately before
//  attempting playback and renders a real UI state when it reports true; see that file.
//
//  Deliberately the ONLY thing this type does. It knows nothing about the wire, the
//  playback client, or the drafted UI states — a charge/no-charge reading, nothing else.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import WatchKit

public final class WatchChargingMonitor {

    public init() {
        // watchOS reports `.unknown` for every read until monitoring is explicitly turned
        // on; this call is what makes the property below mean anything.
        WKInterfaceDevice.current().isBatteryMonitoringEnabled = true
    }

    /// True only for `.charging` and `.full` — the two states in which watchOS itself
    /// silences the speaker regardless of what this app does. `.unknown` (monitoring not
    /// yet settled) reads as false rather than guessing a charge state that has not been
    /// reported yet, matching the "never guess a value wombat/watchOS has not actually
    /// given us" discipline WatchTalkSessionController's staleness check already follows
    /// (see that file's `isAlreadyStale`).
    public var isCharging: Bool {
        let state = WKInterfaceDevice.current().batteryState
        return state == .charging || state == .full
    }
}
